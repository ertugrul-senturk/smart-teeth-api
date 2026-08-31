"""Registration key administration (/v1/admin/keys) and the DB-backed keys
accepted by the desktop endpoints."""

from datetime import datetime, timedelta, timezone

from tests.conftest import MASTER_KEY, unique


def _iso(dt):
    return dt.isoformat().replace('+00:00', 'Z')


def _window(days=365):
    now = datetime.now(timezone.utc)
    return _iso(now), _iso(now + timedelta(days=days))


def _create_key(client, master_headers, name=None, **overrides):
    starts, expires = _window()
    payload = {'name': name or unique('clinic'), 'startsAt': starts, 'expiresAt': expires}
    payload.update(overrides)
    return client.post('/v1/admin/keys', json=payload, headers=master_headers)


# ── Master key gate ─────────────────────────────────────────────────────────

def test_admin_requires_master_key(client):
    assert client.get('/v1/admin/keys').status_code == 401
    assert client.get('/v1/admin/keys', headers={'X-Master-Key': 'wrong'}).status_code == 401


def test_admin_ping_with_master_key(client, master_headers):
    response = client.get('/v1/admin/ping', headers=master_headers)
    assert response.status_code == 200
    assert response.get_json()['ok'] is True


def test_master_key_is_also_a_desktop_api_key(client):
    """The desktop app has one key field — entering the master key must work
    as a regular key (with admin unlocked on top)."""
    response = client.get('/v1/desktop/ping', headers={'X-API-Key': MASTER_KEY})
    assert response.status_code == 200
    assert response.get_json()['keyName'] == 'master'


# ── Creation & validation ───────────────────────────────────────────────────

def test_create_key_returns_license_formatted_key(client, master_headers):
    response = _create_key(client, master_headers, name='Clinic A')
    assert response.status_code == 201, response.get_json()
    body = response.get_json()

    groups = body['key'].split('-')
    assert len(groups) == 5 and all(len(g) == 5 for g in groups)
    assert body['name'] == 'Clinic A'
    assert body['status'] == 'active'
    assert body['keyHint'] == groups[-1]
    assert body['source'] == 'managed'


def test_create_key_rejects_missing_name(client, master_headers):
    response = _create_key(client, master_headers, name='  ')
    assert response.status_code == 400


def test_create_key_rejects_past_start_date(client, master_headers):
    starts = _iso(datetime.now(timezone.utc) - timedelta(days=2))
    response = _create_key(client, master_headers, startsAt=starts)
    assert response.status_code == 400
    assert 'past' in response.get_json()['message'].lower()


def test_create_key_rejects_end_before_start(client, master_headers):
    now = datetime.now(timezone.utc)
    response = _create_key(client, master_headers,
                           startsAt=_iso(now + timedelta(days=2)),
                           expiresAt=_iso(now + timedelta(days=1)))
    assert response.status_code == 400


def test_create_key_rejects_garbage_dates(client, master_headers):
    response = _create_key(client, master_headers, startsAt='not-a-date')
    assert response.status_code == 400


# ── Using a created key on desktop endpoints ────────────────────────────────

def test_created_key_authenticates_desktop_ping(client, master_headers):
    key = _create_key(client, master_headers).get_json()
    response = client.get('/v1/desktop/ping', headers={
        'X-API-Key': key['key'],
        'X-Device-Id': 'device-A',
        'X-Labeler-Name': 'Dr%20Alice',
    })
    assert response.status_code == 200
    assert response.get_json()['keyName'] == key['name']


def test_ping_records_installs_and_sessions(client, master_headers):
    key = _create_key(client, master_headers).get_json()

    for device, name in (('device-A', 'Dr%20Alice'), ('device-A', 'Dr%20Alice'),
                         ('device-B', 'Dr%20Bob')):
        assert client.get('/v1/desktop/ping', headers={
            'X-API-Key': key['key'], 'X-Device-Id': device, 'X-Labeler-Name': name,
        }).status_code == 200

    details = client.get(f"/v1/admin/keys/{key['id']}", headers=master_headers).get_json()
    assert details['deviceCount'] == 2
    assert details['sessionCount'] == 3
    assert details['userNames'] == ['Dr Alice', 'Dr Bob']

    by_device = {i['deviceId']: i for i in details['installs']}
    assert by_device['device-A']['sessionCount'] == 2
    assert by_device['device-A']['userName'] == 'Dr Alice'


def test_registration_pings_count_installations_separately(client, master_headers):
    key = _create_key(client, master_headers).get_json()

    def ping(device, registration):
        headers = {'X-API-Key': key['key'], 'X-Device-Id': device,
                   'X-Labeler-Name': 'Dr%20Alice'}
        if registration:
            headers['X-Registration'] = '1'
        assert client.get('/v1/desktop/ping', headers=headers).status_code == 200

    ping('device-A', registration=True)    # initial install
    ping('device-A', registration=False)   # routine app start
    ping('device-A', registration=True)    # re-registered (reinstall)
    ping('device-B', registration=False)   # legacy client: never marks

    details = client.get(f"/v1/admin/keys/{key['id']}", headers=master_headers).get_json()
    assert details['sessionCount'] == 4
    assert details['registrationCount'] == 2

    by_device = {i['deviceId']: i for i in details['installs']}
    assert by_device['device-A']['registrationCount'] == 2
    assert by_device['device-A']['lastRegisteredAt'] is not None
    assert by_device['device-B']['registrationCount'] == 0
    assert by_device['device-B']['lastRegisteredAt'] is None


def test_scheduled_key_is_rejected_until_start(client, master_headers):
    now = datetime.now(timezone.utc)
    key = _create_key(client, master_headers,
                      startsAt=_iso(now + timedelta(days=30)),
                      expiresAt=_iso(now + timedelta(days=60))).get_json()
    response = client.get('/v1/desktop/ping',
                          headers={'X-API-Key': key['key'], 'X-Device-Id': 'd1'})
    assert response.status_code == 401
    assert 'not active yet' in response.get_json()['message']


# ── List ────────────────────────────────────────────────────────────────────

def test_list_includes_only_managed_keys(client, master_headers):
    created = _create_key(client, master_headers).get_json()
    keys = client.get('/v1/admin/keys', headers=master_headers).get_json()['keys']

    by_id = {k['id']: k for k in keys}
    assert created['id'] in by_id
    assert 'key' not in by_id[created['id']]  # plaintext never appears in lists
    assert all(k['source'] == 'managed' for k in keys)  # legacy env keys are gone


# ── Expire / reveal / update / delete ───────────────────────────────────────

def test_expire_key_immediately_blocks_access(client, master_headers):
    key = _create_key(client, master_headers).get_json()
    response = client.post(f"/v1/admin/keys/{key['id']}/expire", headers=master_headers)
    assert response.status_code == 200
    assert response.get_json()['status'] == 'expired'

    response = client.get('/v1/desktop/ping',
                          headers={'X-API-Key': key['key'], 'X-Device-Id': 'd1'})
    assert response.status_code == 401
    assert 'expired' in response.get_json()['message']


def test_extending_expired_key_reactivates_it(client, master_headers):
    key = _create_key(client, master_headers).get_json()
    client.post(f"/v1/admin/keys/{key['id']}/expire", headers=master_headers)

    new_end = _iso(datetime.now(timezone.utc) + timedelta(days=90))
    response = client.patch(f"/v1/admin/keys/{key['id']}",
                            json={'expiresAt': new_end}, headers=master_headers)
    assert response.status_code == 200
    assert response.get_json()['status'] == 'active'

    assert client.get('/v1/desktop/ping', headers={
        'X-API-Key': key['key'], 'X-Device-Id': 'd1'}).status_code == 200


def test_reveal_returns_the_original_key(client, master_headers):
    key = _create_key(client, master_headers).get_json()
    response = client.get(f"/v1/admin/keys/{key['id']}/reveal", headers=master_headers)
    assert response.status_code == 200
    assert response.get_json()['key'] == key['key']


def test_delete_removes_key_and_installs(client, master_headers, db):
    key = _create_key(client, master_headers).get_json()
    client.get('/v1/desktop/ping', headers={
        'X-API-Key': key['key'], 'X-Device-Id': 'd1', 'X-Labeler-Name': 'Dr%20Zed'})

    response = client.delete(f"/v1/admin/keys/{key['id']}", headers=master_headers)
    assert response.status_code == 200

    assert client.get(f"/v1/admin/keys/{key['id']}", headers=master_headers).status_code == 404
    assert client.get('/v1/desktop/ping', headers={
        'X-API-Key': key['key'], 'X-Device-Id': 'd1'}).status_code == 401
    assert db['key_installs'].count_documents({'keyRef': key['id']}) == 0


def test_unknown_key_id_is_404(client, master_headers):
    assert client.get('/v1/admin/keys/ffffffffffffffffffffffff',
                      headers=master_headers).status_code == 404
    assert client.get('/v1/admin/keys/not-an-oid',
                      headers=master_headers).status_code == 404
