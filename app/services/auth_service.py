from app.repositories import UserRepository
from app.models import User
from app.utils import hash_password, verify_password, generate_token


class AuthService:
    def __init__(self):
        self.user_repository = UserRepository()

    def register(self, username, name, email, password,
                 age=None, gender=None, ethnicity=None, has_insurance=None, last_doctor_visit=None):
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
            raise ValueError('Invalid username or password')

        if not verify_password(password, user.password):
            raise ValueError('Invalid username or password')

        token = generate_token(user._id)

        user_response = user.to_json()
        user_response['token'] = token

        return user_response

    def login_with_email(self, email, password):
        user = self.user_repository.find_by_email(email)

        if not user:
            raise ValueError('Invalid email or password')

        if not verify_password(password, user.password):
            raise ValueError('Invalid email or password')

        token = generate_token(user._id)

        user_response = user.to_json()
        user_response['token'] = token

        return user_response

    def get_user_by_id(self, user_id):
        user = self.user_repository.find_by_id(user_id)
        if not user:
            raise ValueError('User not found')
        return user

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