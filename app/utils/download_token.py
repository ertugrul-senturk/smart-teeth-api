"""Short-lived HMAC tokens for update-installer downloads.

The update *check* is authenticated by the device's registration key, but the
Tauri updater's follow-up download request may not carry custom headers — so
the check response embeds a signed, expiring token in the download URL
instead. No DB state: the token is HMAC(SECRET_KEY, assetId:expiry).
"""

import hashlib
import hmac
import time

from app.config import Config

DOWNLOAD_TOKEN_TTL_SECS = 6 * 3600


def make_download_token(asset_id, ttl_secs=DOWNLOAD_TOKEN_TTL_SECS):
    """→ (expires_epoch, signature_hex) for the given asset."""
    expires = int(time.time()) + ttl_secs
    return expires, _sign(asset_id, expires)


def verify_download_token(asset_id, expires, signature):
    try:
        expires = int(expires)
    except (TypeError, ValueError):
        return False
    if expires < time.time():
        return False
    return hmac.compare_digest(_sign(asset_id, expires), signature or '')


def _sign(asset_id, expires):
    return hmac.new(
        Config.SECRET_KEY.encode(),
        f'{asset_id}:{expires}'.encode(),
        hashlib.sha256,
    ).hexdigest()
