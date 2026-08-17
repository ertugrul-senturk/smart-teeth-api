import hmac

from flask import request, jsonify, g
from functools import wraps
from app.utils.token import decode_token
from app.services import AuthService
from app.config import Config


def api_key_required(f):
    """Auth for the desktop app's endpoints — static shared keys instead of
    a user token, since the desktop app has no login. Keys can be named
    (DESKTOP_API_KEYS=name:key,…) so the audit log can attribute actions;
    the matched name is exposed as g.api_key_name."""
    @wraps(f)
    def decorated(*args, **kwargs):
        keys = Config.DESKTOP_API_KEYS
        if not keys:
            return jsonify({'message': 'Desktop sync is not configured'}), 503

        provided = request.headers.get('X-API-Key', '')
        matched = None
        # Compare against every key (no early break) to keep timing flat.
        for name, key in keys.items():
            if provided and hmac.compare_digest(provided, key):
                matched = name

        if matched is None:
            return jsonify({'message': 'Invalid API key'}), 401

        g.api_key_name = matched
        return f(*args, **kwargs)

    return decorated


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

        if getattr(current_user, 'status', 'active') == 'suspended':
            return jsonify({'message': 'This account has been suspended'}), 403

        return f(current_user, *args, **kwargs)

    return decorated
