import os
import tempfile
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, current_app, g, send_file, Response

from app.services.desktop_service import DesktopSyncService
from app.services.export_service import ExportService
from app.services.sync_service import ImageTooLargeError
from app.utils.middleware import api_key_required
from app.utils.audit import audit
from app.utils.download_token import make_download_token, verify_download_token

desktop_bp = Blueprint('desktop', __name__)


def get_desktop_service():
    if 'desktop_service' not in g:
        g.desktop_service = DesktopSyncService(current_app.config['DB'])
    return g.desktop_service


def _actor():
    """Audit identity: API key name + the caller's device id (if sent)."""
    device = request.headers.get('X-Device-Id')
    key_name = getattr(g, 'api_key_name', 'unknown')
    return f"desktop:{key_name}/{device}" if device else f"desktop:{key_name}"


def _parse_date_arg(name):
    """ISO date/datetime query arg → aware UTC datetime (or None).
    Raises ValueError with a client-friendly message on garbage input."""
    raw = request.args.get(name)
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError(f"Invalid '{name}' date: {raw!r} (expected ISO 8601)")
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@desktop_bp.route('/ping', methods=['GET'])
@api_key_required
def ping():
    """Authenticated probe the desktop app calls on registration and on every
    app start. Doubles as the session counter behind the admin tab: each ping
    upserts the (key, device) install row with the caller's labeler name.
    Recording must never break the probe itself."""
    device_id = request.headers.get('X-Device-Id')
    # Master-key sessions aren't recorded — the install ledger tracks the
    # managed registration keys the admin hands out, not the admin's own use.
    if device_id and getattr(g, 'registration_key', None) is not None:
        try:
            from urllib.parse import unquote
            from app.repositories.registration_key_repository import RegistrationKeyRepository

            labeler = unquote(request.headers.get('X-Labeler-Name') or '').strip()
            # The register-with-code flow marks its probe so key installations
            # are countable separately from routine app-start sessions.
            is_registration = request.headers.get('X-Registration') == '1'
            app_version = (request.headers.get('X-App-Version') or '').strip()
            repo = RegistrationKeyRepository(current_app.config['DB'])
            key_ref = RegistrationKeyRepository.key_ref(g.registration_key)
            repo.record_session(key_ref, g.api_key_name, device_id, labeler or None,
                                registration=is_registration,
                                app_version=app_version or None)
        except Exception:
            current_app.logger.exception('Failed to record key session')
    return jsonify({'ok': True, 'keyName': g.api_key_name}), 200


# ── OTA updates ──────────────────────────────────────────────────────────────

@desktop_bp.route('/updates/<target>/<arch>/<current_version>', methods=['GET'])
@api_key_required
def check_update(target, arch, current_version):
    """Tauri updater endpoint: 204 when the device is current, otherwise the
    dynamic-server JSON contract (version / pub_date / url / signature /
    notes). The download URL carries a short-lived signed token instead of
    relying on auth headers — see app/utils/download_token.py."""
    from app.repositories.update_repository import UpdateRepository

    platform = f'{target}-{arch}'
    repo = UpdateRepository(current_app.config['DB'])
    release = repo.latest_for(platform, current_version)
    if release is None:
        return '', 204

    entry = release['platforms'][platform]
    expires, token = make_download_token(entry['assetId'])
    pub_date = release.get('pubDate')
    if pub_date is not None and pub_date.tzinfo is None:
        pub_date = pub_date.replace(tzinfo=timezone.utc)
    return jsonify({
        'version': release['version'],
        'pub_date': pub_date.isoformat().replace('+00:00', 'Z') if pub_date else None,
        'url': (f"{request.url_root.rstrip('/')}/v1/desktop/updates/download/"
                f"{entry['assetId']}?e={expires}&t={token}"),
        'signature': entry['signature'],
        'notes': release.get('notes') or '',
    }), 200


@desktop_bp.route('/updates/download/<asset_id>', methods=['GET'])
def download_update(asset_id):
    """Streamed installer download, authenticated by the signed token issued
    by check_update (no API key header — the updater's download request is
    not guaranteed to carry custom headers)."""
    from app.repositories.update_repository import UpdateRepository

    if not verify_download_token(asset_id, request.args.get('e'), request.args.get('t')):
        return jsonify({'message': 'Invalid or expired download link'}), 403

    repo = UpdateRepository(current_app.config['DB'])
    asset = repo.get_asset(asset_id)
    if asset is None or not asset.get('complete'):
        return jsonify({'message': 'Update file not found'}), 404

    repo.count_download(asset_id)
    audit('desktop.update_downloaded', 'desktop:updater',
          assetId=asset_id, filename=asset['filename'])
    return Response(
        repo.iter_chunks(asset['_id']),
        mimetype='application/octet-stream',
        headers={
            'Content-Length': str(asset['size']),
            'Content-Disposition': f"attachment; filename=\"{asset['filename']}\"",
        },
    )


@desktop_bp.route('/sync', methods=['POST'])
@api_key_required
def sync_entries():
    try:
        data = request.get_json()

        if not data:
            return jsonify({'message': 'No data provided'}), 400

        device_id = data.get('deviceId')
        entries = data.get('entries')

        if not device_id or not isinstance(device_id, str):
            return jsonify({'message': 'deviceId is required'}), 400

        if not isinstance(entries, list):
            return jsonify({'message': 'entries must be a list'}), 400

        desktop_service = get_desktop_service()
        result = desktop_service.sync_entries(device_id, entries)

        return jsonify(result), 200

    except Exception:
        current_app.logger.exception('Desktop sync failed')
        return jsonify({'message': 'Desktop sync failed'}), 500


@desktop_bp.route('/images/upload', methods=['POST'])
@api_key_required
def upload_images():
    try:
        data = request.get_json()

        if not data:
            return jsonify({'message': 'No data provided'}), 400

        device_id = data.get('deviceId')
        images = data.get('images')

        if not device_id or not isinstance(device_id, str):
            return jsonify({'message': 'deviceId is required'}), 400

        if not isinstance(images, list) or not images:
            return jsonify({'message': 'images must be a non-empty list'}), 400

        desktop_service = get_desktop_service()
        result = desktop_service.upload_images(device_id, images)

        return jsonify(result), 200

    except ImageTooLargeError as e:
        return jsonify({'message': str(e)}), 413
    except Exception:
        current_app.logger.exception('Desktop image upload failed')
        return jsonify({'message': 'Desktop image upload failed'}), 500


# ── Patient browsing: dentist access to mobile app data ─────────────────────

def _parse_page_args():
    """Optional pagination args: (page, pageSize) — (None, None) when the
    caller didn't paginate (legacy full-list clients). Clamped so a bad
    client can't ask the server to build an absurd page."""
    raw_page = request.args.get('page')
    if raw_page is None:
        return None, None
    try:
        page = max(int(raw_page), 1)
        page_size = min(max(int(request.args.get('pageSize', '50')), 1), 200)
    except ValueError:
        raise ValueError('page and pageSize must be integers')
    return page, page_size


@desktop_bp.route('/patients', methods=['GET'])
@api_key_required
def list_patients():
    try:
        page, page_size = _parse_page_args()
        search = request.args.get('search') or None
        result = get_desktop_service().list_patients(search, page, page_size)
        audit('patients.list', _actor(), count=len(result['patients']),
              search=bool(search), page=page)
        return jsonify(result), 200
    except ValueError as e:
        return jsonify({'message': str(e)}), 400
    except Exception:
        current_app.logger.exception('Patient list failed')
        return jsonify({'message': 'Patient list failed'}), 500


@desktop_bp.route('/patients/<user_id>/records', methods=['GET'])
@api_key_required
def patient_records(user_id):
    try:
        collection_key = request.args.get('collection')
        if not collection_key:
            return jsonify({'message': 'collection query parameter is required'}), 400

        date_from = _parse_date_arg('from')
        date_to = _parse_date_arg('to')
        page, page_size = _parse_page_args()
        include_trend = request.args.get('includeTrend') in ('1', 'true')

        result = get_desktop_service().patient_records(
            user_id, collection_key, date_from, date_to,
            page=page, page_size=page_size, include_trend=include_trend,
        )
        audit('patient.records.read', _actor(),
              patientId=user_id, collection=collection_key,
              count=len(result['records']))
        return jsonify(result), 200

    except ValueError as e:
        return jsonify({'message': str(e)}), 400
    except LookupError as e:
        return jsonify({'message': str(e)}), 404
    except Exception:
        current_app.logger.exception('Patient records read failed')
        return jsonify({'message': 'Patient records read failed'}), 500


@desktop_bp.route('/patients/<user_id>/images/fetch', methods=['POST'])
@api_key_required
def fetch_patient_images(user_id):
    try:
        data = request.get_json(silent=True) or {}
        image_ids = data.get('imageIds')
        if not isinstance(image_ids, list) or not image_ids:
            return jsonify({'message': 'imageIds must be a non-empty list'}), 400

        result = get_desktop_service().fetch_patient_images(user_id, image_ids)
        audit('patient.images.fetch', _actor(),
              patientId=user_id, requested=len(image_ids),
              returned=len(result['images']))
        return jsonify(result), 200

    except LookupError as e:
        return jsonify({'message': str(e)}), 404
    except Exception:
        current_app.logger.exception('Patient image fetch failed')
        return jsonify({'message': 'Patient image fetch failed'}), 500


@desktop_bp.route('/patients/<user_id>', methods=['DELETE'])
@api_key_required
def delete_patient(user_id):
    """Permanently erase a patient's synced data (records, images, import
    links). With deleteAccount=true the mobile account itself is removed too,
    including any archived copy. Irreversible; desktop app only."""
    try:
        data = request.get_json(silent=True) or {}
        delete_account = bool(data.get('deleteAccount'))

        result = get_desktop_service().delete_patient(user_id, delete_account)
        audit('patient.delete', _actor(),
              patientId=user_id, deleteAccount=delete_account,
              removed=result['removed'])
        return jsonify(result), 200

    except LookupError as e:
        return jsonify({'message': str(e)}), 404
    except Exception:
        current_app.logger.exception('Patient delete failed')
        return jsonify({'message': 'Patient delete failed'}), 500


@desktop_bp.route('/patients/<user_id>/records', methods=['DELETE'])
@api_key_required
def delete_patient_records(user_id):
    """Permanently erase selected records of a patient plus the images they
    reference. Irreversible; desktop app only."""
    try:
        data = request.get_json(silent=True) or {}
        collection_key = data.get('collection')
        record_ids = data.get('recordIds')
        if not collection_key:
            return jsonify({'message': 'collection is required'}), 400
        if (not isinstance(record_ids, list) or not record_ids
                or not all(isinstance(r, str) and r for r in record_ids)):
            return jsonify({'message': 'recordIds must be a non-empty list of ids'}), 400

        result = get_desktop_service().delete_patient_records(
            user_id, collection_key, record_ids
        )
        audit('patient.records.delete', _actor(),
              patientId=user_id, collection=collection_key,
              requested=len(record_ids), removed=result['removed'])
        return jsonify(result), 200

    except ValueError as e:
        return jsonify({'message': str(e)}), 400
    except LookupError as e:
        return jsonify({'message': str(e)}), 404
    except Exception:
        current_app.logger.exception('Patient records delete failed')
        return jsonify({'message': 'Patient records delete failed'}), 500


@desktop_bp.route('/import-link', methods=['POST'])
@api_key_required
def register_import_link():
    try:
        data = request.get_json(silent=True) or {}
        required = ['sourceCollection', 'sourceUserId', 'sourceRecordId',
                    'deviceId', 'desktopEntryId']
        missing = [f for f in required if not data.get(f)]
        if missing:
            return jsonify({'message': f"Missing fields: {', '.join(missing)}"}), 400

        result = get_desktop_service().register_import(
            data['sourceCollection'], data['sourceUserId'], data['sourceRecordId'],
            data['deviceId'], data['desktopEntryId'],
            key_name=getattr(g, 'api_key_name', None),
        )
        audit('patient.record.import', _actor(),
              patientId=data['sourceUserId'],
              collection=data['sourceCollection'],
              recordId=data['sourceRecordId'],
              desktopEntryId=data['desktopEntryId'],
              deviceId=data['deviceId'])
        return jsonify(result), 201

    except ValueError as e:
        return jsonify({'message': str(e)}), 400
    except LookupError as e:
        return jsonify({'message': str(e)}), 404
    except Exception:
        current_app.logger.exception('Import link registration failed')
        return jsonify({'message': 'Import link registration failed'}), 500


# ── Dataset export ───────────────────────────────────────────────────────────

def _parse_body_date(data, name):
    raw = data.get(name)
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
    except ValueError:
        raise ValueError(f"Invalid '{name}' date: {raw!r} (expected ISO 8601)")
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@desktop_bp.route('/export', methods=['POST'])
@api_key_required
def export_dataset():
    tmp_path = None
    try:
        data = request.get_json(silent=True) or {}

        source = data.get('source') or 'both'
        date_from = _parse_body_date(data, 'from')
        date_to = _parse_body_date(data, 'to')
        patient_ids = data.get('patientIds') or None
        device_ids = data.get('deviceIds') or None
        collections = data.get('collections') or None
        anonymize = bool(data.get('anonymize', False))
        include_images = bool(data.get('includeImages', True))

        # Build into a temp file (datasets can be far larger than RAM-safe
        # response buffers), stream it, delete after the response closes.
        fd, tmp_path = tempfile.mkstemp(suffix='.zip', prefix='st-export-')
        os.close(fd)

        manifest = ExportService(current_app.config['DB']).build_dataset(
            tmp_path,
            source=source, date_from=date_from, date_to=date_to,
            patient_ids=patient_ids, device_ids=device_ids,
            collections=collections, anonymize=anonymize,
            include_images=include_images,
        )

        audit('dataset.export', _actor(),
              params=manifest['params'], counts=manifest['counts'],
              skipped=len(manifest['skipped']))

        stamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
        response = send_file(
            tmp_path,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f'smart-teeth-dataset_{stamp}.zip',
            conditional=False,
        )

        cleanup_path = tmp_path
        tmp_path = None  # ownership handed to the response

        @response.call_on_close
        def _cleanup():
            try:
                os.remove(cleanup_path)
            except OSError:
                pass

        return response

    except ValueError as e:
        return jsonify({'message': str(e)}), 400
    except Exception:
        current_app.logger.exception('Dataset export failed')
        return jsonify({'message': 'Dataset export failed'}), 500
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
