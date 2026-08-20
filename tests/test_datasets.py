"""Shared dataset workspace: /v1/desktop/datasets/*.

Covers the concurrency contract (optimistic locking, tombstones, delta feed)
and the labeler-identity requirement on writes.
"""

import base64
import hashlib
import uuid

import pytest


def _asset(content=b'image-bytes'):
    data = base64.b64encode(content).decode()
    raw = base64.b64decode(data)
    return {'hash': hashlib.sha256(raw).hexdigest(), 'base64': data,
            'mimeType': 'image/png', 'filename': 'photo.png',
            'width': 640, 'height': 480, 'sourceType': 'local'}


@pytest.fixture()
def labeler_headers(desktop_headers):
    return {**desktop_headers, 'X-Labeler-Name': 'Dr. Aylin'}


@pytest.fixture()
def dataset(client, labeler_headers):
    response = client.post('/v1/desktop/datasets', headers=labeler_headers, json={
        'name': 'Caries study', 'description': 'test set',
        'tasks': ['cavity', 'gingivitis'],
    })
    assert response.status_code == 201, response.get_json()
    return response.get_json()['dataset']


def _add_item(client, headers, dataset_id, content=b'image-bytes', **overrides):
    """Upload an asset and add one item; returns (asset, add-result, item_id).
    Item ids are uuids in the real client — mirror that so ids never collide
    across tests (they are globally unique document ids)."""
    asset = _asset(content)
    upload = client.post('/v1/desktop/datasets/assets', headers=headers,
                         json={'assets': [asset]})
    assert upload.status_code == 200, upload.get_json()

    item = {
        'id': f'item-{uuid.uuid4().hex[:10]}',
        'assetHash': asset['hash'], 'sourceType': 'local',
        'sourceRef': {'historyEntryId': 'h1'},
        'image': {'findings': ['caries'], 'severity': 1.2},
        'masks': {'gingivitis': asset['base64'],
                  'gingivitisAi': asset['base64'],
                  'plaque': {'0': asset['base64']},
                  'plaqueAi': {'0': asset['base64']}},
    }
    item.update(overrides)
    response = client.post(f'/v1/desktop/datasets/{dataset_id}/items',
                           headers=headers, json={'items': [item]})
    assert response.status_code == 200, response.get_json()
    result = response.get_json()
    item_id = result['added'][0] if result['added'] else item['id']
    return asset, result, item_id


class TestLabelerIdentity:
    def test_write_without_labeler_name_is_rejected(self, client, desktop_headers):
        response = client.post('/v1/desktop/datasets', headers=desktop_headers,
                               json={'name': 'X', 'tasks': ['cavity']})
        assert response.status_code == 400
        assert 'X-Labeler-Name' in response.get_json()['message']

    def test_short_labeler_name_is_rejected(self, client, desktop_headers):
        headers = {**desktop_headers, 'X-Labeler-Name': 'Ay'}
        response = client.post('/v1/desktop/datasets', headers=headers,
                               json={'name': 'X', 'tasks': ['cavity']})
        assert response.status_code == 400

    def test_reads_need_no_labeler_name(self, client, desktop_headers):
        response = client.get('/v1/desktop/datasets', headers=desktop_headers)
        assert response.status_code == 200


class TestDatasetCrud:
    def test_create_and_list_with_counts(self, client, labeler_headers, dataset):
        assert dataset['version'] == 1
        assert dataset['createdBy'] == 'Dr. Aylin'
        listing = client.get('/v1/desktop/datasets', headers=labeler_headers)
        rows = [d for d in listing.get_json()['datasets'] if d['id'] == dataset['id']]
        assert rows and rows[0]['imageCount'] == 0

    def test_unknown_task_rejected(self, client, labeler_headers):
        response = client.post('/v1/desktop/datasets', headers=labeler_headers,
                               json={'name': 'X', 'tasks': ['teleportation']})
        assert response.status_code == 400

    def test_versioned_update_and_conflict(self, client, desktop_headers,
                                           labeler_headers, dataset):
        ok = client.patch(f"/v1/desktop/datasets/{dataset['id']}",
                          headers=labeler_headers,
                          json={'baseVersion': 1, 'name': 'Renamed'})
        assert ok.status_code == 200
        assert ok.get_json()['dataset']['version'] == 2

        other = {**desktop_headers, 'X-Labeler-Name': 'Dr. Burak'}
        stale = client.patch(f"/v1/desktop/datasets/{dataset['id']}",
                             headers=other,
                             json={'baseVersion': 1, 'name': 'Mine now'})
        assert stale.status_code == 409
        conflict = stale.get_json()['conflict']
        assert conflict['version'] == 2
        assert conflict['updatedBy'] == 'Dr. Aylin'

    def test_delete_tombstones(self, client, labeler_headers, dataset):
        assert client.delete(f"/v1/desktop/datasets/{dataset['id']}",
                             headers=labeler_headers).status_code == 200
        listing = client.get('/v1/desktop/datasets', headers=labeler_headers)
        assert dataset['id'] not in [d['id'] for d in listing.get_json()['datasets']]


class TestAssets:
    def test_check_upload_check(self, client, labeler_headers):
        asset = _asset(b'fresh-bytes-1')
        check = client.post('/v1/desktop/datasets/assets/check',
                            headers=labeler_headers,
                            json={'hashes': [asset['hash']]})
        assert check.get_json()['missing'] == [asset['hash']]

        upload = client.post('/v1/desktop/datasets/assets',
                             headers=labeler_headers, json={'assets': [asset]})
        assert upload.status_code == 200

        recheck = client.post('/v1/desktop/datasets/assets/check',
                              headers=labeler_headers,
                              json={'hashes': [asset['hash']]})
        assert recheck.get_json()['missing'] == []

        fetched = client.get(f"/v1/desktop/datasets/assets/{asset['hash']}",
                             headers=labeler_headers)
        assert fetched.status_code == 200
        assert fetched.get_json()['asset']['base64'] == asset['base64']

    def test_hash_mismatch_rejected(self, client, labeler_headers):
        asset = _asset(b'real-content')
        asset['hash'] = hashlib.sha256(b'other-content').hexdigest()
        response = client.post('/v1/desktop/datasets/assets',
                               headers=labeler_headers, json={'assets': [asset]})
        assert response.status_code == 400
        assert 'mismatch' in response.get_json()['message']


class TestItems:
    def test_add_lists_and_fetches(self, client, db, labeler_headers, dataset):
        asset, result, item_id = _add_item(client, labeler_headers, dataset['id'])
        assert result['added'] == [item_id]

        items = client.get(f"/v1/desktop/datasets/{dataset['id']}/items",
                           headers=labeler_headers).get_json()['items']
        assert len(items) == 1
        assert items[0]['status'] == 'unlabeled'
        assert 'ai' not in items[0]  # AI snapshot only on full item fetch

        full = client.get(f'/v1/desktop/datasets/items/{item_id}',
                          headers=labeler_headers).get_json()['item']
        assert full['ai'] == {'findings': ['caries'], 'severity': 1.2}
        assert full['gingivitisMask'] == asset['base64']
        assert full['gingivitisMaskAi'] == asset['base64']
        assert list(full['plaqueMasks'].keys()) == ['0']

        events = client.get(f'/v1/desktop/datasets/items/{item_id}/events',
                            headers=labeler_headers).get_json()['events']
        assert events[0]['task'] == 'import' and events[0]['source'] == 'ai'

    def test_duplicate_add_is_skipped(self, client, labeler_headers, dataset):
        _add_item(client, labeler_headers, dataset['id'], content=b'dup-bytes')
        _, again, _ = _add_item(client, labeler_headers, dataset['id'],
                                content=b'dup-bytes')
        assert again['added'] == []
        assert again['skipped'][0]['reason'] == 'already in dataset'

    def test_add_without_uploaded_asset_is_skipped(self, client, labeler_headers,
                                                   dataset):
        response = client.post(f"/v1/desktop/datasets/{dataset['id']}/items",
                               headers=labeler_headers, json={'items': [{
                                   'id': 'ghost',
                                   'assetHash': 'a' * 64,
                                   'sourceType': 'local',
                                   'image': {},
                               }]})
        assert response.get_json()['skipped'][0]['reason'] == 'asset not uploaded'

    def test_save_labels_bumps_status_and_version(self, client, labeler_headers,
                                                  dataset):
        _, _, item_id = _add_item(client, labeler_headers, dataset['id'])
        response = client.put(f'/v1/desktop/datasets/items/{item_id}/labels',
                              headers=labeler_headers, json={
                                  'baseVersion': 1,
                                  'image': {'findings': [], 'severity': 0.0},
                                  'tasksChanged': ['cavity'],
                              })
        assert response.status_code == 200
        item = response.get_json()['item']
        assert item['status'] == 'in_progress'
        assert item['version'] == 2
        assert item['updatedBy'] == 'Dr. Aylin'

        events = client.get(f'/v1/desktop/datasets/items/{item_id}/events',
                            headers=labeler_headers).get_json()['events']
        human = [e for e in events if e['source'] == 'human']
        assert human and human[0]['task'] == 'cavity'
        assert human[0]['labeledBy'] == 'Dr. Aylin'

    def test_stale_label_save_conflicts_with_winner_name(self, client,
                                                         desktop_headers,
                                                         labeler_headers, dataset):
        _, _, item_id = _add_item(client, labeler_headers, dataset['id'])
        first = client.put(f'/v1/desktop/datasets/items/{item_id}/labels',
                           headers=labeler_headers,
                           json={'baseVersion': 1, 'image': {'severity': 0.1}})
        assert first.status_code == 200

        other = {**desktop_headers, 'X-Labeler-Name': 'Dr. Burak'}
        stale = client.put(f'/v1/desktop/datasets/items/{item_id}/labels',
                           headers=other,
                           json={'baseVersion': 1, 'image': {'severity': 0.9}})
        assert stale.status_code == 409
        assert stale.get_json()['conflict']['updatedBy'] == 'Dr. Aylin'

    def test_reedit_never_demotes_approved(self, client, labeler_headers, dataset):
        _, _, item_id = _add_item(client, labeler_headers, dataset['id'])
        approve = client.patch(f'/v1/desktop/datasets/items/{item_id}',
                               headers=labeler_headers,
                               json={'baseVersion': 1, 'status': 'approved'})
        assert approve.status_code == 200

        save = client.put(f'/v1/desktop/datasets/items/{item_id}/labels',
                          headers=labeler_headers,
                          json={'baseVersion': 2, 'image': {'severity': 0.5}})
        assert save.get_json()['item']['status'] == 'approved'

    def test_exclude_carries_reason(self, client, labeler_headers, dataset):
        _, _, item_id = _add_item(client, labeler_headers, dataset['id'])
        response = client.patch(f'/v1/desktop/datasets/items/{item_id}',
                                headers=labeler_headers,
                                json={'baseVersion': 1, 'status': 'excluded',
                                      'excludeReason': 'blurry photo'})
        item = response.get_json()['item']
        assert item['status'] == 'excluded'
        assert item['excludeReason'] == 'blurry photo'

    def test_delete_tombstones_and_readd_revives(self, client, db,
                                                 labeler_headers, dataset):
        _, _, item_id = _add_item(client, labeler_headers, dataset['id'],
                                  content=b'revive-bytes')
        assert client.delete(f'/v1/desktop/datasets/items/{item_id}',
                             headers=labeler_headers).status_code == 200

        items = client.get(f"/v1/desktop/datasets/{dataset['id']}/items",
                           headers=labeler_headers).get_json()['items']
        assert items == []
        # Tombstoned masks are purged.
        assert db['dataset_masks'].count_documents({'itemId': item_id}) == 0

        _, readd, _ = _add_item(client, labeler_headers, dataset['id'],
                                content=b'revive-bytes')
        # The tombstoned row is revived under its original id.
        assert readd['added'] == [item_id]
        items = client.get(f"/v1/desktop/datasets/{dataset['id']}/items",
                           headers=labeler_headers).get_json()['items']
        assert len(items) == 1 and items[0]['status'] == 'unlabeled'


class TestExportLedger:
    def test_versions_increment_and_list(self, client, labeler_headers, dataset):
        first = client.post(f"/v1/desktop/datasets/{dataset['id']}/exports",
                            headers=labeler_headers,
                            json={'itemCount': 5, 'filters': {'statuses': ['approved']}})
        assert first.status_code == 201
        assert first.get_json()['version'] == 1

        second = client.post(f"/v1/desktop/datasets/{dataset['id']}/exports",
                             headers=labeler_headers, json={'itemCount': 7})
        assert second.get_json()['version'] == 2

        listing = client.get(f"/v1/desktop/datasets/{dataset['id']}/exports",
                             headers=labeler_headers).get_json()['exports']
        assert [e['version'] for e in listing] == [2, 1]
        assert listing[1]['createdBy'] == 'Dr. Aylin'


class TestItemAssetJoin:
    def test_item_get_joins_demographics_from_asset(self, client, labeler_headers,
                                                    dataset):
        _, _, item_id = _add_item(
            client, labeler_headers, dataset['id'], content=b'joined-photo',
            demographics={'source': 'mobile_profile', 'ageBand': '18_34'},
            clinicalContext={'snapshotAt': '2026-08-19T00:00:00Z'},
        )
        full = client.get(f'/v1/desktop/datasets/items/{item_id}',
                          headers=labeler_headers).get_json()['item']
        # Demographics are normalized to the full field set on write.
        assert full['demographics']['source'] == 'mobile_profile'
        assert full['demographics']['ageBand'] == '18_34'
        assert full['demographics']['gender'] is None
        assert full['clinicalContext'] == {'snapshotAt': '2026-08-19T00:00:00Z'}
        assert full['filename'] == 'photo.png'

    def test_item_list_joins_demographics(self, client, labeler_headers, dataset):
        _, _, item_id = _add_item(
            client, labeler_headers, dataset['id'], content=b'list-join-photo',
            demographics={'source': 'manual', 'gender': 'female'},
        )
        listing = client.get(f"/v1/desktop/datasets/{dataset['id']}/items",
                             headers=labeler_headers).get_json()['items']
        row = next(i for i in listing if i['id'] == item_id)
        assert row['demographics']['gender'] == 'female'
        # Manual entries are stamped with who typed them.
        assert row['demographics']['enteredBy'] == 'Dr. Aylin'
        assert row['demographics']['enteredAt']

    def test_invalid_demographics_skips_item(self, client, labeler_headers, dataset):
        asset = _asset(b'bad-demo-photo')
        client.post('/v1/desktop/datasets/assets', headers=labeler_headers,
                    json={'assets': [asset]})
        response = client.post(
            f"/v1/desktop/datasets/{dataset['id']}/items",
            headers=labeler_headers,
            json={'items': [{
                'id': f'item-{uuid.uuid4().hex[:10]}',
                'assetHash': asset['hash'], 'sourceType': 'local',
                'sourceRef': {}, 'image': {'findings': []},
                'demographics': {'source': 'astrology', 'ageBand': '18_34'},
            }]},
        )
        result = response.get_json()
        assert result['added'] == []
        assert 'source' in result['skipped'][0]['reason']


class TestAssetMeta:
    def _meta_url(self, asset_hash):
        return f'/v1/desktop/datasets/assets/{asset_hash}/meta'

    def test_manual_entry_is_stamped_and_joined(self, client, labeler_headers,
                                                dataset):
        asset, _, item_id = _add_item(client, labeler_headers, dataset['id'],
                                      content=b'meta-photo-1')
        response = client.patch(
            self._meta_url(asset['hash']), headers=labeler_headers,
            json={'demographics': {'source': 'manual', 'ageBand': '6_12',
                                   'hasInsurance': True}},
        )
        assert response.status_code == 200, response.get_json()
        stored = response.get_json()['demographics']
        assert stored['ageBand'] == '6_12'
        assert stored['hasInsurance'] is True
        assert stored['enteredBy'] == 'Dr. Aylin'
        assert stored['enteredAt']

        full = client.get(f'/v1/desktop/datasets/items/{item_id}',
                          headers=labeler_headers).get_json()['item']
        assert full['demographics']['ageBand'] == '6_12'

    def test_mobile_profile_needs_explicit_overwrite(self, client,
                                                     labeler_headers, dataset):
        asset, _, _ = _add_item(
            client, labeler_headers, dataset['id'], content=b'meta-photo-2',
            demographics={'source': 'mobile_profile', 'ageBand': '18_34'},
        )
        manual = {'demographics': {'source': 'manual', 'ageBand': '35_54'}}
        refused = client.patch(self._meta_url(asset['hash']),
                               headers=labeler_headers, json=manual)
        assert refused.status_code == 400
        assert 'overwriteMobileProfile' in refused.get_json()['message']

        allowed = client.patch(
            self._meta_url(asset['hash']), headers=labeler_headers,
            json={**manual, 'overwriteMobileProfile': True},
        )
        assert allowed.status_code == 200
        assert allowed.get_json()['demographics']['ageBand'] == '35_54'

    def test_unknown_asset_404s_and_writes_need_labeler(self, client,
                                                        desktop_headers,
                                                        labeler_headers):
        missing = 'ab' * 32
        response = client.patch(self._meta_url(missing), headers=labeler_headers,
                                json={'demographics': {'source': 'manual'}})
        assert response.status_code == 404

        unnamed = client.patch(self._meta_url(missing), headers=desktop_headers,
                               json={'demographics': {'source': 'manual'}})
        assert unnamed.status_code == 400
        assert 'X-Labeler-Name' in unnamed.get_json()['message']


class TestChangesFeed:
    def test_initial_pull_excludes_tombstones(self, client, labeler_headers,
                                              dataset):
        _, _, item_id = _add_item(client, labeler_headers, dataset['id'])
        client.delete(f'/v1/desktop/datasets/items/{item_id}',
                      headers=labeler_headers)

        initial = client.get('/v1/desktop/datasets/changes',
                             headers=labeler_headers).get_json()
        assert item_id not in [i['id'] for i in initial['items']]

    def test_delta_carries_tombstones_and_cursor_advances(self, client,
                                                          labeler_headers,
                                                          dataset):
        _, _, item_id = _add_item(client, labeler_headers, dataset['id'])
        cursor = client.get('/v1/desktop/datasets/changes',
                            headers=labeler_headers).get_json()['now']

        client.delete(f'/v1/desktop/datasets/items/{item_id}',
                      headers=labeler_headers)

        delta = client.get(f'/v1/desktop/datasets/changes?since={cursor}',
                           headers=labeler_headers).get_json()
        tombstones = [i for i in delta['items'] if i['id'] == item_id]
        assert tombstones and tombstones[0]['isDeleted'] is True

    def test_bad_cursor_rejected(self, client, labeler_headers):
        response = client.get('/v1/desktop/datasets/changes?since=yesterday',
                              headers=labeler_headers)
        assert response.status_code == 400


class TestUsage:
    def test_usage_by_hash_and_mobile_record(self, client, labeler_headers,
                                             dataset):
        asset, _, _ = _add_item(
            client, labeler_headers, dataset['id'], content=b'mobile-photo',
            sourceType='mobile',
            sourceRef={'patientId': 'p1', 'sourceCollection': 'tooth_scan_history',
                       'sourceRecordId': 'scan-77'},
            subject='child',
        )
        response = client.post('/v1/desktop/datasets/usage',
                               headers=labeler_headers, json={
                                   'assetHashes': [asset['hash'], 'f' * 64],
                                   'mobileRecordIds': ['scan-77', 'scan-unknown'],
                               })
        body = response.get_json()
        assert body['assetHashes'][asset['hash']][0]['datasetId'] == dataset['id']
        assert 'f' * 64 not in body['assetHashes']
        assert body['mobileRecordIds']['scan-77'][0]['datasetName'] == dataset['name']
        assert 'scan-unknown' not in body['mobileRecordIds']
