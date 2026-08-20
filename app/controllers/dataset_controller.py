"""HTTP surface of the shared dataset workspace: /v1/desktop/datasets/*.

Same auth as the rest of the desktop API (static named keys). Writes
additionally require an X-Labeler-Name header (the dentist name entered at
registration, min 3 chars) — it becomes updatedBy on documents and labeledBy
on label events, which is what conflict dialogs and provenance display.

Concurrency contract: every mutating call on an existing document carries
baseVersion; a stale write returns 409 with the winning document's
version/updatedBy so the client can offer reload-or-overwrite.
"""

from functools import wraps
from urllib.parse import unquote

from flask import Blueprint, request, jsonify, current_app, g

from app.services.dataset_service import (
    DatasetService, ConflictError, conflict_payload,
)
from app.services.sync_service import ImageTooLargeError
from app.utils.middleware import api_key_required
from app.utils.audit import audit

datasets_bp = Blueprint('datasets', __name__)

MIN_LABELER_NAME_LEN = 3


def get_dataset_service():
    if 'dataset_service' not in g:
        g.dataset_service = DatasetService(current_app.config['DB'])
    return g.dataset_service


def _actor():
    """Audit identity: API key name + the caller's device id (if sent)."""
    device = request.headers.get('X-Device-Id')
    key_name = getattr(g, 'api_key_name', 'unknown')
    return f"desktop:{key_name}/{device}" if device else f"desktop:{key_name}"


def _labeler():
    """Dentist display name for writes — required so every edit in a shared
    dataset is attributable to a person, not just an install. The desktop
    client percent-encodes the header (names can be any UTF-8, headers can't);
    unquote is a no-op for plain ASCII names."""
    name = unquote((request.headers.get('X-Labeler-Name') or '')).strip()
    if len(name) < MIN_LABELER_NAME_LEN:
        raise ValueError(
            f'X-Labeler-Name header (min {MIN_LABELER_NAME_LEN} characters) '
            f'is required for dataset writes'
        )
    return name


def _handles(what):
    """Uniform error mapping for every dataset route. Order matters:
    ImageTooLargeError subclasses ValueError, so it is caught first."""
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            try:
                return f(*args, **kwargs)
            except ConflictError as e:
                return jsonify({
                    'message': 'Version conflict',
                    'conflict': conflict_payload(e.current),
                }), 409
            except ImageTooLargeError as e:
                return jsonify({'message': str(e)}), 413
            except ValueError as e:
                return jsonify({'message': str(e)}), 400
            except LookupError as e:
                return jsonify({'message': str(e)}), 404
            except Exception:
                current_app.logger.exception(f'{what} failed')
                return jsonify({'message': f'{what} failed'}), 500
        return wrapped
    return decorator


def _body():
    return request.get_json(silent=True) or {}


# ── datasets ─────────────────────────────────────────────────────────────────

@datasets_bp.route('', methods=['GET'])
@api_key_required
@_handles('Dataset list')
def list_datasets():
    return jsonify({'datasets': get_dataset_service().list_datasets()}), 200


@datasets_bp.route('', methods=['POST'])
@api_key_required
@_handles('Dataset create')
def create_dataset():
    labeler = _labeler()
    data = _body()
    dataset = get_dataset_service().create_dataset(
        data.get('name'), data.get('description'), data.get('tasks'), labeler,
    )
    audit('dataset.create', _actor(), labeler=labeler,
          datasetId=dataset['id'], name=dataset['name'])
    return jsonify({'dataset': dataset}), 201


@datasets_bp.route('/<dataset_id>', methods=['PATCH'])
@api_key_required
@_handles('Dataset update')
def update_dataset(dataset_id):
    labeler = _labeler()
    data = _body()
    dataset = get_dataset_service().update_dataset(
        dataset_id, data.get('baseVersion'), data, labeler,
    )
    audit('dataset.update', _actor(), labeler=labeler, datasetId=dataset_id)
    return jsonify({'dataset': dataset}), 200


@datasets_bp.route('/<dataset_id>', methods=['DELETE'])
@api_key_required
@_handles('Dataset delete')
def delete_dataset(dataset_id):
    labeler = _labeler()
    get_dataset_service().delete_dataset(dataset_id, labeler)
    audit('dataset.delete', _actor(), labeler=labeler, datasetId=dataset_id)
    return jsonify({'ok': True}), 200


# ── delta feed & usage ───────────────────────────────────────────────────────

@datasets_bp.route('/changes', methods=['GET'])
@api_key_required
@_handles('Dataset changes')
def changes():
    # Polled every ~25s per client — deliberately not audited.
    return jsonify(get_dataset_service().changes(request.args.get('since'))), 200


@datasets_bp.route('/usage', methods=['POST'])
@api_key_required
@_handles('Dataset usage lookup')
def usage():
    data = _body()
    result = get_dataset_service().usage(
        data.get('assetHashes'), data.get('mobileRecordIds'),
    )
    return jsonify(result), 200


# ── assets ───────────────────────────────────────────────────────────────────

@datasets_bp.route('/assets/check', methods=['POST'])
@api_key_required
@_handles('Asset check')
def check_assets():
    return jsonify(get_dataset_service().check_assets(_body().get('hashes'))), 200


@datasets_bp.route('/assets', methods=['POST'])
@api_key_required
@_handles('Asset upload')
def upload_assets():
    labeler = _labeler()
    result = get_dataset_service().upload_assets(_body().get('assets'), labeler)
    audit('dataset.assets.upload', _actor(), labeler=labeler,
          count=len(result['stored']))
    return jsonify(result), 200


@datasets_bp.route('/assets/<asset_hash>/meta', methods=['PATCH'])
@api_key_required
@_handles('Asset metadata update')
def set_asset_meta(asset_hash):
    labeler = _labeler()
    result = get_dataset_service().set_asset_meta(asset_hash, _body(), labeler)
    audit('dataset.asset.meta', _actor(), labeler=labeler, assetHash=asset_hash)
    return jsonify(result), 200


@datasets_bp.route('/assets/<asset_hash>', methods=['GET'])
@api_key_required
@_handles('Asset fetch')
def get_asset(asset_hash):
    asset = get_dataset_service().get_asset(asset_hash)
    # Assets can carry patient imagery/metadata — reads are audited like the
    # patient endpoints (once per install in practice; clients cache by hash).
    audit('dataset.asset.fetch', _actor(), assetHash=asset_hash)
    return jsonify({'asset': asset}), 200


# ── items ────────────────────────────────────────────────────────────────────

@datasets_bp.route('/<dataset_id>/items', methods=['POST'])
@api_key_required
@_handles('Dataset item add')
def add_items(dataset_id):
    labeler = _labeler()
    result = get_dataset_service().add_items(
        dataset_id, _body().get('items'), labeler,
    )
    audit('dataset.items.add', _actor(), labeler=labeler,
          datasetId=dataset_id, added=len(result['added']),
          skipped=len(result['skipped']))
    return jsonify(result), 200


@datasets_bp.route('/<dataset_id>/items', methods=['GET'])
@api_key_required
@_handles('Dataset item list')
def list_items(dataset_id):
    return jsonify(get_dataset_service().list_items(dataset_id)), 200


@datasets_bp.route('/items/<item_id>', methods=['GET'])
@api_key_required
@_handles('Dataset item fetch')
def get_item(item_id):
    return jsonify({'item': get_dataset_service().get_item(item_id)}), 200


@datasets_bp.route('/items/<item_id>/labels', methods=['PUT'])
@api_key_required
@_handles('Dataset label save')
def save_labels(item_id):
    labeler = _labeler()
    data = _body()
    item = get_dataset_service().save_labels(
        item_id, data.get('baseVersion'), data.get('image'),
        data.get('masks'), data.get('tasksChanged'), labeler,
    )
    audit('dataset.item.labels', _actor(), labeler=labeler, itemId=item_id,
          tasksChanged=data.get('tasksChanged') or [])
    return jsonify({'item': item}), 200


@datasets_bp.route('/items/<item_id>', methods=['PATCH'])
@api_key_required
@_handles('Dataset item update')
def patch_item(item_id):
    labeler = _labeler()
    data = _body()
    item = get_dataset_service().patch_item(
        item_id, data.get('baseVersion'), data, labeler,
    )
    audit('dataset.item.update', _actor(), labeler=labeler, itemId=item_id,
          status=item.get('status'), subject=item.get('subject'))
    return jsonify({'item': item}), 200


@datasets_bp.route('/items/<item_id>', methods=['DELETE'])
@api_key_required
@_handles('Dataset item delete')
def delete_item(item_id):
    labeler = _labeler()
    get_dataset_service().delete_item(item_id, labeler)
    audit('dataset.item.delete', _actor(), labeler=labeler, itemId=item_id)
    return jsonify({'ok': True}), 200


@datasets_bp.route('/items/<item_id>/events', methods=['GET'])
@api_key_required
@_handles('Dataset item events')
def item_events(item_id):
    return jsonify(get_dataset_service().item_events(item_id)), 200


# ── Export ledger (shared version numbers) ───────────────────────────────────

@datasets_bp.route('/<dataset_id>/exports', methods=['POST'])
@api_key_required
@_handles('Dataset export record')
def record_export(dataset_id):
    labeler = _labeler()
    data = _body()
    result = get_dataset_service().record_export(
        dataset_id, data.get('itemCount'), data.get('filters'), labeler,
    )
    audit('dataset.export.record', _actor(), labeler=labeler,
          datasetId=dataset_id, version=result['version'],
          itemCount=data.get('itemCount'))
    return jsonify(result), 201


@datasets_bp.route('/<dataset_id>/exports', methods=['GET'])
@api_key_required
@_handles('Dataset export list')
def list_exports(dataset_id):
    return jsonify(get_dataset_service().list_exports(dataset_id)), 200
