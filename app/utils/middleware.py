from flask import request, jsonify
from functools import wraps
from app.utils.token import decode_token
from app.services import AuthService

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization')

        if not token:
            return jsonify({'message': 'Token is missing'}), 401

        payload = decode_token(token)
        
        if not payload:
            return jsonify({'message': 'Token is invalid or expired'}), 401

        try:
            auth_service = AuthService()
            current_user = auth_service.get_user_by_id(payload['user_id'])
        except ValueError:
            return jsonify({'message': 'User not found'}), 401

        return f(current_user, *args, **kwargs)

    return decorated
