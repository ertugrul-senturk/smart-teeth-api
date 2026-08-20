from datetime import datetime, timezone

from app.config.collections import (
    ALLOWED_SYNC_COLLECTIONS,
    IMAGES_COLLECTION,
    IMPORT_LINKS_COLLECTION,
)
from app.models import User
from app.repositories.sync_repository import _jsonify_safe

_NOT_DELETED = {'$or': [{'isDeleted': {'$exists': False}}, {'isDeleted': False}]}


# Every field on a scan record that references a doc in the images
# collection. Image docs carry no back-reference to their record, so a
# record delete must collect these ids first or the images are orphaned.
_RECORD_IMAGE_FIELDS = ('mouthImageId', 'gumImageId', 'gumMaskId')
_TOOTH_IMAGE_FIELDS = ('imageId', 'plaqueMaskId')


def _collect_image_ids(docs):
    ids = []
    for doc in docs:
        for field in _RECORD_IMAGE_FIELDS:
            if doc.get(field):
                ids.append(str(doc[field]))
        for tooth in doc.get('toothImages') or []:
            if isinstance(tooth, dict):
                for field in _TOOTH_IMAGE_FIELDS:
                    if tooth.get(field):
                        ids.append(str(tooth[field]))
    return ids


class PatientRepository:
    """Access to mobile users' data for the desktop (dentist) app: browsing,
    plus permanent deletion (the only write the desktop is allowed here).

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

    # ── Permanent deletion (desktop-initiated) ────────────────────────────

    def delete_records(self, user_id, collection_key, record_ids):
        """Hard-delete selected records of one patient plus every image they
        reference and their import-link provenance rows.

        Matches by client `id` OR Mongo `_id` (the desktop sees `id or _id`),
        and deliberately ignores the isDeleted tombstone filter — a purge
        must remove tombstoned rows too. Returns {records, images, importLinks}.
        """
        from bson import ObjectId

        oids = [ObjectId(r) for r in record_ids if ObjectId.is_valid(r)]
        docs = list(self.db[collection_key].find({
            'userId': user_id,
            '$or': [{'id': {'$in': record_ids}}, {'_id': {'$in': oids}}],
        }))
        if not docs:
            return {'records': 0, 'images': 0, 'importLinks': 0}

        image_oids = [
            ObjectId(i) for i in _collect_image_ids(docs) if ObjectId.is_valid(i)
        ]
        removed_images = 0
        if image_oids:
            removed_images = self.db[IMAGES_COLLECTION].delete_many(
                {'userId': user_id, '_id': {'$in': image_oids}}
            ).deleted_count

        removed_records = self.db[collection_key].delete_many(
            {'_id': {'$in': [d['_id'] for d in docs]}}
        ).deleted_count

        source_ids = list({str(d.get('id') or d['_id']) for d in docs})
        removed_links = self.db[IMPORT_LINKS_COLLECTION].delete_many({
            'sourceUserId': user_id,
            'sourceCollection': collection_key,
            'sourceRecordId': {'$in': source_ids},
        }).deleted_count

        return {
            'records': removed_records,
            'images': removed_images,
            'importLinks': removed_links,
        }

    def purge_patient(self, user_id, delete_account=False):
        """Permanently erase everything a patient synced: all sync-collection
        docs (tombstoned included), their images, and their import links.

        With delete_account, the user document is removed too — including any
        archive copy in deleted_users, so nothing recoverable remains. Returns
        ({collection: count}, account_deleted).
        """
        removed = {}
        for name in ALLOWED_SYNC_COLLECTIONS:
            n = self.db[name].delete_many({'userId': user_id}).deleted_count
            if n:
                removed[name] = n
        n = self.db[IMAGES_COLLECTION].delete_many({'userId': user_id}).deleted_count
        if n:
            removed['images'] = n
        n = self.db[IMPORT_LINKS_COLLECTION].delete_many(
            {'sourceUserId': user_id}
        ).deleted_count
        if n:
            removed['importLinks'] = n

        account_deleted = False
        if delete_account:
            from bson import ObjectId
            oid = ObjectId(user_id)
            account_deleted = self.users.delete_one({'_id': oid}).deleted_count > 0
            # A user who previously self-deleted leaves an archived copy —
            # "permanent" means that goes too.
            self.db['deleted_users'].delete_many({'_id': oid})
        return removed, account_deleted


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
