import secrets
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId

from app.config.collections import (
    REGISTRATION_KEYS_COLLECTION,
    KEY_INSTALLS_COLLECTION,
)
from app.services.security_service import SecurityService

# License-key alphabet: uppercase alphanumerics minus the ambiguous 0/O/1/I,
# so keys survive being read over the phone or retyped from paper.
_KEY_ALPHABET = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ'
_KEY_GROUPS = 5
_KEY_GROUP_LEN = 5


def generate_key():
    """A registration key in classic licensing format: XXXXX-XXXXX-XXXXX-XXXXX-XXXXX."""
    groups = (
        ''.join(secrets.choice(_KEY_ALPHABET) for _ in range(_KEY_GROUP_LEN))
        for _ in range(_KEY_GROUPS)
    )
    return '-'.join(groups)


def _utcnow():
    return datetime.now(timezone.utc)


def _aware(dt):
    """Mongo returns naive UTC datetimes — make them comparable to aware ones."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def effective_status(doc, now=None):
    """The status a key actually has right now: manual expiry wins, then the
    date window (scheduled before startsAt, expired after expiresAt)."""
    now = now or _utcnow()
    if doc.get('status') != 'active':
        return 'expired'
    if _aware(doc.get('startsAt')) and now < _aware(doc['startsAt']):
        return 'scheduled'
    if _aware(doc.get('expiresAt')) and now >= _aware(doc['expiresAt']):
        return 'expired'
    return 'active'


class RegistrationKeyRepository:
    """Registration (licensing) keys created by the master user, plus the
    per-device install ledger behind the admin tab's usage stats.

    The key itself is never stored in plaintext: `keyHash` (blind index) is
    the lookup column, `keyEnc` (Fernet) lets the master reveal it later.
    Installs are keyed by `keyRef` — the key's ObjectId as a string. (Rows
    with the historical 'env:<name>' refs from removed legacy .env keys may
    linger in old databases; they simply never match a managed key.)
    """

    def __init__(self, db):
        self.keys = db[REGISTRATION_KEYS_COLLECTION]
        self.installs = db[KEY_INSTALLS_COLLECTION]

    # ── Key CRUD (master only) ───────────────────────────────────────────

    def create_key(self, name, starts_at, expires_at):
        """Create a key and return (doc, plain_key). The plaintext is returned
        exactly once here; afterwards it's only available via reveal_key."""
        plain = generate_key()
        now = _utcnow()
        doc = {
            'name': name,
            'keyHash': SecurityService.generate_blind_index(plain),
            'keyEnc': SecurityService.encrypt(plain),
            'keyHint': plain.rsplit('-', 1)[-1],
            'status': 'active',
            'startsAt': starts_at,
            'expiresAt': expires_at,
            'createdAt': now,
            'expiredAt': None,
        }
        doc['_id'] = self.keys.insert_one(doc).inserted_id
        return doc, plain

    def find_by_plain_key(self, provided):
        """Constant-time lookup: hash the provided key and match on the blind
        index. Returns the doc regardless of status — the caller decides what
        an expired/scheduled key means for its request."""
        if not provided:
            return None
        return self.keys.find_one({'keyHash': SecurityService.generate_blind_index(provided)})

    def get(self, key_id):
        oid = self._oid(key_id)
        return self.keys.find_one({'_id': oid}) if oid else None

    def list_keys(self):
        return list(self.keys.find().sort('createdAt', -1))

    def reveal(self, doc):
        return SecurityService.decrypt(doc['keyEnc'])

    def expire_now(self, key_id):
        """Manual kill switch: flips status and pins expiredAt/expiresAt to now."""
        oid = self._oid(key_id)
        if not oid:
            return False
        now = _utcnow()
        result = self.keys.update_one(
            {'_id': oid, 'status': 'active'},
            {'$set': {'status': 'expired', 'expiredAt': now, 'expiresAt': now}},
        )
        return result.modified_count > 0

    def update_key(self, key_id, updates):
        """Master edits: rename and/or move the validity window. Reactivating a
        manually-expired key is deliberate — extend it by setting new dates."""
        oid = self._oid(key_id)
        if not oid:
            return None
        allowed = {k: v for k, v in updates.items()
                   if k in ('name', 'startsAt', 'expiresAt', 'status', 'expiredAt')}
        if not allowed:
            return self.keys.find_one({'_id': oid})
        return self.keys.find_one_and_update(
            {'_id': oid}, {'$set': allowed}, return_document=True,
        )

    def delete_key(self, key_id):
        """Hard delete the key and its install ledger."""
        oid = self._oid(key_id)
        if not oid:
            return False
        deleted = self.keys.delete_one({'_id': oid}).deleted_count > 0
        if deleted:
            self.installs.delete_many({'keyRef': str(oid)})
        return deleted

    @staticmethod
    def _oid(key_id):
        try:
            return ObjectId(key_id)
        except (InvalidId, TypeError):
            return None

    # ── Install / session ledger ─────────────────────────────────────────

    @staticmethod
    def key_ref(doc):
        return str(doc['_id'])

    def record_session(self, key_ref, key_name, device_id, labeler_name=None,
                       registration=False, app_version=None):
        """Called from /v1/desktop/ping (once per app start): upsert the
        (key, device) install row and count the session. A ping marked as a
        registration event (the app's register-with-code flow, vs a routine
        start) additionally counts as an installation of the key. The app
        version rides along so OTA rollout progress is visible per device."""
        now = _utcnow()
        update = {
            '$set': {'lastSeenAt': now, 'keyName': key_name},
            '$setOnInsert': {'firstSeenAt': now},
            '$inc': {'sessionCount': 1},
        }
        if registration:
            update['$inc']['registrationCount'] = 1
            update['$set']['lastRegisteredAt'] = now
        if labeler_name:
            update['$set']['labelerName'] = labeler_name
        if app_version:
            update['$set']['appVersion'] = app_version
        self.installs.update_one(
            {'keyRef': key_ref, 'deviceId': device_id}, update, upsert=True,
        )

    def installs_for(self, key_ref):
        return list(self.installs.find({'keyRef': key_ref}).sort('lastSeenAt', -1))

    def install_stats(self):
        """Per-key aggregate for the list view: device count, total sessions,
        distinct labeler names, last activity. Returns {keyRef: stats}."""
        rows = self.installs.aggregate([
            {'$group': {
                '_id': '$keyRef',
                'deviceCount': {'$sum': 1},
                'sessionCount': {'$sum': '$sessionCount'},
                'registrationCount': {'$sum': '$registrationCount'},
                'userNames': {'$addToSet': '$labelerName'},
                'lastSeenAt': {'$max': '$lastSeenAt'},
            }},
        ])
        return {
            row['_id']: {
                'deviceCount': row['deviceCount'],
                'sessionCount': row['sessionCount'],
                'registrationCount': row['registrationCount'],
                'userNames': sorted(n for n in row['userNames'] if n),
                'lastSeenAt': row['lastSeenAt'],
            }
            for row in rows
        }
