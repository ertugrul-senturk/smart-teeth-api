from flask import Blueprint, request, jsonify, current_app, g

from app.services.sync_service import SyncService, ImageService
from app.utils.middleware import token_required

sync_bp = Blueprint('sync', __name__, url_prefix='/sync')


def get_sync_service():
    if 'sync_service' not in g:
        g.sync_service = SyncService(current_app.config['DB'])
    return g.sync_service


def get_image_service():
    if 'image_service' not in g:
        g.image_service = ImageService(current_app.config['DB'])
    return g.image_service


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

    except Exception as e:
        return jsonify({
            'message': 'Sync step 1 failed',
            'error': str(e)
        }), 500


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

    except Exception as e:
        return jsonify({
            'message': 'Sync step 2 failed',
            'error': str(e)
        }), 500


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

    except Exception as e:
        return jsonify({
            'message': 'Image upload failed',
            'error': str(e)
        }), 500


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

    except Exception as e:
        return jsonify({
            'message': 'Image download failed',
            'error': str(e)
        }), 500


@sync_bp.route('/delete', methods=['DELETE'])
@token_required
def mark_as_delete(current_user):
    try:
        data = request.get_json()
        collection_key = data.get('collection_key'),
        ids = data.get('ids')
        if not data:
            return jsonify({'message': 'No data provided'}), 400

        user_id = str(current_user._id)

        sync_service = get_sync_service()
        result = sync_service.mark_delete(user_id, collection_key, ids)

        return jsonify(result), 200

    except Exception as e:
        return jsonify({
            'message': 'Items deleted',
            'error': str(e)
        }), 500