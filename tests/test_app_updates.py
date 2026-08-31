"""OTA update publishing (/v1/admin/updates) and the device-facing
check/download endpoints (/v1/desktop/updates)."""

import hashlib

import pytest

from tests.conftest import API_KEY


CHUNK = 1024 * 64


@pytest.fixture()
def clean_updates(db):
    """The update check scans every active release, so tests that assert on
    check results must not see releases published by other tests."""
    for name in ('app_updates', 'update_assets', 'update_asset_chunks'):
        db[name].delete_many({})


def _upload_asset(client, master_headers, payload=b'installer-bytes' * 5000,
                  filename='SmartTeeth_2.1.0_x64-setup.exe'):
    chunks = [payload[i:i + CHUNK] for i in range(0, len(payload), CHUNK)]
    response = client.post('/v1/admin/updates/assets', json={
        'filename': filename,
        'size': len(payload),
        'sha256': hashlib.sha256(payload).hexdigest(),
        'chunkCount': len(chunks),
    }, headers=master_headers)
    assert response.status_code == 201, response.get_json()
    asset_id = response.get_json()['id']

    for seq, chunk in enumerate(chunks):
        response = client.put(f'/v1/admin/updates/assets/{asset_id}/chunks/{seq}',
                              data=chunk, headers=master_headers)
        assert response.status_code == 200, response.get_json()

    response = client.post(f'/v1/admin/updates/assets/{asset_id}/complete',
                           headers=master_headers)
    assert response.status_code == 200, response.get_json()
    return asset_id, payload


def _publish(client, master_headers, version, asset_id,
             platform='windows-x86_64', notes='Bug fixes'):
    return client.post('/v1/admin/updates', json={
        'version': version,
        'notes': notes,
        'platforms': {platform: {'assetId': asset_id, 'signature': 'sig-base64'}},
    }, headers=master_headers)


def _check(client, current='2.0.0', target='windows', arch='x86_64'):
    return client.get(f'/v1/desktop/updates/{target}/{arch}/{current}',
                      headers={'X-API-Key': API_KEY, 'X-Device-Id': 'test-device-1'})


# ── Auth gates ───────────────────────────────────────────────────────────────

def test_update_admin_requires_master_key(client):
    assert client.get('/v1/admin/updates').status_code == 401
    assert client.post('/v1/admin/updates/assets', json={}).status_code == 401


def test_update_check_requires_api_key(client):
    assert client.get('/v1/desktop/updates/windows/x86_64/1.0.0').status_code == 401


# ── Asset upload ─────────────────────────────────────────────────────────────

def test_complete_rejects_missing_chunks(client, master_headers):
    response = client.post('/v1/admin/updates/assets', json={
        'filename': 'a.exe', 'size': 10, 'sha256': '0' * 64, 'chunkCount': 2,
    }, headers=master_headers)
    asset_id = response.get_json()['id']
    client.put(f'/v1/admin/updates/assets/{asset_id}/chunks/0',
               data=b'12345', headers=master_headers)

    response = client.post(f'/v1/admin/updates/assets/{asset_id}/complete',
                           headers=master_headers)
    assert response.status_code == 400
    assert 'incomplete' in response.get_json()['message'].lower()


def test_complete_rejects_checksum_mismatch(client, master_headers):
    response = client.post('/v1/admin/updates/assets', json={
        'filename': 'a.exe', 'size': 5, 'sha256': 'f' * 64, 'chunkCount': 1,
    }, headers=master_headers)
    asset_id = response.get_json()['id']
    client.put(f'/v1/admin/updates/assets/{asset_id}/chunks/0',
               data=b'12345', headers=master_headers)

    response = client.post(f'/v1/admin/updates/assets/{asset_id}/complete',
                           headers=master_headers)
    assert response.status_code == 400
    assert 'corrupt' in response.get_json()['message'].lower()


# ── Publishing ───────────────────────────────────────────────────────────────

def test_publish_validates_input(client, master_headers):
    asset_id, _ = _upload_asset(client, master_headers)

    assert _publish(client, master_headers, 'not-a-version', asset_id).status_code == 400
    assert _publish(client, master_headers, '9.0.0', asset_id,
                    platform='amiga-68k').status_code == 400

    response = client.post('/v1/admin/updates', json={
        'version': '9.0.1', 'platforms': {
            'windows-x86_64': {'assetId': asset_id, 'signature': ''}},
    }, headers=master_headers)
    assert response.status_code == 400

    assert _publish(client, master_headers, '9.1.0', asset_id).status_code == 201
    assert _publish(client, master_headers, '9.1.0', asset_id).status_code == 409


# ── Device check + download ──────────────────────────────────────────────────

def test_check_and_download_roundtrip(client, master_headers, clean_updates):
    asset_id, payload = _upload_asset(client, master_headers)
    assert _publish(client, master_headers, '2.1.0', asset_id).status_code == 201

    response = _check(client, current='2.0.0')
    assert response.status_code == 200
    body = response.get_json()
    assert body['version'] == '2.1.0'
    assert body['signature'] == 'sig-base64'
    assert body['notes'] == 'Bug fixes'
    assert '/v1/desktop/updates/download/' in body['url']

    # The download URL is self-authenticating — no headers needed.
    path = body['url'].split('//', 1)[1].split('/', 1)[1]
    download = client.get(f'/{path}')
    assert download.status_code == 200
    assert download.data == payload
    assert int(download.headers['Content-Length']) == len(payload)

    # Download count lands in the admin release list.
    releases = client.get('/v1/admin/updates', headers=master_headers).get_json()['releases']
    release = next(r for r in releases if r['version'] == '2.1.0')
    assert release['platforms']['windows-x86_64']['downloadCount'] == 1


def test_check_returns_204_when_current_or_no_platform(client, master_headers, clean_updates):
    asset_id, _ = _upload_asset(client, master_headers)
    _publish(client, master_headers, '3.0.0', asset_id)

    assert _check(client, current='3.0.0').status_code == 204   # same version
    assert _check(client, current='3.5.0').status_code == 204   # device is newer
    assert _check(client, current='2.0.0', target='darwin',
                  arch='aarch64').status_code == 204            # platform not shipped


def test_check_picks_newest_active_release(client, master_headers, clean_updates):
    a1, _ = _upload_asset(client, master_headers)
    a2, _ = _upload_asset(client, master_headers, payload=b'v2' * 100,
                          filename='SmartTeeth_4.1.0_x64-setup.exe')
    _publish(client, master_headers, '4.0.0', a1)
    newer = _publish(client, master_headers, '4.1.0', a2).get_json()

    assert _check(client, current='2.0.0').get_json()['version'] == '4.1.0'

    # Deactivating the newest rolls devices back to the next active one.
    response = client.patch(f"/v1/admin/updates/{newer['id']}",
                            json={'active': False}, headers=master_headers)
    assert response.status_code == 200
    assert _check(client, current='2.0.0').get_json()['version'] == '4.0.0'


def test_download_rejects_bad_token(client, master_headers, clean_updates):
    asset_id, _ = _upload_asset(client, master_headers)
    _publish(client, master_headers, '5.0.0', asset_id)
    url = _check(client, current='2.0.0').get_json()['url']
    path = url.split('//', 1)[1].split('/', 1)[1]

    tampered = f'/{path[:-4]}0000'
    assert client.get(tampered).status_code == 403
    assert client.get(f'/v1/desktop/updates/download/{asset_id}').status_code == 403


def test_expired_download_token_is_rejected(client, master_headers, app):
    from app.utils.download_token import make_download_token
    asset_id, _ = _upload_asset(client, master_headers)
    _publish(client, master_headers, '6.0.0', asset_id)

    expires, token = make_download_token(asset_id, ttl_secs=-10)
    response = client.get(f'/v1/desktop/updates/download/{asset_id}?e={expires}&t={token}')
    assert response.status_code == 403


def test_delete_release_removes_unused_assets(client, master_headers, db):
    asset_id, _ = _upload_asset(client, master_headers)
    release = _publish(client, master_headers, '7.0.0', asset_id).get_json()

    response = client.delete(f"/v1/admin/updates/{release['id']}",
                             headers=master_headers)
    assert response.status_code == 200
    assert db['update_assets'].count_documents({}) == 0 or \
        db['update_asset_chunks'].count_documents({'assetId': asset_id}) == 0


def test_ping_records_app_version(client, master_headers, desktop_headers):
    headers = dict(desktop_headers)
    headers['X-App-Version'] = '2.0.5'
    assert client.get('/v1/desktop/ping', headers=headers).status_code == 200

    keys = client.get('/v1/admin/keys', headers=master_headers).get_json()['keys']
    test_key = next(k for k in keys if k['name'] == 'testkey')
    assert test_key['sessionCount'] >= 1
