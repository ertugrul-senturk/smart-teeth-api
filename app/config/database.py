import os
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """Raised at startup when required environment variables are missing."""
    pass


def _validate_required():
    """Fail fast with one clear message instead of a mid-request stack trace."""
    missing = [name for name in ('SECRET_KEY', 'MONGODB_URI', 'DATABASE_NAME')
               if not os.getenv(name)]
    if missing:
        raise ConfigError(
            f"Missing required environment variables: {', '.join(missing)}. "
            f"Copy .env.example to .env and fill them in."
        )


def _parse_desktop_keys():
    """Desktop API keys, as {name: key}.

    DESKTOP_API_KEYS="clinic-a:key1,clinic-b:key2" gives each key holder a name
    (used in the audit log) and lets a single leaked key be revoked without
    rotating everyone. The legacy single DESKTOP_API_KEY still works and shows
    up under the name 'default'.
    """
    keys = {}
    raw = os.getenv('DESKTOP_API_KEYS') or ''
    for pair in raw.split(','):
        pair = pair.strip()
        if not pair:
            continue
        if ':' in pair:
            name, key = pair.split(':', 1)
        else:
            name, key = f'key{len(keys) + 1}', pair
        if name.strip() and key.strip():
            keys[name.strip()] = key.strip()

    single = os.getenv('DESKTOP_API_KEY')
    if single and single.strip():
        keys.setdefault('default', single.strip())
    return keys


_validate_required()


class Config:
    SECRET_KEY = os.getenv('SECRET_KEY')
    MONGODB_URI = os.getenv('MONGODB_URI')
    DATABASE_NAME = os.getenv('DATABASE_NAME')
    PORT = int(os.getenv('PORT') or 5000)
    HOST = os.getenv('HOST') or '0.0.0.0'
    DEBUG = os.getenv('DEBUG') == 'True'

    # ── Secret separation ────────────────────────────────────────────────
    # Each concern gets its own secret, all defaulting to SECRET_KEY so
    # existing deployments (tokens, encrypted fields, blind indexes) keep
    # working. Set them individually in prod so leaking one doesn't leak all.
    JWT_SECRET = os.getenv('JWT_SECRET') or SECRET_KEY
    BLIND_INDEX_PEPPER = os.getenv('BLIND_INDEX_PEPPER') or SECRET_KEY
    # NOTE: changing the salt re-keys field encryption — existing user rows
    # would no longer decrypt. Only set this on a fresh database.
    ENCRYPTION_SALT = os.getenv('ENCRYPTION_SALT') or 'static_salt_change_me'

    # ── Request/payload limits ───────────────────────────────────────────
    MAX_CONTENT_LENGTH_MB = int(os.getenv('MAX_CONTENT_LENGTH_MB') or 64)
    MAX_IMAGE_MB = int(os.getenv('MAX_IMAGE_MB') or 10)

    # ── CORS ─────────────────────────────────────────────────────────────
    # Comma-separated origin allowlist; '*' (default) keeps current behavior.
    CORS_ORIGINS = [o.strip() for o in (os.getenv('CORS_ORIGINS') or '*').split(',') if o.strip()]

    # ── Status page ──────────────────────────────────────────────────────
    # The landing page at '/' ("Smart Teeth server is up and running" + a live
    # storage-reachability dot). Set False to hide it (returns 404) — it does a
    # Mongo ping and reveals storage status, so disable it on a public host.
    SHOW_STATUS_PAGE = (os.getenv('SHOW_STATUS_PAGE') or 'True') == 'True'

    # ── Rate limiting (auth endpoints) ───────────────────────────────────
    RATELIMIT_ENABLED = (os.getenv('RATELIMIT_ENABLED') or 'True') == 'True'
    RATELIMIT_LOGIN = os.getenv('RATELIMIT_LOGIN') or '10 per minute'
    RATELIMIT_RESET = os.getenv('RATELIMIT_RESET') or '5 per hour'

    # Email (SMTP) — used to send password reset links. Leave SMTP_HOST unset to
    # disable sending; in that case the reset link is logged to the console,
    # which is enough for local testing.
    SMTP_HOST = os.getenv('SMTP_HOST')
    SMTP_PORT = int(os.getenv('SMTP_PORT') or 587)
    SMTP_USER = os.getenv('SMTP_USER')
    SMTP_PASSWORD = os.getenv('SMTP_PASSWORD')
    SMTP_FROM = os.getenv('SMTP_FROM') or os.getenv('SMTP_USER')
    SMTP_USE_TLS = (os.getenv('SMTP_USE_TLS') or 'True') == 'True'

    # Target for the reset link in the email. For the mobile app this is a deep
    # link; for a web flow it would be an https URL.
    PASSWORD_RESET_URL = os.getenv('PASSWORD_RESET_URL') or 'smart-teeth://reset-password'

    # Desktop app keys as {name: key} — see _parse_desktop_keys. Empty dict
    # means desktop sync is disabled (endpoints return 503).
    DESKTOP_API_KEYS = _parse_desktop_keys()

class Database:
    _instance = None
    _client = None
    _db = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(Database, cls).__new__(cls)
        return cls._instance

    def connect(self):
        if self._client is None:
            self._client = MongoClient(Config.MONGODB_URI)
            self._db = self._client[Config.DATABASE_NAME]
        return self._db

    def get_db(self):
        if self._db is None:
            self.connect()
        return self._db

    def close(self):
        if self._client:
            self._client.close()
            self._client = None
            self._db = None

db_instance = Database()


def ensure_indexes(db):
    """Create the unique indexes sync upserts rely on.

    Pre-existing duplicate data (created before upserts were keyed on
    (userId, id)) makes create_index raise — log and keep serving so a dirty
    collection never blocks startup.
    """
    from app.config.collections import (
        ALLOWED_SYNC_COLLECTIONS,
        DESKTOP_HISTORY_COLLECTION,
        DESKTOP_IMAGES_COLLECTION,
        IMAGES_COLLECTION,
        IMPORT_LINKS_COLLECTION,
        AUDIT_LOG_COLLECTION,
    )

    # Users: unique blind-index lookups. Created here once at startup —
    # previously UserRepository recreated these on every request.
    try:
        db['users'].create_index('eh', unique=True)
        db['users'].create_index('uh', unique=True)
    except Exception as e:
        print(f"WARNING: could not create index on 'users': {e}")

    # Mobile images are always queried by owner.
    try:
        db[IMAGES_COLLECTION].create_index('userId')
    except Exception as e:
        print(f"WARNING: could not create index on '{IMAGES_COLLECTION}': {e}")

    # Provenance ledger: one link per (source record, desktop device).
    try:
        db[IMPORT_LINKS_COLLECTION].create_index(
            [('sourceCollection', 1), ('sourceUserId', 1), ('sourceRecordId', 1), ('deviceId', 1)],
            unique=True,
            name='uniq_source_device',
        )
        db[IMPORT_LINKS_COLLECTION].create_index([('sourceUserId', 1), ('sourceCollection', 1)])
    except Exception as e:
        print(f"WARNING: could not create index on '{IMPORT_LINKS_COLLECTION}': {e}")

    try:
        db[AUDIT_LOG_COLLECTION].create_index([('ts', -1)])
    except Exception as e:
        print(f"WARNING: could not create index on '{AUDIT_LOG_COLLECTION}': {e}")

    for name in ALLOWED_SYNC_COLLECTIONS:
        try:
            db[name].create_index(
                [('userId', 1), ('id', 1)],
                unique=True,
                name='uniq_userId_id',
                # Legacy docs synced without an `id` field must not collide.
                partialFilterExpression={'id': {'$exists': True}},
            )
        except Exception as e:
            print(f"WARNING: could not create index on '{name}': {e}")

    for name in (DESKTOP_HISTORY_COLLECTION, DESKTOP_IMAGES_COLLECTION):
        try:
            db[name].create_index(
                [('deviceId', 1), ('id', 1)],
                unique=True,
                name='uniq_deviceId_id',
            )
        except Exception as e:
            print(f"WARNING: could not create index on '{name}': {e}")
