from bson import ObjectId
from bson.errors import InvalidId
from datetime import datetime, timezone
import base64


def _jsonify_safe(value):
    """Recursively convert BSON-only types so jsonify never chokes on a doc."""
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonify_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonify_safe(v) for v in value]
    return value


def _parse_client_ts(value):
    """Client-sent updatedAt (ISO string) → aware UTC datetime, or None."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _as_aware(dt):
    """Mongo returns naive UTC datetimes — normalize for comparison."""
    if isinstance(dt, datetime):
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _coerce_ids(ids):
    """Ids may be ObjectId strings or plain client strings — match both."""
    coerced = []
    for oid in ids:
        try:
            coerced.append(ObjectId(oid))
        except (InvalidId, TypeError, KeyError):
            coerced.append(oid)
    return coerced


class SyncRepository:

    def __init__(self, db):
        self.db = db

    def find_existing_ids(self, user_id, collection_key, object_ids):
        collection = self.db[collection_key]
        valid_object_ids = _coerce_ids(object_ids)

        query = {
            'userId': user_id,
            'id': {'$in': valid_object_ids},
            '$or': [
                {'isDeleted': {'$exists': False}},
                {'isDeleted': False}
            ]
        }

        existing_docs = collection.find(query, {'id': 1})
        return {str(doc['id']) for doc in existing_docs}

    def find_deleted_ids(self, user_id, collection_key, client_ids):
        """Of the ids the client still holds, which are tombstoned here?
        Returned to the client so it purges them instead of re-uploading."""
        if not client_ids:
            return []
        collection = self.db[collection_key]
        docs = collection.find(
            {
                'userId': user_id,
                'id': {'$in': _coerce_ids(client_ids)},
                'isDeleted': True,
            },
            {'id': 1},
        )
        return [str(doc['id']) for doc in docs]

    def find_missing_data(self, user_id, collection_key, existing_ids_on_client):
        collection = self.db[collection_key]

        client_object_ids = _coerce_ids(existing_ids_on_client)

        query = {
            'userId': user_id,
            'id': {'$nin': client_object_ids},
            '$or': [
                {'isDeleted': {'$exists': False}},
                {'isDeleted': False}
            ]
        }

        return [_jsonify_safe(doc) for doc in collection.find(query)]

    def bulk_insert(self, user_id, collection_key, documents):
        """Upsert documents keyed on (userId, id).

        Re-sent documents (e.g. a plan whose goals were toggled) update the
        existing record instead of duplicating it. The client-side `_id` is
        never trusted — Mongo assigns one on insert.

        Two guards protect existing server state:
        - tombstones: a document deleted on the server is never revived by a
          client that still holds a copy (its id lands in `skipped`);
        - staleness: when both sides carry a client edit timestamp, an
          incoming write older than the stored one is dropped, so a device
          that syncs late can't overwrite a newer edit.

        Returns (inserted_ids, skipped_ids). Skipped ids matter to clients:
        they must clear those records from their retry/dirty queues or they
        would re-send them forever.
        """
        collection = self.db[collection_key]

        if not documents:
            return [], []

        # One round-trip for the state of every incoming id.
        incoming_ids = [doc.get('id') for doc in documents if doc.get('id')]
        server_state = {}
        if incoming_ids:
            for existing in collection.find(
                {'userId': user_id, 'id': {'$in': _coerce_ids(incoming_ids)}},
                {'id': 1, 'isDeleted': 1, 'clientUpdatedAt': 1},
            ):
                server_state[str(existing['id'])] = existing

        now = datetime.now(timezone.utc)
        inserted_ids = []
        skipped_ids = []
        for doc in documents:
            doc.pop('_id', None)
            client_created = doc.pop('createdAt', None)
            client_ts = _parse_client_ts(doc.get('updatedAt'))
            doc['userId'] = user_id
            doc['updatedAt'] = now
            doc['isDeleted'] = False
            if client_ts:
                doc['clientUpdatedAt'] = client_ts

            doc_id = doc.get('id')
            if not doc_id:
                # Legacy payloads without a client id can't be reconciled;
                # store them as-is so nothing is lost.
                doc['createdAt'] = client_created or now
                result = collection.insert_one(doc)
                inserted_ids.append(str(result.inserted_id))
                continue

            existing = server_state.get(str(doc_id))
            if existing:
                if existing.get('isDeleted'):
                    skipped_ids.append(str(doc_id))
                    continue
                existing_ts = _as_aware(existing.get('clientUpdatedAt'))
                if client_ts and existing_ts and client_ts < existing_ts:
                    skipped_ids.append(str(doc_id))
                    continue

            collection.update_one(
                {'userId': user_id, 'id': doc_id},
                {
                    '$set': doc,
                    '$setOnInsert': {
                        'createdAt': client_created or now
                    },
                },
                upsert=True
            )
            inserted_ids.append(str(doc_id))

        return inserted_ids, skipped_ids

    def purge_user_data(self, user_id):
        """Hard-delete every sync record and image belonging to a user.
        Called on account deletion so no orphaned medical data lingers after
        the owning account is gone."""
        from app.config.collections import ALLOWED_SYNC_COLLECTIONS, IMAGES_COLLECTION

        removed = {}
        for name in ALLOWED_SYNC_COLLECTIONS:
            result = self.db[name].delete_many({'userId': user_id})
            if result.deleted_count:
                removed[name] = result.deleted_count
        result = self.db[IMAGES_COLLECTION].delete_many({'userId': user_id})
        if result.deleted_count:
            removed[IMAGES_COLLECTION] = result.deleted_count
        return removed

    def mark_as_deleted(self, user_id, collection_key, object_ids):
        collection = self.db[collection_key]

        valid_object_ids = _coerce_ids(object_ids)

        result = collection.update_many(
            {
                'id': {'$in': valid_object_ids},
                'userId': user_id
            },
            {
                '$set': {
                    'isDeleted': True,
                    'deletedAt': datetime.now(timezone.utc),
                    'updatedAt': datetime.now(timezone.utc)
                }
            }
        )

        return result.modified_count


class ImageRepository:
    COLLECTION_NAME = 'images'

    def __init__(self, db):
        self.db = db
        self.collection = db[self.COLLECTION_NAME]

    def store_image(self, user_id, image_data):
        if not image_data:
            return None
        doc = {
            'userId': user_id,
            'base64Data': image_data.get('base64', ''),
            'mimeType': image_data.get('mimeType', 'image/jpeg'),
            'createdAt': datetime.now(timezone.utc)
        }
        result = self.collection.insert_one(doc)
        return str(result.inserted_id)

    def store_images_binary(self, user_id, images_binary):
        if not images_binary:
            return []

        image_ids = []
        for img in images_binary:
            base64_data = base64.b64encode(img['data']).decode('utf-8')

            doc = {
                'userId': user_id,
                'filename': img.get('filename', ''),
                'base64Data': base64_data,
                'mimeType': img.get('mimeType', 'image/jpeg'),
                'size': len(img['data']),
                'createdAt': datetime.now(timezone.utc)
            }

            result = self.collection.insert_one(doc)
            image_ids.append(str(result.inserted_id))

        return image_ids

    def get_images_by_ids(self, user_id, image_ids):
        if not image_ids:
            return []

        object_ids = []
        for img_id in image_ids:
            try:
                object_ids.append(ObjectId(img_id))
            except (InvalidId, TypeError, KeyError):
                object_ids.append(img_id)

        query = {
            'userId': user_id,
            '_id': {'$in': object_ids}
        }

        docs = list(self.collection.find(query))

        result = []
        for doc in docs:
            result.append({
                '_id': str(doc['_id']),
                'base64': doc.get('base64Data', ''),
                'mimeType': doc.get('mimeType', 'image/jpeg'),
                'filename': doc.get('filename', ''),
                'size': doc.get('size', 0),
                'createdAt': doc.get('createdAt', '').isoformat() if doc.get('createdAt') else None
            })

        return result
