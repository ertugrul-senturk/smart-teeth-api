import hmac

from flask import request, jsonify, g
from functools import wraps
from app.utils.token import decode_token
from app.services import AuthService
from app.config import Config, db_instance


def api_key_required(f):
    """Auth for the desktop app's endpoints — a registration key created by
    the master user in the admin tab (stored hashed in Mongo, checked against
    its validity window) instead of a user token, since the desktop app has
    no login. The master registration key itself is also accepted: the app
    has a single key field, and the master key doubles as a regular key with
    admin on top. The matched key's name is exposed as g.api_key_name; for
    registration keys the doc is on g.registration_key."""
    @wraps(f)
    def decorated(*args, **kwargs):
        from app.repositories.registration_key_repository import (
            RegistrationKeyRepository, effective_status,
        )

        master = Config.MASTER_REGISTRATION_KEY
        if not master:
            return jsonify({'message': 'Desktop sync is not configured'}), 503

        provided = request.headers.get('X-API-Key', '')

        g.registration_key = None
        matched = None
        if provided and hmac.compare_digest(
                provided.encode('utf-8'), master.encode('utf-8')):
            matched = 'master'

        if matched is None:
            # Registration keys: constant-time blind-index lookup.
            repo = RegistrationKeyRepository(db_instance.get_db())
            doc = repo.find_by_plain_key(provided)
            if doc is not None:
                status = effective_status(doc)
                if status == 'active':
                    matched = doc['name']
                    g.registration_key = doc
                elif status == 'expired':
                    return jsonify({'message': 'Registration key has expired'}), 401
                else:  # scheduled
                    return jsonify({'message': 'Registration key is not active yet'}), 401

        if matched is None:
            return jsonify({'message': 'Invalid API key'}), 401

        g.api_key_name = matched
        return f(*args, **kwargs)

    return decorated


def master_key_required(f):
    """Auth for the /v1/admin key-management endpoints. The master
    registration key lives only in the server's .env (MASTER_REGISTRATION_KEY)
    and is presented by the desktop app's hidden admin tab via X-Master-Key.
    It is deliberately not accepted by api_key_required."""
    @wraps(f)
    def decorated(*args, **kwargs):
        master = Config.MASTER_REGISTRATION_KEY
        if not master:
            return jsonify({'message': 'Key administration is not configured'}), 503

        provided = request.headers.get('X-Master-Key', '')
        if not provided or not hmac.compare_digest(
                provided.encode('utf-8'), master.encode('utf-8')):
            return jsonify({'message': 'Invalid master key'}), 401

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
