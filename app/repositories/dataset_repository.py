"""Shared dataset store — the server-authoritative labeling workspace.

Datasets are shared across every desktop installation: this store is the
single source of truth and desktop clients keep only a rebuildable cache.
Three primitives keep concurrent dentists safe:

- optimistic locking: every dataset/item carries an integer `version`; a
  write must present the version it was based on, and a stale write raises
  ConflictError so the client can show who got there first;
- tombstones: deletes set isDeleted/deletedAt instead of removing, so delta
  polls converge on every client;
- `updatedAt` drives the /changes delta feed.

Images live in a content-addressed asset collection (one doc per sha256)
and masks in per-item docs, so no single document approaches Mongo's 16MB
cap. Assets are shared between datasets and never garbage-collected — a
dataset snapshot must stay intact even if its source scan or patient is
deleted.
"""

from datetime import datetime, timezone

from pymongo import ReturnDocument

from app.config.collections import (
    DATASETS_COLLECTION,
    DATASET_ITEMS_COLLECTION,
    DATASET_ASSETS_COLLECTION,
    DATASET_MASKS_COLLECTION,
    DATASET_EVENTS_COLLECTION,
    DATASET_EXPORTS_COLLECTION,
)
from app.repositories.sync_repository import _jsonify_safe


class ConflictError(Exception):
    """A versioned write lost the race. `current` is the doc that won —
    controllers surface its version/updatedBy so the UI can name the other
    dentist and offer reload-or-overwrite."""

    def __init__(self, current):
        self.current = current
        super().__init__('Version conflict')


def _now():
    return datetime.now(timezone.utc)


# Item list/delta responses omit the immutable AI snapshot — it can be large
# (per-tooth arrays) and is only needed when an item is opened for labeling.
_ITEM_LIST_PROJECTION = {'aiJson': 0}


class DatasetRepository:

    def __init__(self, db):
        self.datasets = db[DATASETS_COLLECTION]
        self.items = db[DATASET_ITEMS_COLLECTION]
        self.assets = db[DATASET_ASSETS_COLLECTION]
        self.masks = db[DATASET_MASKS_COLLECTION]
        self.events = db[DATASET_EVENTS_COLLECTION]
        self.exports = db[DATASET_EXPORTS_COLLECTION]

    # ── versioned writes ──────────────────────────────────────────────────

    def _versioned_update(self, collection, doc_id, base_version, update, actor):
        """Apply `update` only if the doc is live and still at base_version;
        bump version + updatedAt/updatedBy atomically. Raises LookupError if
        the doc is missing/tombstoned, ConflictError if someone else won."""
        update = dict(update)
        sets = dict(update.get('$set') or {})
        sets['updatedAt'] = _now()
        sets['updatedBy'] = actor
        update['$set'] = sets
        update['$inc'] = {'version': 1}

        result = collection.find_one_and_update(
            {'_id': doc_id, 'version': base_version, 'isDeleted': False},
            update,
            return_document=ReturnDocument.AFTER,
        )
        if result is not None:
            return result

        current = collection.find_one({'_id': doc_id})
        if current is None or current.get('isDeleted'):
            raise LookupError('Not found')
        raise ConflictError(current)

    # ── datasets ──────────────────────────────────────────────────────────

    def create_dataset(self, dataset_id, name, description, tasks, actor):
        now = _now()
        doc = {
            '_id': dataset_id,
            'name': name,
            'description': description,
            'tasks': tasks,
            'version': 1,
            'createdAt': now,
            'createdBy': actor,
            'updatedAt': now,
            'updatedBy': actor,
            'isDeleted': False,
            'deletedAt': None,
        }
        self.datasets.insert_one(doc)
        return doc

    def get_dataset(self, dataset_id):
        doc = self.datasets.find_one({'_id': dataset_id, 'isDeleted': False})
        return doc

    def list_datasets(self):
        docs = list(self.datasets.find({'isDeleted': False}).sort('createdAt', -1))
        self._attach_counts(docs)
        return docs

    def search_datasets(self, search=None, page=1, page_size=50):
        """One page of datasets matching `search` (case-insensitive substring
        of name or description), newest first, plus the total match count.
        Status counts are aggregated only for the returned page."""
        import re

        query = {'isDeleted': False}
        if search and search.strip():
            pattern = re.compile(re.escape(search.strip()), re.IGNORECASE)
            query['$or'] = [{'name': pattern}, {'description': pattern}]

        total = self.datasets.count_documents(query)
        docs = list(
            self.datasets.find(query)
            .sort('createdAt', -1)
            .skip(max(page - 1, 0) * page_size)
            .limit(page_size)
        )
        self._attach_counts(docs)
        return docs, total

    def _attach_counts(self, docs):
        counts = self._status_counts([d['_id'] for d in docs])
        for doc in docs:
            doc['counts'] = counts.get(doc['_id'], {})
            doc['imageCount'] = sum(doc['counts'].values())

    def _status_counts(self, dataset_ids):
        if not dataset_ids:
            return {}
        rows = self.items.aggregate([
            {'$match': {'datasetId': {'$in': dataset_ids}, 'isDeleted': False}},
            {'$group': {
                '_id': {'datasetId': '$datasetId', 'status': '$status'},
                'n': {'$sum': 1},
            }},
        ])
        counts = {}
        for row in rows:
            key = row['_id']['datasetId']
            counts.setdefault(key, {})[row['_id']['status']] = row['n']
        return counts

    def update_dataset(self, dataset_id, base_version, fields, actor):
        return self._versioned_update(
            self.datasets, dataset_id, base_version, {'$set': fields}, actor
        )

    def delete_dataset(self, dataset_id, actor):
        """Tombstone the dataset. Items are left in place (hidden by the
        dataset tombstone) so their assets stay explainable in usage lookups
        and a future undelete stays possible."""
        result = self.datasets.find_one_and_update(
            {'_id': dataset_id, 'isDeleted': False},
            {
                '$set': {
                    'isDeleted': True,
                    'deletedAt': _now(),
                    'updatedAt': _now(),
                    'updatedBy': actor,
                },
                '$inc': {'version': 1},
            },
        )
        if result is None:
            raise LookupError('Not found')

    # ── delta feed ────────────────────────────────────────────────────────

    def changes(self, since):
        """Everything that changed after `since` (tombstones included), plus
        a cursor for the next poll. The cursor is captured *before* querying,
        so a write racing the query is re-sent next poll rather than lost;
        clients must apply deltas idempotently (upsert by id, keep the higher
        version)."""
        cursor = _now()
        if since is None:
            dataset_filter = {'isDeleted': False}
            item_filter = {'isDeleted': False}
        else:
            dataset_filter = {'updatedAt': {'$gt': since}}
            item_filter = {'updatedAt': {'$gt': since}}

        datasets = list(self.datasets.find(dataset_filter))
        items = list(self.items.find(item_filter, _ITEM_LIST_PROJECTION))
        return cursor, datasets, items

    # ── usage lookups (builder badges) ────────────────────────────────────

    def usage(self, asset_hashes, mobile_record_ids):
        """Which datasets already contain these images? Keyed by asset hash
        for local files (the client hashes before upload) and by mobile
        record id so mobile rows can be badged without downloading bytes."""
        clauses = []
        if asset_hashes:
            clauses.append({'assetHash': {'$in': asset_hashes}})
        if mobile_record_ids:
            clauses.append({'sourceRef.sourceRecordId': {'$in': mobile_record_ids}})
        if not clauses:
            return {}, {}

        items = self.items.find(
            {'$or': clauses, 'isDeleted': False},
            {'assetHash': 1, 'datasetId': 1, 'sourceRef.sourceRecordId': 1},
        )

        dataset_names = {
            d['_id']: d['name']
            for d in self.datasets.find({'isDeleted': False}, {'name': 1})
        }

        by_hash = {}
        by_record = {}
        wanted_hashes = set(asset_hashes or [])
        wanted_records = set(mobile_record_ids or [])
        for item in items:
            if item['datasetId'] not in dataset_names:
                continue  # item of a tombstoned dataset
            ref = {'datasetId': item['datasetId'],
                   'datasetName': dataset_names[item['datasetId']]}
            if item.get('assetHash') in wanted_hashes:
                by_hash.setdefault(item['assetHash'], []).append(ref)
            record_id = (item.get('sourceRef') or {}).get('sourceRecordId')
            if record_id in wanted_records:
                by_record.setdefault(record_id, []).append(ref)
        return by_hash, by_record

    # ── assets ────────────────────────────────────────────────────────────

    def missing_assets(self, hashes):
        if not hashes:
            return []
        present = {
            doc['_id']
            for doc in self.assets.find({'_id': {'$in': hashes}}, {'_id': 1})
        }
        return [h for h in hashes if h not in present]

    def upsert_asset(self, asset, actor):
        """Insert-if-absent: assets are immutable by content address, so a
        re-upload of an existing hash is a no-op."""
        self.assets.update_one(
            {'_id': asset['hash']},
            {'$setOnInsert': {
                'base64Data': asset['base64'],
                'mimeType': asset['mimeType'],
                'filename': asset.get('filename'),
                'width': asset.get('width'),
                'height': asset.get('height'),
                'sourceType': asset.get('sourceType'),
                'createdAt': _now(),
                'createdBy': actor,
            }},
            upsert=True,
        )

    def merge_asset_meta(self, asset_hash, demographics, clinical_context):
        """Patient metadata rides on the asset (like the SQLite store) and is
        merged, never blanked — a later add without metadata must not erase
        what an earlier add captured."""
        sets = {}
        if demographics is not None:
            sets['demographics'] = demographics
        if clinical_context is not None:
            sets['clinicalContext'] = clinical_context
        if sets:
            self.assets.update_one({'_id': asset_hash}, {'$set': sets})

    def get_asset(self, asset_hash):
        return self.assets.find_one({'_id': asset_hash})

    def get_asset_meta(self, asset_hash):
        """Asset metadata without the image bytes — for item detail joins."""
        return self.assets.find_one({'_id': asset_hash}, {'base64Data': 0})

    def assets_demographics(self, hashes):
        """hash → demographics for a batch of assets (item list joins)."""
        if not hashes:
            return {}
        return {
            doc['_id']: doc.get('demographics')
            for doc in self.assets.find(
                {'_id': {'$in': list(set(hashes))}}, {'demographics': 1},
            )
        }

    # ── items ─────────────────────────────────────────────────────────────

    def find_item_by_asset(self, dataset_id, asset_hash):
        return self.items.find_one(
            {'datasetId': dataset_id, 'assetHash': asset_hash}
        )

    def insert_item(self, doc):
        self.items.insert_one(doc)

    def revive_item(self, item_id, doc):
        """Re-adding an image whose item was removed: reuse the tombstoned
        item's id (the (datasetId, assetHash) unique index points at it) and
        replace its content with the fresh snapshot."""
        doc = dict(doc)
        previous = self.items.find_one({'_id': item_id}, {'version': 1})
        doc['_id'] = item_id
        doc['version'] = (previous or {}).get('version', 0) + 1
        self.items.replace_one({'_id': item_id}, doc)

    def list_items(self, dataset_id):
        return list(
            self.items
            .find({'datasetId': dataset_id, 'isDeleted': False}, _ITEM_LIST_PROJECTION)
            .sort('addedAt', 1)
        )

    def get_item(self, item_id):
        doc = self.items.find_one({'_id': item_id, 'isDeleted': False})
        return doc

    def save_labels(self, item_id, base_version, image_json, actor):
        """Versioned working-copy overwrite. Uses a pipeline update because
        the status transition is conditional: a first edit moves unlabeled →
        in_progress, but a re-edit never demotes labeled/approved (same rule
        as the old SQLite store). $literal guards client JSON from being
        evaluated as aggregation expressions."""
        result = self.items.find_one_and_update(
            {'_id': item_id, 'version': base_version, 'isDeleted': False},
            [{'$set': {
                'imageJson': {'$literal': image_json},
                'status': {'$cond': [
                    {'$eq': ['$status', 'unlabeled']}, 'in_progress', '$status'
                ]},
                'updatedAt': '$$NOW',
                'updatedBy': {'$literal': actor},
                'version': {'$add': ['$version', 1]},
            }}],
            return_document=ReturnDocument.AFTER,
        )
        if result is not None:
            return result

        current = self.items.find_one({'_id': item_id})
        if current is None or current.get('isDeleted'):
            raise LookupError('Not found')
        raise ConflictError(current)

    def patch_item(self, item_id, base_version, fields, actor):
        return self._versioned_update(
            self.items, item_id, base_version, {'$set': fields}, actor
        )

    def delete_item(self, item_id, actor):
        result = self.items.find_one_and_update(
            {'_id': item_id, 'isDeleted': False},
            {
                '$set': {
                    'isDeleted': True,
                    'deletedAt': _now(),
                    'updatedAt': _now(),
                    'updatedBy': actor,
                },
                '$inc': {'version': 1},
            },
        )
        if result is None:
            raise LookupError('Not found')
        # Mask bytes are dead weight once the item is tombstoned; a revive
        # re-uploads them from the client.
        self.masks.delete_many({'itemId': item_id})

    # ── masks ─────────────────────────────────────────────────────────────

    def put_masks(self, item_id, kinds_to_base64, actor):
        now = _now()
        for kind, data in kinds_to_base64.items():
            self.masks.update_one(
                {'_id': f'{item_id}:{kind}'},
                {'$set': {
                    'itemId': item_id,
                    'kind': kind,
                    'base64Data': data,
                    'updatedAt': now,
                    'updatedBy': actor,
                }},
                upsert=True,
            )

    def get_masks(self, item_id):
        return {
            doc['kind']: doc['base64Data']
            for doc in self.masks.find({'itemId': item_id})
        }

    # ── export ledger ─────────────────────────────────────────────────────
    # Export versions are shared: v3 means the same thing on every install.

    def record_export(self, dataset_id, item_count, filters, actor):
        latest = self.exports.find_one(
            {'datasetId': dataset_id}, sort=[('version', -1)],
        )
        version = (latest or {}).get('version', 0) + 1
        self.exports.insert_one({
            'datasetId': dataset_id,
            'version': version,
            'itemCount': item_count,
            'filters': filters or {},
            'createdAt': _now(),
            'createdBy': actor,
        })
        return version

    def list_exports(self, dataset_id):
        return [
            _jsonify_safe({k: v for k, v in doc.items() if k != '_id'})
            for doc in self.exports.find({'datasetId': dataset_id}).sort('version', -1)
        ]

    # ── label events ──────────────────────────────────────────────────────

    def log_event(self, item_id, task, source, labeled_by, detail=None):
        self.events.insert_one({
            'itemId': item_id,
            'task': task,
            'source': source,
            'labeledBy': labeled_by,
            'detail': detail or {},
            'createdAt': _now(),
        })

    def item_events(self, item_id):
        return [
            _jsonify_safe({k: v for k, v in doc.items() if k != '_id'})
            for doc in self.events.find({'itemId': item_id}).sort('createdAt', 1)
        ]
