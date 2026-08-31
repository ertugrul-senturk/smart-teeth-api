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

    def _record_counts_by_user(self, user_ids=None):
        """{userId: {collection: count}} across all sync collections.
        With user_ids, counts only those users (one page instead of the
        whole fleet)."""
        match = dict(_NOT_DELETED)
        if user_ids is not None:
            if not user_ids:
                return {}
            match = {'$and': [{'userId': {'$in': list(user_ids)}}, _NOT_DELETED]}
        counts = {}
        for name in ALLOWED_SYNC_COLLECTIONS:
            pipeline = [
                {'$match': match},
                {'$group': {'_id': '$userId', 'n': {'$sum': 1}}},
            ]
            for row in self.db[name].aggregate(pipeline):
                counts.setdefault(str(row['_id']), {})[name] = row['n']
        return counts

    def _patient_dict(self, user, counts):
        user_id = str(user._id)
        return {
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
        }

    def list_patients(self):
        counts = self._record_counts_by_user()
        return [
            self._patient_dict(User.from_dict(doc), counts)
            for doc in self.users.find()
        ]

    def search_patients(self, search=None, page=1, page_size=50):
        """One name-sorted page of patients matching `search` (case-insensitive
        substring of name or email), plus the total match count.

        Names and emails are encrypted at rest (only exact-match blind indexes
        exist), so matching decrypts just those two fields per user — the full
        profile blob and the record counts are only resolved for the returned
        page. Returns (patients, total).
        """
        from app.services.security_service import SecurityService

        def _decrypt(value):
            if not value:
                return ''
            try:
                return SecurityService.decrypt(value) or ''
            except Exception:
                return ''

        query = (search or '').strip().lower()
        rows = []
        for doc in self.users.find({}, {'n': 1, 'e': 1}):
            name = _decrypt(doc.get('n'))
            email = _decrypt(doc.get('e'))
            if query and query not in name.lower() and query not in email.lower():
                continue
            rows.append((doc['_id'], name))
        rows.sort(key=lambda r: r[1].lower())

        total = len(rows)
        start = max(page - 1, 0) * page_size
        page_ids = [oid for oid, _ in rows[start:start + page_size]]
        if not page_ids:
            return [], total

        docs = {d['_id']: d for d in self.users.find({'_id': {'$in': page_ids}})}
        counts = self._record_counts_by_user([str(oid) for oid in page_ids])
        patients = [
            self._patient_dict(User.from_dict(docs[oid]), counts)
            for oid in page_ids if oid in docs
        ]
        return patients, total

    def get_patient(self, user_id):
        from bson import ObjectId
        from bson.errors import InvalidId
        try:
            oid = ObjectId(user_id)
        except (InvalidId, TypeError):
            return None
        doc = self.users.find_one({'_id': oid})
        return User.from_dict(doc) if doc else None

    @staticmethod
    def _records_query(user_id, date_from=None, date_to=None):
        query = {'userId': user_id, **_NOT_DELETED}
        created = {}
        if date_from:
            created['$gte'] = date_from
        if date_to:
            created['$lte'] = date_to
        if created:
            query['createdAt'] = created
        return query

    def get_records(self, user_id, collection_key, date_from=None, date_to=None,
                    skip=None, limit=None):
        """Non-deleted records of one patient, optionally createdAt-bounded
        and skip/limit-windowed (newest first)."""
        query = self._records_query(user_id, date_from, date_to)
        cursor = self.db[collection_key].find(query).sort('createdAt', -1)
        if skip:
            cursor = cursor.skip(skip)
        if limit:
            cursor = cursor.limit(limit)
        return [_jsonify_safe(doc) for doc in cursor]

    def count_records(self, user_id, collection_key, date_from=None, date_to=None):
        query = self._records_query(user_id, date_from, date_to)
        return self.db[collection_key].count_documents(query)

    # Trend rows deliberately skip the heavyweight per-tooth arrays — only the
    # fields the desktop's progress sparklines read. toothImages is projected
    # to the three label fields so summaries can be derived for old records
    # that never synced one.
    _TREND_PROJECTION = {
        'id': 1, 'timestamp': 1, 'createdAt': 1, 'detectionCount': 1,
        'summary': 1, 'plaqueSummary': 1, 'gingivitisSummary': 1,
        'toothImages.cavityLabel': 1,
        'toothImages.plaqueLevel': 1,
        'toothImages.plaqueCoverage': 1,
    }

    @staticmethod
    def _derived_cavity_summary(doc):
        summary = doc.get('summary')
        if isinstance(summary, dict) and isinstance(summary.get('total'), (int, float)):
            return summary
        teeth = [t for t in doc.get('toothImages') or [] if isinstance(t, dict)]
        if not teeth:
            return None
        labels = [t.get('cavityLabel') for t in teeth]
        return {
            'total': len(teeth),
            'healthy': labels.count('healthy'),
            'level1': labels.count('level_1'),
            'level2': labels.count('level_2'),
        }

    @staticmethod
    def _derived_plaque_summary(doc):
        summary = doc.get('plaqueSummary')
        if isinstance(summary, dict) and isinstance(summary.get('total'), (int, float)):
            return summary
        teeth = [
            t for t in doc.get('toothImages') or []
            if isinstance(t, dict) and t.get('plaqueLevel') is not None
        ]
        if not teeth:
            return None
        covered = [t['plaqueCoverage'] for t in teeth
                   if isinstance(t.get('plaqueCoverage'), (int, float))]
        levels = [t.get('plaqueLevel') for t in teeth]
        return {
            'total': len(teeth),
            'healthy': levels.count('healthy'),
            'mild': levels.count('mild'),
            'moderate': levels.count('moderate'),
            'severe': levels.count('severe'),
            'avgCoverage': (sum(covered) * 100 / len(covered)) if covered else 0,
        }

    def get_trend(self, user_id, collection_key):
        """Lightweight metric rows for EVERY record of the patient (the
        progress trend always spans the whole history, regardless of the
        date filter or page shown)."""
        docs = self.db[collection_key].find(
            self._records_query(user_id), self._TREND_PROJECTION,
        ).sort('createdAt', 1)
        rows = []
        for doc in docs:
            row = {
                'id': str(doc.get('id') or doc.get('_id')),
                'timestamp': doc.get('timestamp'),
                'detectionCount': doc.get('detectionCount'),
                'summary': self._derived_cavity_summary(doc),
                'plaqueSummary': self._derived_plaque_summary(doc),
                'gingivitisSummary': doc.get('gingivitisSummary'),
            }
            rows.append(_jsonify_safe(row))
        return rows

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
