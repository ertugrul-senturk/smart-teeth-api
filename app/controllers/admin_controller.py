from datetime import datetime, timezone, timedelta

from flask import Blueprint, request, jsonify, current_app, g

from app.config import Config
from app.repositories.registration_key_repository import (
    RegistrationKeyRepository,
    effective_status,
    _aware,
)
from app.repositories.update_repository import (
    UpdateRepository,
    ALLOWED_PLATFORMS,
    parse_version,
)
from app.utils.middleware import master_key_required
from app.utils.audit import audit
from app.utils.rate_limit import limiter

admin_bp = Blueprint('admin', __name__)

MASTER_ACTOR = 'admin:master'

# A start date of "today" may arrive as local midnight, which is already in
# the past in UTC — accept up to 24h back so today always passes and yesterday
# never does, regardless of the client's timezone.
START_DATE_GRACE = timedelta(hours=24)


def get_key_repo():
    if 'key_repo' not in g:
        g.key_repo = RegistrationKeyRepository(current_app.config['DB'])
    return g.key_repo


def _parse_iso(value, field):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        raise ValueError(f"Invalid '{field}' date (expected ISO 8601)")
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt):
    dt = _aware(dt)
    return dt.isoformat().replace('+00:00', 'Z') if dt else None


def _key_json(doc, stats=None):
    stats = stats or {}
    return {
        'id': str(doc['_id']),
        'name': doc['name'],
        'keyHint': doc.get('keyHint'),
        'status': effective_status(doc),
        'startsAt': _iso(doc.get('startsAt')),
        'expiresAt': _iso(doc.get('expiresAt')),
        'createdAt': _iso(doc.get('createdAt')),
        'expiredAt': _iso(doc.get('expiredAt')),
        'source': 'managed',
        'deviceCount': stats.get('deviceCount', 0),
        'sessionCount': stats.get('sessionCount', 0),
        'registrationCount': stats.get('registrationCount', 0),
        'userNames': stats.get('userNames', []),
        'lastSeenAt': _iso(stats.get('lastSeenAt')),
    }


def _install_json(row):
    return {
        'deviceId': row.get('deviceId'),
        'userName': row.get('labelerName') or None,
        'firstSeenAt': _iso(row.get('firstSeenAt')),
        'lastSeenAt': _iso(row.get('lastSeenAt')),
        'sessionCount': row.get('sessionCount', 0),
        'registrationCount': row.get('registrationCount', 0),
        'lastRegisteredAt': _iso(row.get('lastRegisteredAt')),
        'appVersion': row.get('appVersion'),
    }


@admin_bp.route('/ping', methods=['GET'])
@limiter.limit(Config.RATELIMIT_LOGIN)
@master_key_required
def ping():
    """Cheap master-key probe: the desktop app calls this to unlock the
    hidden admin tab. Rate-limited like login since it's a guessing target."""
    return jsonify({'ok': True}), 200


@admin_bp.route('/keys', methods=['GET'])
@master_key_required
def list_keys():
    repo = get_key_repo()
    stats = repo.install_stats()
    keys = [_key_json(doc, stats.get(str(doc['_id']))) for doc in repo.list_keys()]
    return jsonify({'keys': keys}), 200


@admin_bp.route('/keys', methods=['POST'])
@master_key_required
def create_key():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'message': 'A key name is required'}), 400

    try:
        starts_at = _parse_iso(data.get('startsAt'), 'startsAt')
        expires_at = _parse_iso(data.get('expiresAt'), 'expiresAt')
    except ValueError as e:
        return jsonify({'message': str(e)}), 400

    now = datetime.now(timezone.utc)
    if starts_at < now - START_DATE_GRACE:
        return jsonify({'message': 'Start date cannot be in the past'}), 400
    if expires_at <= starts_at:
        return jsonify({'message': 'End date must be after the start date'}), 400

    repo = get_key_repo()
    doc, plain = repo.create_key(name, starts_at, expires_at)
    audit('admin.key_created', MASTER_ACTOR, keyId=str(doc['_id']), keyName=name,
          startsAt=_iso(starts_at), expiresAt=_iso(expires_at))

    body = _key_json(doc)
    body['key'] = plain  # full key returned once at creation (and via reveal)
    return jsonify(body), 201


@admin_bp.route('/keys/<key_id>', methods=['GET'])
@master_key_required
def key_details(key_id):
    repo = get_key_repo()
    doc = repo.get(key_id)
    if doc is None:
        return jsonify({'message': 'Key not found'}), 404
    ref = repo.key_ref(doc)
    body = _key_json(doc, repo.install_stats().get(ref))
    body['installs'] = [_install_json(row) for row in repo.installs_for(ref)]
    return jsonify(body), 200


@admin_bp.route('/keys/<key_id>/reveal', methods=['GET'])
@master_key_required
def reveal_key(key_id):
    repo = get_key_repo()
    doc = repo.get(key_id)
    if doc is None:
        return jsonify({'message': 'Key not found'}), 404
    audit('admin.key_revealed', MASTER_ACTOR, keyId=key_id, keyName=doc['name'])
    return jsonify({'id': key_id, 'key': repo.reveal(doc)}), 200


@admin_bp.route('/keys/<key_id>/expire', methods=['POST'])
@master_key_required
def expire_key(key_id):
    repo = get_key_repo()
    doc = repo.get(key_id)
    if doc is None:
        return jsonify({'message': 'Key not found'}), 404
    repo.expire_now(key_id)
    audit('admin.key_expired', MASTER_ACTOR, keyId=key_id, keyName=doc['name'])
    return jsonify(_key_json(repo.get(key_id))), 200


@admin_bp.route('/keys/<key_id>', methods=['PATCH'])
@master_key_required
def update_key(key_id):
    """Master edits: rename, or move the validity window. Setting a future
    expiresAt on an expired key reactivates it — deliberate, the master has
    full control."""
    data = request.get_json(silent=True) or {}
    repo = get_key_repo()
    doc = repo.get(key_id)
    if doc is None:
        return jsonify({'message': 'Key not found'}), 404

    updates = {}
    if 'name' in data:
        name = (data.get('name') or '').strip()
        if not name:
            return jsonify({'message': 'A key name is required'}), 400
        updates['name'] = name

    try:
        if data.get('startsAt') is not None:
            updates['startsAt'] = _parse_iso(data['startsAt'], 'startsAt')
        if data.get('expiresAt') is not None:
            updates['expiresAt'] = _parse_iso(data['expiresAt'], 'expiresAt')
    except ValueError as e:
        return jsonify({'message': str(e)}), 400

    starts_at = _aware(updates.get('startsAt', doc.get('startsAt')))
    expires_at = _aware(updates.get('expiresAt', doc.get('expiresAt')))
    if starts_at and expires_at and expires_at <= starts_at:
        return jsonify({'message': 'End date must be after the start date'}), 400

    if 'expiresAt' in updates and updates['expiresAt'] > datetime.now(timezone.utc):
        # A new future end date always reactivates a manually-expired key.
        updates['status'] = 'active'
        updates['expiredAt'] = None

    updated = repo.update_key(key_id, updates)
    audit('admin.key_updated', MASTER_ACTOR, keyId=key_id, keyName=updated['name'],
          changes=sorted(updates.keys()))
    return jsonify(_key_json(updated, repo.install_stats().get(repo.key_ref(updated)))), 200


@admin_bp.route('/keys/<key_id>', methods=['DELETE'])
@master_key_required
def delete_key(key_id):
    repo = get_key_repo()
    doc = repo.get(key_id)
    if doc is None:
        return jsonify({'message': 'Key not found'}), 404
    repo.delete_key(key_id)
    audit('admin.key_deleted', MASTER_ACTOR, keyId=key_id, keyName=doc['name'])
    return jsonify({'ok': True}), 200


# ── OTA updates ──────────────────────────────────────────────────────────────

def get_update_repo():
    if 'update_repo' not in g:
        g.update_repo = UpdateRepository(current_app.config['DB'])
    return g.update_repo


def _release_json(doc, repo):
    platforms = {}
    for platform, entry in doc.get('platforms', {}).items():
        asset = repo.get_asset(entry.get('assetId'))
        platforms[platform] = {
            'assetId': entry.get('assetId'),
            'filename': entry.get('filename'),
            'size': entry.get('size', 0),
            'downloadCount': (asset or {}).get('downloadCount', 0),
        }
    return {
        'id': str(doc['_id']),
        'version': doc['version'],
        'notes': doc.get('notes') or '',
        'active': bool(doc.get('active')),
        'pubDate': _iso(doc.get('pubDate')),
        'createdAt': _iso(doc.get('createdAt')),
        'platforms': platforms,
    }


@admin_bp.route('/updates/assets', methods=['POST'])
@master_key_required
def init_update_asset():
    data = request.get_json(silent=True) or {}
    filename = (data.get('filename') or '').strip()
    size = data.get('size')
    sha256 = (data.get('sha256') or '').strip().lower()
    chunk_count = data.get('chunkCount')

    if not filename:
        return jsonify({'message': 'filename is required'}), 400
    if not isinstance(size, int) or size <= 0:
        return jsonify({'message': 'size must be a positive integer'}), 400
    if len(sha256) != 64 or any(c not in '0123456789abcdef' for c in sha256):
        return jsonify({'message': 'sha256 must be a 64-char hex digest'}), 400
    if not isinstance(chunk_count, int) or chunk_count <= 0:
        return jsonify({'message': 'chunkCount must be a positive integer'}), 400

    asset = get_update_repo().init_asset(filename, size, sha256, chunk_count)
    return jsonify({'id': str(asset['_id'])}), 201


@admin_bp.route('/updates/assets/<asset_id>/chunks/<int:seq>', methods=['PUT'])
@master_key_required
def put_update_chunk(asset_id, seq):
    repo = get_update_repo()
    asset = repo.get_asset(asset_id)
    if asset is None:
        return jsonify({'message': 'Asset not found'}), 404
    if asset.get('complete'):
        return jsonify({'message': 'Asset is already complete'}), 409
    if not (0 <= seq < asset['chunkCount']):
        return jsonify({'message': f"Chunk {seq} out of range (0..{asset['chunkCount'] - 1})"}), 400

    data = request.get_data(cache=False)
    if not data:
        return jsonify({'message': 'Empty chunk'}), 400
    if len(data) > UpdateRepository.CHUNK_DOC_LIMIT_BYTES:
        return jsonify({'message': 'Chunk too large'}), 413

    repo.put_chunk(asset['_id'], seq, data)
    return jsonify({'ok': True}), 200


@admin_bp.route('/updates/assets/<asset_id>/complete', methods=['POST'])
@master_key_required
def complete_update_asset(asset_id):
    asset, error = get_update_repo().complete_asset(asset_id)
    if error:
        return jsonify({'message': error}), 400
    return jsonify({'id': str(asset['_id']), 'size': asset['size']}), 200


@admin_bp.route('/updates', methods=['GET'])
@master_key_required
def list_updates():
    repo = get_update_repo()
    return jsonify({'releases': [_release_json(doc, repo) for doc in repo.list_releases()]}), 200


@admin_bp.route('/updates', methods=['POST'])
@master_key_required
def publish_update():
    data = request.get_json(silent=True) or {}
    version = (data.get('version') or '').strip()
    notes = (data.get('notes') or '').strip()
    platforms_in = data.get('platforms')

    if parse_version(version) is None:
        return jsonify({'message': 'version must be numeric like 2.1.0'}), 400
    if not isinstance(platforms_in, dict) or not platforms_in:
        return jsonify({'message': 'At least one platform artifact is required'}), 400

    repo = get_update_repo()
    if repo.version_exists(version):
        return jsonify({'message': f'Version {version} already exists'}), 409

    platforms = {}
    for platform, entry in platforms_in.items():
        if platform not in ALLOWED_PLATFORMS:
            return jsonify({'message': f'Unknown platform: {platform}'}), 400
        asset = repo.get_asset((entry or {}).get('assetId'))
        signature = ((entry or {}).get('signature') or '').strip()
        if asset is None or not asset.get('complete'):
            return jsonify({'message': f'{platform}: asset missing or upload incomplete'}), 400
        if not signature:
            return jsonify({'message': f'{platform}: updater signature (.sig) is required'}), 400
        platforms[platform] = {
            'assetId': str(asset['_id']),
            'signature': signature,
            'filename': asset['filename'],
            'size': asset['size'],
        }

    doc = repo.create_release(version, notes, platforms)
    audit('admin.update_published', MASTER_ACTOR, version=version,
          platforms=sorted(platforms.keys()))
    return jsonify(_release_json(doc, repo)), 201


@admin_bp.route('/updates/<release_id>', methods=['PATCH'])
@master_key_required
def patch_update(release_id):
    data = request.get_json(silent=True) or {}
    repo = get_update_repo()
    doc = repo.get_release(release_id)
    if doc is None:
        return jsonify({'message': 'Release not found'}), 404
    if 'active' in data:
        doc = repo.set_active(release_id, bool(data['active']))
        audit('admin.update_toggled', MASTER_ACTOR,
              version=doc['version'], active=doc['active'])
    return jsonify(_release_json(doc, repo)), 200


@admin_bp.route('/updates/<release_id>', methods=['DELETE'])
@master_key_required
def delete_update(release_id):
    repo = get_update_repo()
    doc = repo.get_release(release_id)
    if doc is None:
        return jsonify({'message': 'Release not found'}), 404
    repo.delete_release(release_id)
    audit('admin.update_deleted', MASTER_ACTOR, version=doc['version'])
    return jsonify({'ok': True}), 200
