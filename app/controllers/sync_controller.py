from flask import Blueprint, request, jsonify, current_app, g

from app.services.sync_service import SyncService, ImageTooLargeError
from app.utils.middleware import token_required

# Mounted under /v1/sync by create_app.
sync_bp = Blueprint('sync', __name__)


def get_sync_service():
    if 'sync_service' not in g:
        g.sync_service = SyncService(current_app.config['DB'])
    return g.sync_service


@sync_bp.route('/sync1', methods=['POST'])
@token_required
def sync_step_1(current_user):
    try:
        data = request.get_json()

        if not data:
            return jsonify({'message': 'No data provided'}), 400

        user_id = str(current_user._id)

        sync_service = get_sync_service()
        result = sync_service.sync_step_1(user_id, data)

        return jsonify(result), 200

    except Exception:
        current_app.logger.exception('Sync step 1 failed')
        return jsonify({'message': 'Sync step 1 failed'}), 500


@sync_bp.route('/sync2', methods=['POST'])
@token_required
def sync_step_2(current_user):
    try:
        data = request.get_json()

        if not data:
            return jsonify({'message': 'No data provided'}), 400

        user_id = str(current_user._id)

        sync_service = get_sync_service()
        result = sync_service.sync_step_2(user_id, data)

        return jsonify(result), 200

    except Exception:
        current_app.logger.exception('Sync step 2 failed')
        return jsonify({'message': 'Sync step 2 failed'}), 500


@sync_bp.route('/images/upload', methods=['POST'])
@token_required
def upload_image(current_user):
    try:
        data = request.get_json()
        image = data.get('image', None)

        if not image:
            return jsonify({'message': 'No image provided'}), 400

        user_id = str(current_user._id)

        sync_service = get_sync_service()
        result = sync_service.upload_image(user_id, image)

        return jsonify(result), 200

    except ImageTooLargeError as e:
        return jsonify({'message': str(e)}), 413
    except Exception:
        current_app.logger.exception('Image upload failed')
        return jsonify({'message': 'Image upload failed'}), 500


@sync_bp.route('/images/download/<image_id>', methods=['GET'])
@token_required
def download_image(current_user, image_id):
    try:
        if not image_id:
            return jsonify({'message': 'No image ID provided'}), 400

        user_id = str(current_user._id)

        sync_service = get_sync_service()
        images = sync_service.download_images(user_id, [image_id])

        if not images:
            return jsonify({'message': 'Image not found'}), 404

        return jsonify({'image': images[0]}), 200

    except Exception:
        current_app.logger.exception('Image download failed')
        return jsonify({'message': 'Image download failed'}), 500


@sync_bp.route('/delete', methods=['DELETE'])
@token_required
def mark_as_delete(current_user):
    try:
        data = request.get_json()
        if not data:
            return jsonify({'message': 'No data provided'}), 400

        collection_key = data.get('collection_key')
        ids = data.get('ids')

        if not collection_key or not isinstance(ids, list) or not ids:
            return jsonify({'message': 'collection_key and ids are required'}), 400

        user_id = str(current_user._id)

        sync_service = get_sync_service()
        result = sync_service.mark_delete(user_id, collection_key, ids)

        return jsonify(result), 200

    except ValueError as e:
        return jsonify({'message': str(e)}), 400

    except Exception:
        current_app.logger.exception('Delete failed')
        return jsonify({'message': 'Delete failed'}), 500