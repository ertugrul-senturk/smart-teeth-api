import base64


def _upload(client, user, collection, docs):
    return client.post('/v1/sync/sync2', headers=user['headers'],
                       json={collection: docs})


class TestSyncFlow:
    def test_upload_then_step1_reports_known(self, client, user):
        response = _upload(client, user, 'plan_parent',
                           [{'id': 'p1', 'title': 'Floss'}])
        assert response.status_code == 200
        assert response.get_json()['inserted']['plan_parent'] == ['p1']

        response = client.post('/v1/sync/sync1', headers=user['headers'],
                               json={'plan_parent': ['p1']})
        body = response.get_json()
        assert body['requiredIds'] == {}
        assert body['missingData'] == {}
        assert body['deletedIds'] == {}

    def test_fresh_device_downloads_server_data(self, client, user):
        _upload(client, user, 'plan_baby', [{'id': 'b1', 'goal': 'No sugar'}])

        # Fresh install: no local ids at all
        response = client.post('/v1/sync/sync1', headers=user['headers'],
                               json={'plan_baby': []})
        docs = response.get_json()['missingData']['plan_baby']
        assert [d['id'] for d in docs] == ['b1']
        assert docs[0]['goal'] == 'No sugar'

    def test_upsert_updates_instead_of_duplicating(self, client, user, db):
        _upload(client, user, 'plan_parent', [{'id': 'dup', 'title': 'v1'}])
        _upload(client, user, 'plan_parent', [{'id': 'dup', 'title': 'v2'}])

        docs = list(db['plan_parent'].find({'userId': user['id'], 'id': 'dup'}))
        assert len(docs) == 1
        assert docs[0]['title'] == 'v2'

    def test_unknown_collection_rejected_in_sync2_ignored_in_sync1(self, client, user):
        response = _upload(client, user, 'users', [{'id': 'x', 'eh': 'sneaky'}])
        assert response.get_json()['errors'] == {'users': ['Unknown collection']}

        response = client.post('/v1/sync/sync1', headers=user['headers'],
                               json={'users': ['anything']})
        assert response.status_code == 200
        assert response.get_json() == {
            'missingData': {}, 'requiredIds': {}, 'deletedIds': {}}


class TestTombstones:
    """Deleted records must not resurrect (gap G1)."""

    def test_delete_then_step1_returns_deleted_ids(self, client, user):
        _upload(client, user, 'plan_parent', [{'id': 'doomed', 'title': 'x'}])
        response = client.delete('/v1/sync/delete', headers=user['headers'],
                                 json={'collection_key': 'plan_parent', 'ids': ['doomed']})
        assert response.status_code == 200
        assert response.get_json()['modified'] == 1

        # A device still holding the record is told to purge it — and it is
        # NOT listed in requiredIds (which would trigger a re-upload).
        response = client.post('/v1/sync/sync1', headers=user['headers'],
                               json={'plan_parent': ['doomed']})
        body = response.get_json()
        assert body['deletedIds'] == {'plan_parent': ['doomed']}
        assert body['requiredIds'] == {}
        assert body['missingData'] == {}

    def test_reupload_cannot_revive_deleted_record(self, client, user, db):
        _upload(client, user, 'plan_parent', [{'id': 'zombie', 'title': 'alive'}])
        client.delete('/v1/sync/delete', headers=user['headers'],
                      json={'collection_key': 'plan_parent', 'ids': ['zombie']})

        response = _upload(client, user, 'plan_parent',
                           [{'id': 'zombie', 'title': 'raised from the dead'}])
        body = response.get_json()
        assert body['skipped'] == {'plan_parent': ['zombie']}
        assert 'plan_parent' not in body['inserted']

        doc = db['plan_parent'].find_one({'userId': user['id'], 'id': 'zombie'})
        assert doc['isDeleted'] is True
        assert doc['title'] == 'alive'


class TestConflictGuard:
    """Stale writes must not clobber newer edits (gap G2)."""

    def test_older_edit_is_skipped(self, client, user, db):
        _upload(client, user, 'plan_parent',
                [{'id': 'c1', 'title': 'newer', 'updatedAt': '2026-08-15T12:00:00Z'}])

        response = _upload(client, user, 'plan_parent',
                           [{'id': 'c1', 'title': 'older', 'updatedAt': '2026-08-14T12:00:00Z'}])
        body = response.get_json()
        assert body['skipped'] == {'plan_parent': ['c1']}

        doc = db['plan_parent'].find_one({'userId': user['id'], 'id': 'c1'})
        assert doc['title'] == 'newer'

    def test_newer_edit_wins(self, client, user, db):
        _upload(client, user, 'plan_parent',
                [{'id': 'c2', 'title': 'old', 'updatedAt': '2026-08-14T12:00:00Z'}])
        response = _upload(client, user, 'plan_parent',
                           [{'id': 'c2', 'title': 'new', 'updatedAt': '2026-08-16T12:00:00Z'}])
        assert response.get_json()['inserted']['plan_parent'] == ['c2']

        doc = db['plan_parent'].find_one({'userId': user['id'], 'id': 'c2'})
        assert doc['title'] == 'new'

    def test_no_timestamps_keeps_old_behavior(self, client, user, db):
        _upload(client, user, 'plan_parent', [{'id': 'c3', 'title': 'first'}])
        response = _upload(client, user, 'plan_parent', [{'id': 'c3', 'title': 'second'}])
        assert response.get_json()['inserted']['plan_parent'] == ['c3']
        doc = db['plan_parent'].find_one({'userId': user['id'], 'id': 'c3'})
        assert doc['title'] == 'second'


class TestImages:
    def test_upload_download_roundtrip(self, client, user):
        payload = base64.b64encode(b'fake-image-bytes').decode()
        response = client.post('/v1/sync/images/upload', headers=user['headers'],
                               json={'image': {'base64': payload, 'mimeType': 'image/png'}})
        assert response.status_code == 200
        image_id = response.get_json()['imageId']
        assert image_id

        response = client.get(f'/v1/sync/images/download/{image_id}',
                              headers=user['headers'])
        assert response.status_code == 200
        image = response.get_json()['image']
        assert image['base64'] == payload
        assert image['mimeType'] == 'image/png'

    def test_cannot_download_another_users_image(self, client, user):
        payload = base64.b64encode(b'private').decode()
        response = client.post('/v1/sync/images/upload', headers=user['headers'],
                               json={'image': {'base64': payload, 'mimeType': 'image/png'}})
        image_id = response.get_json()['imageId']

        from tests.conftest import unique
        other = client.post('/v1/auth/register', json={
            'username': unique('thief'), 'name': 'Thief',
            'email': f'{unique("thief")}@example.com', 'password': 'hunter22',
        }).get_json()
        response = client.get(f'/v1/sync/images/download/{image_id}',
                              headers={'Authorization': f"Bearer {other['token']}"})
        assert response.status_code == 404

    def test_oversized_image_rejected_413(self, client, user):
        # MAX_IMAGE_MB=1 in tests; ~1.5MB decoded
        payload = base64.b64encode(b'x' * (1_500_000)).decode()
        response = client.post('/v1/sync/images/upload', headers=user['headers'],
                               json={'image': {'base64': payload, 'mimeType': 'image/png'}})
        assert response.status_code == 413
