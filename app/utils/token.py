import jwt
import hashlib
from datetime import datetime, timezone, timedelta
from app.config import Config

def generate_token(user_id):
    payload = {
        'user_id': str(user_id),
        'exp': datetime.now(timezone.utc) + timedelta(days=30),
        'iat': datetime.now(timezone.utc)
    }
    return jwt.encode(payload, Config.JWT_SECRET, algorithm='HS256')

def decode_token(token):
    try:
        if token.startswith('Bearer '):
            token = token[7:]
        payload = jwt.decode(token, Config.JWT_SECRET, algorithms=['HS256'])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None

def password_fingerprint(password_hash):
    """Short non-reversible fingerprint of the current password hash. Baked
    into reset tokens so a token dies the moment the password changes —
    which makes every reset link effectively single-use."""
    return hashlib.sha256((password_hash or '').encode()).hexdigest()[:16]

def generate_reset_token(user_id, password_hash):
    """Short-lived (1 hour) token scoped to password resets only, bound to
    the password hash it is meant to replace."""
    payload = {
        'user_id': str(user_id),
        'purpose': 'password_reset',
        'pwd': password_fingerprint(password_hash),
        'exp': datetime.now(timezone.utc) + timedelta(hours=1),
        'iat': datetime.now(timezone.utc)
    }
    return jwt.encode(payload, Config.JWT_SECRET, algorithm='HS256')

def decode_reset_token(token):
    """Validate a reset token's signature, expiry and purpose. Returns the
    payload or None. The caller must additionally compare payload['pwd']
    against password_fingerprint(user.password) — a mismatch means the token
    was already used (or predates a password change)."""
    try:
        if token.startswith('Bearer '):
            token = token[7:]
        payload = jwt.decode(token, Config.JWT_SECRET, algorithms=['HS256'])
        if payload.get('purpose') != 'password_reset':
            return None
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
