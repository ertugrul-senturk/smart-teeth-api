from datetime import datetime, timezone

from app.config.collections import (
    ALLOWED_SYNC_COLLECTIONS,
    IMAGES_COLLECTION,
    IMPORT_LINKS_COLLECTION,
)
from app.models import User
from app.repositories.sync_repository import _jsonify_safe

_NOT_DELETED = {'$or': [{'isDeleted': {'$exists': False}}, {'isDeleted': False}]}


class PatientRepository:
    """Read-only access to mobile users' data for the desktop (dentist) app.

    Decryption happens here — names/emails/demographics are stored encrypted
    and are only ever readable through the API layer.
    """

    def __init__(self, db):
        self.db = db
        self.users = db['users']

    def _record_counts_by_user(self):
        """{userId: {collection: count}} across all sync collections."""
        counts = {}
        for name in ALLOWED_SYNC_COLLECTIONS:
            pipeline = [
                {'$match': _NOT_DELETED},
                {'$group': {'_id': '$userId', 'n': {'$sum': 1}}},
            ]
            for row in self.db[name].aggregate(pipeline):
                counts.setdefault(str(row['_id']), {})[name] = row['n']
        return counts

    def list_patients(self):
        counts = self._record_counts_by_user()
        patients = []
        for doc in self.users.find():
            user = User.from_dict(doc)
            user_id = str(user._id)
            patients.append({
                'id': user_id,
                'name': user.name,
                'email': user.email,
                'age': user.age,
                'gender': user.gender,
                'ethnicity': user.ethnicity,
                'has_insurance': user.has_insurance,
                'last_doctor_visit': user.last_doctor_visit,
                'status': user.status,
                'created_at': user.created_at.isoformat() if user.created_at else None,
                'recordCounts': counts.get(user_id, {}),
            })
        return patients

    def get_patient(self, user_id):
        from bson import ObjectId
        from bson.errors import InvalidId
        try:
            oid = ObjectId(user_id)
        except (InvalidId, TypeError):
            return None
        doc = self.users.find_one({'_id': oid})
        return User.from_dict(doc) if doc else None

    def get_records(self, user_id, collection_key, date_from=None, date_to=None):
        """Non-deleted records of one patient, optionally createdAt-bounded."""
        query = {'userId': user_id, **_NOT_DELETED}

        created = {}
        if date_from:
            created['$gte'] = date_from
        if date_to:
            created['$lte'] = date_to
        if created:
            query['createdAt'] = created

        docs = self.db[collection_key].find(query).sort('createdAt', -1)
        return [_jsonify_safe(doc) for doc in docs]


class ImportLinkRepository:
    """Provenance ledger: which mobile records were imported into which
    desktop install, and whether they've been edited there since."""

    def __init__(self, db):
        self.collection = db[IMPORT_LINKS_COLLECTION]

    def register(self, source_collection, source_user_id, source_record_id,
                 device_id, desktop_entry_id, key_name=None):
        now = datetime.now(timezone.utc)
        self.collection.update_one(
            {
                'sourceCollection': source_collection,
                'sourceUserId': source_user_id,
                'sourceRecordId': source_record_id,
                'deviceId': device_id,
            },
            {
                '$set': {
                    'desktopEntryId': desktop_entry_id,
                    'keyName': key_name,
                },
                '$setOnInsert': {
                    'importedAt': now,
                    'lastEditedAt': None,
                },
            },
            upsert=True,
        )
        return self.collection.find_one({
            'sourceCollection': source_collection,
            'sourceUserId': source_user_id,
            'sourceRecordId': source_record_id,
            'deviceId': device_id,
        })

    def links_for_user(self, source_user_id, source_collection=None):
        """{sourceRecordId: [links…]} — a record can be imported by several
        desktop installs, so each id maps to a list."""
        query = {'sourceUserId': source_user_id}
        if source_collection:
            query['sourceCollection'] = source_collection

        by_record = {}
        for link in self.collection.find(query):
            by_record.setdefault(str(link['sourceRecordId']), []).append(link)
        return by_record

    def mark_edited(self, device_id, desktop_entry_id, edited_at):
        """Stamp the edit time on the link owning this desktop entry (if it
        was imported from mobile; no-op otherwise)."""
        self.collection.update_one(
            {'deviceId': device_id, 'desktopEntryId': desktop_entry_id},
            {'$set': {'lastEditedAt': edited_at}},
        )
