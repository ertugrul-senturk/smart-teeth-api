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

        user_response = auth_service.register(username, name, email, password)
        return jsonify(user_response), 201

    except ValueError as e:
        return jsonify({'message': str(e)}), 400
    except Exception as e:
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
    except Exception as e:
        return jsonify({'message': 'An error occurred'}), 500

@auth_bp.route('/update-profile', methods=['PUT'])
@token_required
def update_profile(current_user):
    try:
        data = request.get_json()

        name = data.get('name')
        password_data = data.get('password')

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
            new_password=new_password
        )

        return jsonify(user_response), 200

    except ValueError as e:
        return jsonify({'message': str(e)}), 401
    except Exception as e:
        return jsonify({'message': 'An error occurred'}), 500

@auth_bp.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'healthy'}), 200
