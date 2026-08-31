from flask import Blueprint, request, jsonify, current_app
from app.config import Config
from app.services import AuthService
from app.services.auth_service import AccountSuspendedError
from app.utils.middleware import token_required
from app.utils.rate_limit import limiter

auth_bp = Blueprint('auth', __name__)
auth_service = AuthService()


@auth_bp.route('/register', methods=['POST'])
def register():
    try:
        data = request.get_json()

        username = data.get('username')
        name = data.get('name')
        email = data.get('email')
        password = data.get('password')

        if not all([username, name, email, password]):
            return jsonify({'message': 'Missing required fields'}), 400

        optional_fields = {
            'age': data.get('age'),
            'gender': data.get('gender'),
            'ethnicity': data.get('ethnicity'),
            'has_insurance': data.get('has_insurance'),
            'last_doctor_visit': data.get('last_doctor_visit'),
        }
        # Remove None values so they don't override defaults
        optional_fields = {k: v for k, v in optional_fields.items() if v is not None}

        user_response = auth_service.register(username, name, email, password, **optional_fields)
        return jsonify(user_response), 201

    except ValueError as e:
        return jsonify({'message': str(e)}), 400
    except Exception:
        return jsonify({'message': 'An error occurred'}), 500


@auth_bp.route('/login', methods=['POST'])
@limiter.limit(Config.RATELIMIT_LOGIN)
def login():
    try:
        data = request.get_json()

        username = data.get('username')
        email = data.get('email')
        password = data.get('password')

        if not username and not email:
            return jsonify({'message': 'Missing login fields'}), 400

        if not password:
            return jsonify({'message': 'Missing password'}), 400

        user_response = None
        if username:
            user_response = auth_service.login_with_username(username, password)
        elif email:
            user_response = auth_service.login_with_email(email, password)
        return jsonify(user_response), 200

    except AccountSuspendedError as e:
        # Credentials were correct but the account is suspended — flag it so the
        # app can offer reactivation instead of a plain failure.
        return jsonify({'message': str(e), 'suspended': True}), 403
    except ValueError as e:
        return jsonify({'message': str(e)}), 401
    except Exception:
        return jsonify({'message': 'An error occurred'}), 500


@auth_bp.route('/reactivate', methods=['POST'])
@limiter.limit(Config.RATELIMIT_LOGIN)
def reactivate():
    try:
        data = request.get_json(silent=True) or {}
        username = data.get('username')
        email = data.get('email')
        password = data.get('password')

        if not username and not email:
            return jsonify({'message': 'Missing login fields'}), 400
        if not password:
            return jsonify({'message': 'Missing password'}), 400

        result = auth_service.reactivate_account(username=username, email=email, password=password)
        return jsonify(result), 200

    except ValueError as e:
        return jsonify({'message': str(e)}), 401
    except Exception:
        return jsonify({'message': 'An error occurred'}), 500


@auth_bp.route('/update-profile', methods=['PUT'])
@token_required
def update_profile(current_user):
    try:
        data = request.get_json()

        name = data.get('name')
        password_data = data.get('password')
        profile_data = data.get('profile_data')

        current_password = None
        new_password = None

        if password_data:
            current_password = password_data.get('currentPassword')
            new_password = password_data.get('newPassword')

            if not current_password or not new_password:
                return jsonify({'message': 'Missing password fields'}), 400

        user_response = auth_service.update_profile(
            str(current_user._id),
            name=name,
            current_password=current_password,
            new_password=new_password,
            profile_data=profile_data,
        )

        return jsonify(user_response), 200

    except ValueError as e:
        return jsonify({'message': str(e)}), 401
    except Exception:
        return jsonify({'message': 'An error occurred'}), 500


@auth_bp.route('/forgot-password', methods=['POST'])
@limiter.limit(Config.RATELIMIT_RESET)
def forgot_password():
    data = request.get_json(silent=True) or {}
    email = data.get('email')

    try:
        if email:
            auth_service.request_password_reset(email)
    except Exception:
        # Log but don't leak failures — the response is identical regardless.
        current_app.logger.exception('[forgot-password] failed')

    # Always the same response so callers can't tell whether the email exists.
    return jsonify({'message': 'If that email is registered, a reset link has been sent'}), 200


@auth_bp.route('/reset-password', methods=['POST'])
@limiter.limit(Config.RATELIMIT_RESET)
def reset_password():
    try:
        data = request.get_json(silent=True) or {}
        token = data.get('token')
        new_password = data.get('password')

        if not token or not new_password:
            return jsonify({'message': 'Missing token or password'}), 400

        result = auth_service.reset_password(token, new_password)
        return jsonify(result), 200

    except ValueError as e:
        return jsonify({'message': str(e)}), 400
    except Exception:
        return jsonify({'message': 'An error occurred'}), 500


@auth_bp.route('/suspend', methods=['POST'])
@token_required
def suspend_account(current_user):
    try:
        result = auth_service.suspend_account(str(current_user._id))
        return jsonify(result), 200
    except ValueError as e:
        return jsonify({'message': str(e)}), 400
    except Exception:
        return jsonify({'message': 'An error occurred'}), 500


@auth_bp.route('/account', methods=['DELETE'])
@token_required
def delete_account(current_user):
    try:
        result = auth_service.delete_account(str(current_user._id))
        return jsonify(result), 200
    except ValueError as e:
        return jsonify({'message': str(e)}), 400
    except Exception:
        return jsonify({'message': 'An error occurred'}), 500


@auth_bp.route('/me', methods=['GET'])
@token_required
def me(current_user):
    # Lightweight existence probe for clients: token_required already answers
    # 401 when the account is gone (e.g. deleted from the desktop app), so
    # reaching this handler means the account is still alive.
    return jsonify({'id': str(current_user._id), 'username': current_user.username}), 200


@auth_bp.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'healthy'}), 200