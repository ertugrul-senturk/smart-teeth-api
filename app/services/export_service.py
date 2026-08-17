"""Dataset export: everything the system knows, as one organized ZIP.

Layout (schema 1.0):

    smart-teeth-dataset_<stamp>/
    ├─ manifest.json                     # params, counts, skipped, field docs
    ├─ mobile/
    │  ├─ patients.jsonl                 # demographics, one row per patient
    │  ├─ scans.jsonl                    # tooth_scan_history + image paths
    │  ├─ records/<collection>.jsonl     # plans, risk scores, questionnaires
    │  └─ images/<patientId>/<recordId>/…   # decoded binaries (mouth/gum/teeth)
    └─ desktop/
       ├─ entries.jsonl                  # analysis history entries
       └─ images/<deviceId>/<entryId>/…  # masks + originals (id == path)

Design invariants:
- patientId is a deterministic HMAC pseudo-id: stable across exports (so
  successive dataset versions are joinable) but never the raw Mongo id.
- Demographics ride along denormalized on every scan row (patient_age, …)
  so the file is directly loadable without a join.
- Nothing is silently dropped: unreadable/missing images land in
  manifest['skipped'] with a reason.
"""

import base64
import hashlib
import hmac as hmac_mod
import json
import os
import posixpath
import zipfile
from datetime import datetime, timezone

from app.config import Config
from app.config.collections import (
    ALLOWED_SYNC_COLLECTIONS,
    DESKTOP_HISTORY_COLLECTION,
    DESKTOP_IMAGES_COLLECTION,
    IMAGES_COLLECTION,
)
from app.models import User
from app.repositories.sync_repository import _jsonify_safe

SCHEMA_VERSION = '1.0'
SCANS_COLLECTION = 'tooth_scan_history'

_NOT_DELETED = {'$or': [{'isDeleted': {'$exists': False}}, {'isDeleted': False}]}

_EXT_BY_MIME = {
    'image/jpeg': 'jpg',
    'image/png': 'png',
    'image/gif': 'gif',
    'image/webp': 'webp',
    'image/heic': 'heic',
    'image/heif': 'heif',
    'image/tiff': 'tif',
}


def pseudo_patient_id(user_id):
    """Deterministic pseudonymous id — stable across exports, not reversible
    to the Mongo id without the server secret."""
    digest = hmac_mod.new(
        Config.SECRET_KEY.encode(),
        f'export-pid:{user_id}'.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f'p_{digest[:16]}'


def _ext_for(mime_type):
    return _EXT_BY_MIME.get((mime_type or '').lower(), 'jpg')


def _safe_zip_path(*parts):
    """Join path parts for use inside the ZIP, refusing traversal tricks."""
    joined = posixpath.join(*[str(p).replace('\\', '/') for p in parts])
    normalized = posixpath.normpath(joined)
    if normalized.startswith(('/', '..')) or '..' in normalized.split('/'):
        raise ValueError(f'Unsafe path in export: {joined!r}')
    return normalized


class ExportService:

    def __init__(self, db):
        self.db = db

    # ── public entry point ────────────────────────────────────────────────

    def build_dataset(self, out_path, source='both', date_from=None, date_to=None,
                      patient_ids=None, device_ids=None, collections=None,
                      anonymize=False, include_images=True):
        """Write the dataset ZIP to out_path. Returns the manifest dict."""
        if source not in ('mobile', 'desktop', 'both'):
            raise ValueError("source must be 'mobile', 'desktop' or 'both'")

        wanted = set(collections or ALLOWED_SYNC_COLLECTIONS)
        unknown = wanted - ALLOWED_SYNC_COLLECTIONS
        if unknown:
            raise ValueError(f"Unknown collections: {', '.join(sorted(unknown))}")

        stamp = datetime.now(timezone.utc)
        root = f"smart-teeth-dataset_{stamp.strftime('%Y%m%d_%H%M%S')}"
        manifest = {
            'schemaVersion': SCHEMA_VERSION,
            'generatedAt': stamp.isoformat(),
            'params': {
                'source': source,
                'from': date_from.isoformat() if date_from else None,
                'to': date_to.isoformat() if date_to else None,
                'collections': sorted(wanted),
                'anonymize': bool(anonymize),
                'includeImages': bool(include_images),
                'patientIds': f'{len(patient_ids)} selected' if patient_ids else 'all',
                'deviceIds': f'{len(device_ids)} selected' if device_ids else 'all',
            },
            'counts': {},
            'skipped': [],
            'notes': [
                'patientId is a stable pseudonymous id (HMAC of the internal id).',
                'anonymize strips name/email; demographics and pseudo-ids remain.',
                'Image fields on rows are paths relative to the dataset root.',
            ],
        }

        with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            if source in ('mobile', 'both'):
                self._export_mobile(zf, root, manifest, wanted, date_from, date_to,
                                    patient_ids, anonymize, include_images)
            if source in ('desktop', 'both'):
                self._export_desktop(zf, root, manifest, date_from, date_to,
                                     device_ids, include_images)

            zf.writestr(f'{root}/manifest.json',
                        json.dumps(manifest, indent=2, default=str))

        return manifest

    # ── mobile side ───────────────────────────────────────────────────────

    def _selected_users(self, patient_ids):
        from bson import ObjectId
        from bson.errors import InvalidId

        query = {}
        if patient_ids:
            oids = []
            for pid in patient_ids:
                try:
                    oids.append(ObjectId(pid))
                except (InvalidId, TypeError):
                    continue
            query['_id'] = {'$in': oids}

        users = []
        for doc in self.db['users'].find(query):
            try:
                users.append(User.from_dict(doc))
            except Exception:
                # Undecryptable row (e.g. foreign SECRET_KEY) — record, move on.
                users.append(None)
        return [u for u in users if u]

    def _demographics(self, user):
        return {
            'patient_age': user.age,
            'patient_gender': user.gender,
            'patient_ethnicity': user.ethnicity,
            'patient_has_insurance': user.has_insurance,
            'patient_last_doctor_visit': user.last_doctor_visit,
        }

    def _time_query(self, date_from, date_to):
        created = {}
        if date_from:
            created['$gte'] = date_from
        if date_to:
            created['$lte'] = date_to
        return {'createdAt': created} if created else {}

    def _export_mobile(self, zf, root, manifest, wanted, date_from, date_to,
                       patient_ids, anonymize, include_images):
        users = self._selected_users(patient_ids)
        pid_by_uid = {str(u._id): pseudo_patient_id(u._id) for u in users}

        # patients.jsonl — demographics (the registration data)
        patient_rows = []
        for user in users:
            row = {
                'patientId': pid_by_uid[str(user._id)],
                'age': user.age,
                'gender': user.gender,
                'ethnicity': user.ethnicity,
                'has_insurance': user.has_insurance,
                'last_doctor_visit': user.last_doctor_visit,
                'status': user.status,
                'registered_at': user.created_at.isoformat() if user.created_at else None,
            }
            if not anonymize:
                row['name'] = user.name
                row['email'] = user.email
            patient_rows.append(row)
        self._write_jsonl(zf, f'{root}/mobile/patients.jsonl', patient_rows)
        manifest['counts']['mobile/patients.jsonl'] = len(patient_rows)

        time_query = self._time_query(date_from, date_to)

        for collection_key in sorted(wanted):
            rows = []
            for user in users:
                uid = str(user._id)
                query = {'userId': uid, **_NOT_DELETED, **time_query}
                for doc in self.db[collection_key].find(query).sort('createdAt', 1):
                    record = _jsonify_safe(doc)
                    record.pop('_id', None)
                    record.pop('userId', None)
                    record['patientId'] = pid_by_uid[uid]
                    record.update(self._demographics(user))

                    if collection_key == SCANS_COLLECTION and include_images:
                        self._attach_scan_images(zf, root, manifest, uid, record)
                    rows.append(record)

            if collection_key == SCANS_COLLECTION:
                path = f'{root}/mobile/scans.jsonl'
                manifest['counts']['mobile/scans.jsonl'] = len(rows)
            else:
                path = f'{root}/mobile/records/{collection_key}.jsonl'
                manifest['counts'][f'mobile/records/{collection_key}.jsonl'] = len(rows)
            self._write_jsonl(zf, path, rows)

    def _attach_scan_images(self, zf, root, manifest, user_id, record):
        """Decode and pack every image a scan references; add relative-path
        fields next to the id fields. Paths are namespaced by pseudo patient
        id — record ids are only unique per user, not globally."""
        record_id = str(record.get('id') or 'unknown')
        patient_dir = record['patientId']

        def pack(image_id, filename, path_field, target):
            if not image_id:
                return
            rel = self._write_mobile_image(zf, root, manifest, user_id,
                                           image_id, patient_dir, record_id, filename)
            if rel:
                target[path_field] = rel

        pack(record.get('mouthImageId'), 'mouth', 'mouthImagePath', record)
        pack(record.get('gumImageId'), 'gum', 'gumImagePath', record)
        pack(record.get('gumMaskId'), 'gum_mask', 'gumMaskPath', record)

        for tooth in record.get('toothImages') or []:
            if not isinstance(tooth, dict):
                continue
            index = tooth.get('index', 'x')
            pack(tooth.get('imageId'), f'tooth_{index}', 'imagePath', tooth)
            pack(tooth.get('plaqueMaskId'), f'tooth_{index}_plaque', 'plaqueMaskPath', tooth)

    def _write_mobile_image(self, zf, root, manifest, user_id, image_id,
                            patient_dir, record_id, filename):
        """Fetch one image doc, decode, write into the ZIP. Returns the
        dataset-relative path, or None (recorded in skipped)."""
        from bson import ObjectId
        from bson.errors import InvalidId

        try:
            oid = ObjectId(str(image_id))
        except (InvalidId, TypeError):
            oid = image_id

        doc = self.db[IMAGES_COLLECTION].find_one({'userId': user_id, '_id': oid})
        if not doc:
            manifest['skipped'].append({
                'type': 'image', 'source': 'mobile', 'recordId': record_id,
                'imageId': str(image_id), 'reason': 'not found',
            })
            return None

        try:
            data = base64.b64decode(doc.get('base64Data') or '')
        except Exception:
            manifest['skipped'].append({
                'type': 'image', 'source': 'mobile', 'recordId': record_id,
                'imageId': str(image_id), 'reason': 'undecodable base64',
            })
            return None

        ext = _ext_for(doc.get('mimeType'))
        rel = _safe_zip_path('mobile', 'images', patient_dir, record_id,
                             f'{filename}.{ext}')
        zf.writestr(f'{root}/{rel}', data)
        return rel

    # ── desktop side ──────────────────────────────────────────────────────

    def _export_desktop(self, zf, root, manifest, date_from, date_to,
                        device_ids, include_images):
        query = {**self._time_query(date_from, date_to)}
        if device_ids:
            query['deviceId'] = {'$in': list(device_ids)}

        entries = []
        referenced = {}  # deviceId -> set(image ids)
        for doc in self.db[DESKTOP_HISTORY_COLLECTION].find(query).sort('createdAt', 1):
            entry = _jsonify_safe(doc)
            entry.pop('_id', None)
            entries.append(entry)
            ids = referenced.setdefault(entry.get('deviceId'), set())
            for field in ('maskImageIds', 'originalImageIds'):
                for image_id in entry.get(field) or []:
                    if isinstance(image_id, str):
                        ids.add(image_id)

        self._write_jsonl(zf, f'{root}/desktop/entries.jsonl', entries)
        manifest['counts']['desktop/entries.jsonl'] = len(entries)

        if not include_images:
            return

        packed = 0
        for device_id, ids in referenced.items():
            if not ids:
                continue
            found = set()
            cursor = self.db[DESKTOP_IMAGES_COLLECTION].find(
                {'deviceId': device_id, 'id': {'$in': sorted(ids)}}
            )
            for doc in cursor:
                image_id = doc.get('id', '')
                try:
                    data = base64.b64decode(doc.get('base64Data') or '')
                    # Desktop image ids are already relative paths
                    # ({entryId}/{imageId}/kind.png) — reuse as layout,
                    # namespaced by device (entry ids are per-install).
                    rel = _safe_zip_path('desktop', 'images', device_id, image_id)
                except Exception:
                    manifest['skipped'].append({
                        'type': 'image', 'source': 'desktop',
                        'imageId': image_id, 'reason': 'undecodable or unsafe id',
                    })
                    continue
                zf.writestr(f'{root}/{rel}', data)
                found.add(image_id)
                packed += 1

            for missing in ids - found:
                manifest['skipped'].append({
                    'type': 'image', 'source': 'desktop',
                    'imageId': missing, 'reason': 'not found',
                })

        manifest['counts']['desktop/images'] = packed

    # ── shared ────────────────────────────────────────────────────────────

    @staticmethod
    def _write_jsonl(zf, zip_path, rows):
        payload = '\n'.join(json.dumps(row, default=str) for row in rows)
        zf.writestr(zip_path, payload + ('\n' if rows else ''))
