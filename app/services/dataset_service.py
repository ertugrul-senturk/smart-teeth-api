"""Validation and composition for the shared dataset workspace.

Sits between the /v1/desktop/datasets controller and DatasetRepository:
everything client-shaped is validated here (ids, hashes, statuses, mask
payloads, image sizes), and multi-step operations (add item = asset check +
insert/revive + masks + provenance event + metadata merge) are composed here
so the controller stays HTTP-only.
"""

import base64
import binascii
import hashlib
import re
import uuid
from datetime import datetime, timezone

from pymongo.errors import DuplicateKeyError

from app.repositories.dataset_repository import DatasetRepository, ConflictError
from app.repositories.sync_repository import _jsonify_safe, _parse_client_ts
from app.services.sync_service import check_image_size

DATASET_TASKS = ('cavity', 'plaque', 'gingivitis')
ITEM_STATUSES = ('unlabeled', 'in_progress', 'labeled', 'approved', 'excluded')
SUBJECTS = ('parent', 'child', 'unknown')

# Demographics ride on the asset. `mobile_profile` is snapshotted from the
# patient's account; `manual` is typed in by a dentist for local scans.
DEMOGRAPHIC_SOURCES = ('mobile_profile', 'manual')
_DEMOGRAPHIC_STR_FIELDS = ('ageBand', 'gender', 'ethnicity', 'lastDentalVisit')

# Client-generated ids travel in URLs and mask keys — keep them boring.
_ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
_HASH_RE = re.compile(r'[0-9a-f]{64}$')

# Usage lookups arrive as id lists from the builder; cap them so a single
# request can't turn into an unbounded $in.
_MAX_USAGE_IDS = 5000


def _require_id(value, label):
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ValueError(f'Invalid {label}')
    return value


def _require_hash(value):
    if not isinstance(value, str) or not _HASH_RE.match(value):
        raise ValueError('Invalid asset hash (expected lowercase sha256 hex)')
    return value


def _clean_demographics(value, actor):
    """Validate a client demographics object down to the known shape. Values
    stay the mobile app's raw enum codes (never display labels); manual
    entries are stamped with who typed them so provenance survives edits."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError('demographics must be an object')
    source = value.get('source')
    if source not in DEMOGRAPHIC_SOURCES:
        raise ValueError(
            f"demographics.source must be one of: {', '.join(DEMOGRAPHIC_SOURCES)}"
        )
    out = {'source': source}
    for field in _DEMOGRAPHIC_STR_FIELDS:
        v = value.get(field)
        if v is not None and not isinstance(v, str):
            raise ValueError(f'demographics.{field} must be a string')
        out[field] = (v.strip()[:64] or None) if isinstance(v, str) else None
    insurance = value.get('hasInsurance')
    if insurance is not None and not isinstance(insurance, bool):
        raise ValueError('demographics.hasInsurance must be a boolean')
    out['hasInsurance'] = insurance
    if source == 'manual':
        out['enteredBy'] = actor
        out['enteredAt'] = datetime.now(timezone.utc)
    return out


def conflict_payload(current):
    """What the client needs to render 'Dr. X got there first'."""
    return _jsonify_safe({
        'version': current.get('version'),
        'updatedBy': current.get('updatedBy'),
        'updatedAt': current.get('updatedAt'),
        'status': current.get('status'),
    })


def _shape_dataset(doc):
    out = {
        'id': doc['_id'],
        'name': doc.get('name'),
        'description': doc.get('description'),
        'tasks': doc.get('tasks') or [],
        'version': doc.get('version'),
        'createdAt': doc.get('createdAt'),
        'createdBy': doc.get('createdBy'),
        'updatedAt': doc.get('updatedAt'),
        'updatedBy': doc.get('updatedBy'),
        'isDeleted': bool(doc.get('isDeleted')),
    }
    if 'counts' in doc:
        out['counts'] = doc['counts']
        out['imageCount'] = doc['imageCount']
    return _jsonify_safe(out)


def _shape_item(doc, include_ai=False):
    out = {
        'id': doc['_id'],
        'datasetId': doc.get('datasetId'),
        'assetHash': doc.get('assetHash'),
        'sourceType': doc.get('sourceType'),
        'sourceRef': doc.get('sourceRef'),
        'subject': doc.get('subject'),
        'status': doc.get('status'),
        'excludeReason': doc.get('excludeReason'),
        'image': doc.get('imageJson'),
        'version': doc.get('version'),
        'addedAt': doc.get('addedAt'),
        'addedBy': doc.get('addedBy'),
        'updatedAt': doc.get('updatedAt'),
        'updatedBy': doc.get('updatedBy'),
        'isDeleted': bool(doc.get('isDeleted')),
    }
    if include_ai:
        out['ai'] = doc.get('aiJson')
    return _jsonify_safe(out)


def _mask_kinds(masks, include_ai):
    """Client mask payload → storage kinds. Kinds are constructed here, never
    taken from the client, so arbitrary strings can't land in mask ids."""
    if masks is None:
        return {}
    if not isinstance(masks, dict):
        raise ValueError('masks must be an object')

    kinds = {}

    def take(value, kind):
        if value is None:
            return
        if not isinstance(value, str):
            raise ValueError(f'mask {kind} must be a base64 string')
        check_image_size(value)
        kinds[kind] = value

    take(masks.get('gingivitis'), 'gingivitis')
    if include_ai:
        take(masks.get('gingivitisAi'), 'gingivitis_ai')

    for field, suffix in (('plaque', ''), ('plaqueAi', '_ai')):
        if suffix and not include_ai:
            continue
        per_tooth = masks.get(field)
        if per_tooth is None:
            continue
        if not isinstance(per_tooth, dict):
            raise ValueError(f'masks.{field} must be an object keyed by tooth index')
        for idx, data in per_tooth.items():
            if not re.fullmatch(r'\d{1,3}', str(idx)):
                raise ValueError(f'Invalid tooth index in masks.{field}')
            take(data, f'plaque_{idx}{suffix}')

    return kinds


class DatasetService:

    def __init__(self, db):
        self.repo = DatasetRepository(db)

    # ── datasets ──────────────────────────────────────────────────────────

    def create_dataset(self, name, description, tasks, actor):
        name = (name or '').strip()
        if not name:
            raise ValueError('name is required')
        if not isinstance(tasks, list) or not tasks:
            raise ValueError('tasks must be a non-empty list')
        bad = [t for t in tasks if t not in DATASET_TASKS]
        if bad:
            raise ValueError(f"Unknown tasks: {', '.join(map(str, bad))}")

        doc = self.repo.create_dataset(
            str(uuid.uuid4()), name, (description or '').strip(),
            sorted(set(tasks), key=DATASET_TASKS.index), actor,
        )
        doc['counts'] = {}
        doc['imageCount'] = 0
        return _shape_dataset(doc)

    def list_datasets(self):
        return [_shape_dataset(doc) for doc in self.repo.list_datasets()]

    def update_dataset(self, dataset_id, base_version, data, actor):
        _require_id(dataset_id, 'dataset id')
        fields = {}
        if 'name' in data:
            name = (data['name'] or '').strip()
            if not name:
                raise ValueError('name cannot be empty')
            fields['name'] = name
        if 'description' in data:
            fields['description'] = (data['description'] or '').strip()
        if 'tasks' in data:
            tasks = data['tasks']
            if not isinstance(tasks, list) or not tasks:
                raise ValueError('tasks must be a non-empty list')
            if any(t not in DATASET_TASKS for t in tasks):
                raise ValueError('Unknown task')
            fields['tasks'] = sorted(set(tasks), key=DATASET_TASKS.index)
        if not fields:
            raise ValueError('Nothing to update')

        doc = self.repo.update_dataset(dataset_id, _require_version(base_version),
                                       fields, actor)
        return _shape_dataset(doc)

    def delete_dataset(self, dataset_id, actor):
        _require_id(dataset_id, 'dataset id')
        self.repo.delete_dataset(dataset_id, actor)

    # ── delta feed ────────────────────────────────────────────────────────

    def changes(self, since_raw):
        since = None
        if since_raw:
            since = _parse_client_ts(since_raw)
            if since is None:
                raise ValueError(f"Invalid 'since' cursor: {since_raw!r}")
        cursor, datasets, items = self.repo.changes(since)
        return {
            # 'Z', not '+00:00': the cursor round-trips through a query
            # string, where '+' decodes to a space and breaks parsing.
            'now': cursor.isoformat().replace('+00:00', 'Z'),
            'datasets': [_shape_dataset(d) for d in datasets],
            'items': [_shape_item(i) for i in items],
        }

    # ── usage (builder badges) ────────────────────────────────────────────

    def usage(self, asset_hashes, mobile_record_ids):
        asset_hashes = asset_hashes or []
        mobile_record_ids = mobile_record_ids or []
        if not isinstance(asset_hashes, list) or not isinstance(mobile_record_ids, list):
            raise ValueError('assetHashes and mobileRecordIds must be lists')
        if len(asset_hashes) + len(mobile_record_ids) > _MAX_USAGE_IDS:
            raise ValueError(f'Too many ids (max {_MAX_USAGE_IDS} per request)')
        asset_hashes = [h for h in asset_hashes if isinstance(h, str)]
        mobile_record_ids = [r for r in mobile_record_ids if isinstance(r, str)]

        by_hash, by_record = self.repo.usage(asset_hashes, mobile_record_ids)
        return {'assetHashes': by_hash, 'mobileRecordIds': by_record}

    # ── assets ────────────────────────────────────────────────────────────

    def check_assets(self, hashes):
        if not isinstance(hashes, list) or not hashes:
            raise ValueError('hashes must be a non-empty list')
        return {'missing': self.repo.missing_assets(
            [_require_hash(h) for h in hashes]
        )}

    def upload_assets(self, assets, actor):
        if not isinstance(assets, list) or not assets:
            raise ValueError('assets must be a non-empty list')

        stored = []
        for asset in assets:
            if not isinstance(asset, dict):
                raise ValueError('Each asset must be an object')
            claimed = _require_hash(asset.get('hash'))
            data = asset.get('base64')
            if not isinstance(data, str) or not data:
                raise ValueError('Each asset needs base64 content')
            check_image_size(data)
            # The store is content-addressed — verify the address, or a bad
            # client could poison every dataset that trusts this hash.
            try:
                raw = base64.b64decode(data, validate=True)
            except (binascii.Error, ValueError):
                raise ValueError('Asset base64 is not decodable')
            actual = hashlib.sha256(raw).hexdigest()
            if actual != claimed:
                raise ValueError(f'Asset hash mismatch (claimed {claimed[:12]}…, '
                                 f'content is {actual[:12]}…)')

            self.repo.upsert_asset({
                'hash': claimed,
                'base64': data,
                'mimeType': asset.get('mimeType') or 'image/png',
                'filename': asset.get('filename'),
                'width': asset.get('width'),
                'height': asset.get('height'),
                'sourceType': asset.get('sourceType'),
            }, actor)
            stored.append(claimed)
        return {'stored': stored}

    def set_asset_meta(self, asset_hash, data, actor):
        """Dentist-entered demographics for one asset (dataset detail's
        "Patient details" editor). Mobile-profile data is authoritative:
        replacing it requires the explicit overwrite flag, which the client
        sends only after the dentist confirmed."""
        _require_hash(asset_hash)
        demographics = _clean_demographics(data.get('demographics'), actor)
        if demographics is None:
            raise ValueError('demographics is required')
        asset = self.repo.get_asset_meta(asset_hash)
        if asset is None:
            raise LookupError('Asset not found')
        existing = asset.get('demographics') or {}
        if (existing.get('source') == 'mobile_profile'
                and demographics['source'] == 'manual'
                and not data.get('overwriteMobileProfile')):
            raise ValueError(
                'This image has demographics from the patient\'s mobile '
                'profile; pass overwriteMobileProfile to replace them'
            )
        self.repo.merge_asset_meta(asset_hash, demographics, None)
        return {'demographics': _jsonify_safe(demographics)}

    def get_asset(self, asset_hash):
        doc = self.repo.get_asset(_require_hash(asset_hash))
        if doc is None:
            raise LookupError('Asset not found')
        return _jsonify_safe({
            'hash': doc['_id'],
            'base64': doc.get('base64Data'),
            'mimeType': doc.get('mimeType'),
            'filename': doc.get('filename'),
            'width': doc.get('width'),
            'height': doc.get('height'),
            'sourceType': doc.get('sourceType'),
            'demographics': doc.get('demographics'),
            'clinicalContext': doc.get('clinicalContext'),
        })

    # ── items ─────────────────────────────────────────────────────────────

    def add_items(self, dataset_id, items, actor):
        _require_id(dataset_id, 'dataset id')
        if self.repo.get_dataset(dataset_id) is None:
            raise LookupError('Dataset not found')
        if not isinstance(items, list) or not items:
            raise ValueError('items must be a non-empty list')

        now = datetime.now(timezone.utc)

        added = []
        skipped = []
        for item in items:
            if not isinstance(item, dict):
                skipped.append({'id': None, 'reason': 'not an object'})
                continue
            try:
                item_id = _require_id(item.get('id'), 'item id')
                asset_hash = _require_hash(item.get('assetHash'))
                source_type = item.get('sourceType')
                if source_type not in ('local', 'mobile'):
                    raise ValueError('sourceType must be local or mobile')
                subject = item.get('subject')
                if subject is not None and subject not in SUBJECTS:
                    raise ValueError('Invalid subject')
                image = item.get('image')
                if not isinstance(image, dict):
                    raise ValueError('image snapshot is required')
                mask_kinds = _mask_kinds(item.get('masks'), include_ai=True)
                demographics = _clean_demographics(item.get('demographics'), actor)
            except ValueError as e:
                skipped.append({'id': item.get('id'), 'reason': str(e)})
                continue

            if self.repo.get_asset(asset_hash) is None:
                skipped.append({'id': item_id, 'reason': 'asset not uploaded'})
                continue

            doc = {
                '_id': item_id,
                'datasetId': dataset_id,
                'assetHash': asset_hash,
                'sourceType': source_type,
                'sourceRef': item.get('sourceRef') if isinstance(item.get('sourceRef'), dict) else {},
                'subject': subject,
                'status': 'unlabeled',
                'excludeReason': None,
                # The snapshot is both the working copy and the frozen AI
                # baseline — they diverge as the dentist relabels.
                'imageJson': image,
                'aiJson': image,
                'version': 1,
                'addedAt': now,
                'addedBy': actor,
                'updatedAt': now,
                'updatedBy': actor,
                'isDeleted': False,
                'deletedAt': None,
            }

            existing = self.repo.find_item_by_asset(dataset_id, asset_hash)
            if existing is not None and not existing.get('isDeleted'):
                skipped.append({'id': item_id, 'reason': 'already in dataset'})
                continue
            if existing is not None:
                self.repo.revive_item(existing['_id'], doc)
                item_id = existing['_id']
            else:
                try:
                    self.repo.insert_item(doc)
                except DuplicateKeyError:
                    # Item ids are client-generated uuids, globally unique
                    # across datasets — a collision means a retry of an add
                    # that already landed, or a misbehaving client.
                    skipped.append({'id': item_id, 'reason': 'duplicate item id'})
                    continue

            if mask_kinds:
                self.repo.put_masks(item_id, mask_kinds, actor)
            self.repo.merge_asset_meta(
                asset_hash, demographics, item.get('clinicalContext')
            )
            self.repo.log_event(item_id, 'import', 'ai', actor,
                                {'sourceType': source_type})
            added.append(item_id)

        return {'added': added, 'skipped': skipped}

    def list_items(self, dataset_id):
        _require_id(dataset_id, 'dataset id')
        if self.repo.get_dataset(dataset_id) is None:
            raise LookupError('Dataset not found')
        docs = self.repo.list_items(dataset_id)
        # Join demographics from the assets so the detail grid can show
        # coverage and edit them without fetching every item individually.
        demo_by_hash = self.repo.assets_demographics(
            [d.get('assetHash') for d in docs if d.get('assetHash')]
        )
        items = []
        for d in docs:
            item = _shape_item(d)
            item['demographics'] = _jsonify_safe(demo_by_hash.get(d.get('assetHash')))
            items.append(item)
        return {'items': items}

    def get_item(self, item_id):
        doc = self.repo.get_item(_require_id(item_id, 'item id'))
        if doc is None:
            raise LookupError('Item not found')
        out = _shape_item(doc, include_ai=True)

        # Patient metadata rides on the asset — join it (bytes excluded) so
        # exports don't have to download the image just for demographics.
        asset = self.repo.get_asset_meta(doc.get('assetHash'))
        if asset is not None:
            out['filename'] = asset.get('filename') or (doc.get('imageJson') or {}).get('filename')
            out['demographics'] = _jsonify_safe(asset.get('demographics'))
            out['clinicalContext'] = _jsonify_safe(asset.get('clinicalContext'))

        masks = self.repo.get_masks(item_id)
        out['gingivitisMask'] = masks.get('gingivitis')
        out['gingivitisMaskAi'] = masks.get('gingivitis_ai')
        # AI plaque variants stay server-side (export-only), matching the old
        # SQLite getter.
        out['plaqueMasks'] = {
            kind[len('plaque_'):]: data
            for kind, data in masks.items()
            if kind.startswith('plaque_') and not kind.endswith('_ai')
        }
        return out

    def save_labels(self, item_id, base_version, image, masks, tasks_changed, actor):
        _require_id(item_id, 'item id')
        if not isinstance(image, dict):
            raise ValueError('image is required')
        # Only ground-truth masks are writable after import; the AI baseline
        # is immutable.
        mask_kinds = _mask_kinds(masks, include_ai=False)
        tasks_changed = tasks_changed or []
        if any(t not in DATASET_TASKS for t in tasks_changed):
            raise ValueError('Unknown task in tasksChanged')

        doc = self.repo.save_labels(item_id, _require_version(base_version),
                                    image, actor)
        if mask_kinds:
            self.repo.put_masks(item_id, mask_kinds, actor)
        for task in tasks_changed:
            self.repo.log_event(item_id, task, 'human', actor)
        return _shape_item(doc)

    def patch_item(self, item_id, base_version, data, actor):
        _require_id(item_id, 'item id')
        fields = {}
        events = []
        if 'status' in data:
            status = data['status']
            if status not in ITEM_STATUSES:
                raise ValueError('Invalid status')
            fields['status'] = status
            reason = data.get('excludeReason')
            fields['excludeReason'] = (
                (reason or '').strip() or None if status == 'excluded' else None
            )
            events.append(('status', {'status': status,
                                      'excludeReason': fields['excludeReason']}))
        if 'subject' in data:
            subject = data['subject']
            if subject not in SUBJECTS:
                raise ValueError('Invalid subject')
            fields['subject'] = subject
            events.append(('subject', {'subject': subject}))
        if not fields:
            raise ValueError('Nothing to update')

        doc = self.repo.patch_item(item_id, _require_version(base_version),
                                   fields, actor)
        for task, detail in events:
            self.repo.log_event(item_id, task, 'human', actor, detail)
        return _shape_item(doc)

    def delete_item(self, item_id, actor):
        self.repo.delete_item(_require_id(item_id, 'item id'), actor)

    # ── exports ───────────────────────────────────────────────────────────

    def record_export(self, dataset_id, item_count, filters, actor):
        _require_id(dataset_id, 'dataset id')
        if self.repo.get_dataset(dataset_id) is None:
            raise LookupError('Dataset not found')
        if not isinstance(item_count, int) or isinstance(item_count, bool) or item_count < 0:
            raise ValueError('itemCount (non-negative integer) is required')
        if filters is not None and not isinstance(filters, dict):
            raise ValueError('filters must be an object')
        version = self.repo.record_export(dataset_id, item_count, filters, actor)
        return {'version': version}

    def list_exports(self, dataset_id):
        _require_id(dataset_id, 'dataset id')
        return {'exports': self.repo.list_exports(dataset_id)}

    def item_events(self, item_id):
        _require_id(item_id, 'item id')
        return {'events': self.repo.item_events(item_id)}


def _require_version(value):
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError('baseVersion (positive integer) is required')
    return value


# Re-exported so controllers can catch it without importing the repository.
__all__ = ['DatasetService', 'ConflictError', 'conflict_payload',
           'DATASET_TASKS', 'ITEM_STATUSES', 'SUBJECTS']
