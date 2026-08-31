import hashlib
import re
from datetime import datetime, timezone

from bson import Binary, ObjectId
from bson.errors import InvalidId

from app.config.collections import (
    APP_UPDATES_COLLECTION,
    UPDATE_ASSETS_COLLECTION,
    UPDATE_ASSET_CHUNKS_COLLECTION,
)

# Platform identifiers the Tauri updater reports: {{target}}-{{arch}}.
ALLOWED_PLATFORMS = frozenset({
    'windows-x86_64',
    'darwin-x86_64',
    'darwin-aarch64',
    'linux-x86_64',
})

_VERSION_RE = re.compile(r'^\d+(\.\d+){0,3}$')


def parse_version(version):
    """'2.1.0' → (2, 1, 0). Returns None for anything non-numeric — versions
    are ours to choose, so pre-release tags are simply not supported."""
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        return None
    return tuple(int(part) for part in version.split('.'))


def _utcnow():
    return datetime.now(timezone.utc)


def _oid(value):
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


class UpdateRepository:
    """OTA releases and their installer binaries.

    Installers arrive from the desktop admin tab in ≤8MB chunks (Mongo caps a
    document at 16MB) and are streamed back to updating devices in the same
    chunks. A release maps platform → (asset, updater signature); the Tauri
    updater on the device verifies the signature against the app's baked-in
    public key, so the server never needs the private signing key.
    """

    CHUNK_DOC_LIMIT_BYTES = 12 * 1024 * 1024  # headroom under Mongo's 16MB cap

    def __init__(self, db):
        self.releases = db[APP_UPDATES_COLLECTION]
        self.assets = db[UPDATE_ASSETS_COLLECTION]
        self.chunks = db[UPDATE_ASSET_CHUNKS_COLLECTION]

    # ── Assets (chunked installer upload) ────────────────────────────────

    def init_asset(self, filename, size, sha256, chunk_count):
        doc = {
            'filename': filename,
            'size': size,
            'sha256': sha256,
            'chunkCount': chunk_count,
            'complete': False,
            'downloadCount': 0,
            'createdAt': _utcnow(),
        }
        doc['_id'] = self.assets.insert_one(doc).inserted_id
        return doc

    def get_asset(self, asset_id):
        oid = _oid(asset_id)
        return self.assets.find_one({'_id': oid}) if oid else None

    def put_chunk(self, asset_id, seq, data):
        self.chunks.update_one(
            {'assetId': str(asset_id), 'seq': seq},
            {'$set': {'data': Binary(data)}},
            upsert=True,
        )

    def complete_asset(self, asset_id):
        """Verify every chunk arrived and the bytes hash to what the uploader
        promised; only then does the asset become publishable."""
        asset = self.get_asset(asset_id)
        if asset is None:
            return None, 'Asset not found'

        seqs = [c['seq'] for c in
                self.chunks.find({'assetId': str(asset['_id'])}, {'seq': 1})]
        if sorted(seqs) != list(range(asset['chunkCount'])):
            return None, (f"Upload incomplete: {len(seqs)} of "
                          f"{asset['chunkCount']} chunks received")

        digest = hashlib.sha256()
        total = 0
        for chunk in self.iter_chunks(asset['_id']):
            digest.update(chunk)
            total += len(chunk)
        if total != asset['size'] or digest.hexdigest() != asset['sha256']:
            return None, 'Upload corrupt: size or checksum mismatch'

        self.assets.update_one({'_id': asset['_id']}, {'$set': {'complete': True}})
        asset['complete'] = True
        return asset, None

    def iter_chunks(self, asset_id):
        for chunk in self.chunks.find({'assetId': str(asset_id)}).sort('seq', 1):
            yield bytes(chunk['data'])

    def count_download(self, asset_id):
        oid = _oid(asset_id)
        if oid:
            self.assets.update_one({'_id': oid}, {'$inc': {'downloadCount': 1}})

    def delete_asset(self, asset_id):
        oid = _oid(asset_id)
        if oid:
            self.assets.delete_one({'_id': oid})
            self.chunks.delete_many({'assetId': str(oid)})

    # ── Releases ─────────────────────────────────────────────────────────

    def create_release(self, version, notes, platforms):
        doc = {
            'version': version,
            'notes': notes,
            'platforms': platforms,
            'active': True,
            'createdAt': _utcnow(),
            'pubDate': _utcnow(),
        }
        doc['_id'] = self.releases.insert_one(doc).inserted_id
        return doc

    def list_releases(self):
        return list(self.releases.find().sort('createdAt', -1))

    def get_release(self, release_id):
        oid = _oid(release_id)
        return self.releases.find_one({'_id': oid}) if oid else None

    def version_exists(self, version):
        return self.releases.count_documents({'version': version}, limit=1) > 0

    def set_active(self, release_id, active):
        oid = _oid(release_id)
        if not oid:
            return None
        return self.releases.find_one_and_update(
            {'_id': oid}, {'$set': {'active': bool(active)}}, return_document=True,
        )

    def delete_release(self, release_id):
        """Delete the release and any of its assets no other release uses."""
        release = self.get_release(release_id)
        if release is None:
            return False
        self.releases.delete_one({'_id': release['_id']})
        for entry in release.get('platforms', {}).values():
            asset_id = entry.get('assetId')
            if not asset_id:
                continue
            still_used = self.releases.count_documents(
                {'$or': [{f'platforms.{p}.assetId': asset_id}
                         for p in ALLOWED_PLATFORMS]},
                limit=1,
            ) > 0
            if not still_used:
                self.delete_asset(asset_id)
        return True

    def latest_for(self, platform, current_version):
        """The newest active release for `platform` strictly newer than the
        device's current version — or None (HTTP 204 upstream)."""
        current = parse_version(current_version)
        best = None
        best_tuple = None
        for release in self.releases.find(
                {'active': True, f'platforms.{platform}': {'$exists': True}}):
            candidate = parse_version(release['version'])
            if candidate is None:
                continue
            if current is not None and candidate <= current:
                continue
            if best_tuple is None or candidate > best_tuple:
                best, best_tuple = release, candidate
        return best
