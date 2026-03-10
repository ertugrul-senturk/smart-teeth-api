from flask import Blueprint, request, jsonify
from app.services import AuthService
from app.utils.middleware import token_required

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


@auth_bp.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'healthy'}), 200