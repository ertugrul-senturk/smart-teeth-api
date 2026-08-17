from datetime import datetime, timezone

from app.repositories import UserRepository
from app.models import User
from app.config import Config, db_instance


class AccountSuspendedError(ValueError):
    """Raised on a valid login to a suspended account, so callers can offer
    reactivation instead of showing a generic failure."""
    pass
from app.utils import (
    hash_password,
    verify_password,
    generate_token,
    generate_reset_token,
    decode_reset_token,
    password_fingerprint,
    send_email,
)
from app.utils.audit import audit

MIN_PASSWORD_LENGTH = 6


class AuthService:
    def __init__(self):
        self.user_repository = UserRepository()

    def register(self, username, name, email, password,
                 age=None, gender=None, ethnicity=None, has_insurance=None, last_doctor_visit=None):
        if not email or '@' not in email or '.' not in email.split('@')[-1]:
            raise ValueError('Invalid email address')

        if not password or len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f'Password must be at least {MIN_PASSWORD_LENGTH} characters')

        if self.user_repository.exists_by_email(email):
            raise ValueError('Email already exists')

        if self.user_repository.exists_by_username(username):
            raise ValueError('Username already exists')

        hashed_password = hash_password(password)
        user = User(
            username=username,
            name=name,
            email=email,
            password=hashed_password,
            age=age,
            gender=gender,
            ethnicity=ethnicity,
            has_insurance=has_insurance,
            last_doctor_visit=last_doctor_visit,
        )

        created_user = self.user_repository.create(user)
        token = generate_token(created_user._id)

        user_response = created_user.to_json()
        user_response['token'] = token

        return user_response

    def login_with_username(self, username, password):
        user = self.user_repository.find_by_username(username)

        if not user:
            raise ValueError('Invalid login or password')

        if not verify_password(password, user.password):
            raise ValueError('Invalid login or password')

        if user.status == 'suspended':
            raise AccountSuspendedError('This account has been suspended')

        token = generate_token(user._id)

        user_response = user.to_json()
        user_response['token'] = token

        return user_response

    def login_with_email(self, email, password):
        user = self.user_repository.find_by_email(email)

        if not user:
            raise ValueError('Invalid login or password')

        if not verify_password(password, user.password):
            raise ValueError('Invalid login or password')

        if user.status == 'suspended':
            raise AccountSuspendedError('This account has been suspended')

        token = generate_token(user._id)

        user_response = user.to_json()
        user_response['token'] = token

        return user_response

    def get_user_by_id(self, user_id):
        user = self.user_repository.find_by_id(user_id)
        if not user:
            raise ValueError('User not found')
        return user

    def suspend_account(self, user_id):
        """Suspend an account. The email/username stay reserved, so a new
        account cannot be created with them while suspended."""
        user = self.user_repository.find_by_id(user_id)
        if not user:
            raise ValueError('User not found')
        if user.status == 'suspended':
            raise ValueError('Account is already suspended')

        self.user_repository.suspend(user_id)
        updated = self.user_repository.find_by_id(user_id)
        return {
            'message': 'Account suspended',
            'status': updated.status,
            'suspended_at': updated.suspended_at.isoformat() if updated.suspended_at else None,
        }

    def reactivate_account(self, username=None, email=None, password=None):
        """Reactivate a suspended account using its own credentials (no session
        token, since a suspended user can't obtain one). Returns the normal
        login payload (user + token) so the app can sign them straight in."""
        user = None
        if username:
            user = self.user_repository.find_by_username(username)
        elif email:
            user = self.user_repository.find_by_email(email)

        if not user or not verify_password(password, user.password):
            raise ValueError('Invalid login or password')

        if user.status == 'suspended':
            self.user_repository.reactivate(user._id)
            user = self.user_repository.find_by_id(user._id)

        token = generate_token(user._id)
        user_response = user.to_json()
        user_response['token'] = token
        return user_response

    def delete_account(self, user_id):
        """Permanently remove an account from active use. The user document
        is archived (with a deletion timestamp) and the email/username become
        available for a brand-new registration. All the account's sync data
        and images are hard-deleted — nothing medical lingers unowned."""
        from app.repositories.sync_repository import SyncRepository

        user = self.user_repository.find_by_id(user_id)
        if not user:
            raise ValueError('User not found')

        self.user_repository.archive_and_delete(user_id)
        removed = SyncRepository(db_instance.get_db()).purge_user_data(str(user_id))
        audit('account.delete', f'user:{user_id}', purged=removed)
        return {
            'message': 'Account deleted',
            'deleted_at': datetime.now(timezone.utc).isoformat(),
        }

    def _build_reset_link(self, token):
        base = Config.PASSWORD_RESET_URL
        separator = '&' if '?' in base else '?'
        return f"{base}{separator}token={token}"

    def request_password_reset(self, email):
        """
        Email a password reset link to the account matching `email`.

        Does nothing (no error) when the email isn't registered — the caller
        must return the same response either way so attackers can't probe which
        emails have accounts (enumeration protection).
        """
        user = self.user_repository.find_by_email(email)
        if not user:
            return

        # Bound to the current password hash — becomes invalid the moment
        # the password changes, so each link works exactly once.
        reset_token = generate_reset_token(user._id, user.password)
        reset_link = self._build_reset_link(reset_token)

        subject = 'Reset your Smart Teeth password'
        body = (
            f"Hi {user.name},\n\n"
            f"We received a request to reset your Smart Teeth password.\n"
            f"Use the link below within 1 hour to choose a new password:\n\n"
            f"{reset_link}\n\n"
            f"If you didn't request this, you can safely ignore this email.\n"
        )

        sent = send_email(user.email, subject, body)
        if not sent:
            # No SMTP configured (or it failed) — log the link so it can still
            # be used for local testing.
            print(f"[password-reset] Reset link for {email}: {reset_link}")

    def reset_password(self, token, new_password):
        payload = decode_reset_token(token)
        if not payload:
            raise ValueError('Invalid or expired reset link')

        if not new_password or len(new_password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f'Password must be at least {MIN_PASSWORD_LENGTH} characters')

        user = self.user_repository.find_by_id(payload.get('user_id'))
        if not user:
            raise ValueError('Invalid or expired reset link')

        # Single-use check: the token carries a fingerprint of the password
        # hash it was issued against. Once the password changes (by this
        # reset or any other), the fingerprint no longer matches.
        if payload.get('pwd') != password_fingerprint(user.password):
            raise ValueError('Invalid or expired reset link')

        self.user_repository.update(str(user._id), {'password': hash_password(new_password)})
        return {'message': 'Password updated successfully'}

    def update_profile(self, user_id, name=None, current_password=None, new_password=None, profile_data=None):
        user = self.user_repository.find_by_id(user_id)

        if not user:
            raise ValueError('User not found')

        update_data = {}

        if name:
            update_data['name'] = name

        if current_password and new_password:
            if not verify_password(current_password, user.password):
                raise ValueError('Current password is incorrect')

            update_data['password'] = hash_password(new_password)

        if profile_data:
            # Merge new profile fields with existing ones so partial updates work
            existing_profile = {}
            if user.age is not None:
                existing_profile['a'] = user.age
            if user.gender is not None:
                existing_profile['g'] = user.gender
            if user.ethnicity is not None:
                existing_profile['e'] = user.ethnicity
            if user.has_insurance is not None:
                existing_profile['i'] = user.has_insurance
            if user.last_doctor_visit is not None:
                existing_profile['d'] = user.last_doctor_visit

            # Map incoming frontend keys to short keys
            key_map = {
                'age': 'a',
                'gender': 'g',
                'ethnicity': 'e',
                'has_insurance': 'i',
                'last_doctor_visit': 'd',
            }
            for frontend_key, short_key in key_map.items():
                if frontend_key in profile_data:
                    val = profile_data[frontend_key]
                    if val is not None:
                        existing_profile[short_key] = val
                    else:
                        existing_profile.pop(short_key, None)

            update_data['profile_data'] = existing_profile if existing_profile else None

        if update_data:
            self.user_repository.update(user_id, update_data)
            user = self.user_repository.find_by_id(user_id)

        return user.to_json()