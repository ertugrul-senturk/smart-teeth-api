import base64
import io
import json
import zipfile

import pytest

from tests.conftest import API_KEY


class TestApiKey:
    def test_missing_key(self, client):
        response = client.post('/v1/desktop/sync', json={})
        assert response.status_code == 401

    def test_wrong_key(self, client):
        response = client.post('/v1/desktop/sync', json={},
                               headers={'X-API-Key': 'nope'})
        assert response.status_code == 401

    def test_named_key_lands_in_audit_log(self, client, db, desktop_headers):
        client.get('/v1/desktop/patients', headers=desktop_headers)
        entry = db['audit_log'].find_one({'action': 'patients.list'},
                                         sort=[('ts', -1)])
        assert entry is not None
        assert entry['actor'] == 'desktop:testkey/test-device-1'


class TestDesktopSync:
    def test_entries_upsert_and_dedupe(self, client, db, desktop_headers):
        body = {
            'deviceId': 'dev-A',
            'entries': [{'id': 'e1', 'folderName': 'batch1', 'cariesCount': 2}],
        }
        response = client.post('/v1/desktop/sync', json=body, headers=desktop_headers)
        assert response.status_code == 200
        assert response.get_json()['synced'] == ['e1']

        body['entries'][0]['cariesCount'] = 5
        client.post('/v1/desktop/sync', json=body, headers=desktop_headers)

        docs = list(db['desktop_history'].find({'deviceId': 'dev-A', 'id': 'e1'}))
        assert len(docs) == 1
        assert docs[0]['cariesCount'] == 5

    def test_entry_without_id_reports_error(self, client, desktop_headers):
        response = client.post('/v1/desktop/sync', headers=desktop_headers, json={
            'deviceId': 'dev-A', 'entries': [{'folderName': 'no-id'}],
        })
        errors = response.get_json()['errors']
        assert errors and errors[0]['message'] == 'Entry is missing an id'

    def test_image_upload_with_slash_ids(self, client, db, desktop_headers):
        payload = base64.b64encode(b'mask-bytes').decode()
        response = client.post('/v1/desktop/images/upload', headers=desktop_headers, json={
            'deviceId': 'dev-A',
            'images': [{'id': 'e1/img1/gum.png', 'base64': payload,
                        'mimeType': 'image/png'}],
        })
        assert response.status_code == 200
        assert response.get_json()['imageIds'] == ['e1/img1/gum.png']
        assert db['desktop_images'].count_documents(
            {'deviceId': 'dev-A', 'id': 'e1/img1/gum.png'}) == 1


class TestPatientBrowsing:
    def _seed_scan(self, client, user):
        response = client.post('/v1/sync/images/upload', headers=user['headers'],
                               json={'image': {'base64': base64.b64encode(b'tooth').decode(),
                                               'mimeType': 'image/jpeg'}})
        image_id = response.get_json()['imageId']
        client.post('/v1/sync/sync2', headers=user['headers'], json={
            'tooth_scan_history': [{
                'id': 'scan-1', 'date': '2026-08-10 09:00',
                'toothImages': [{'imageId': image_id, 'index': 0,
                                 'cavityLabel': 'healthy', 'cavityConfidence': 0.97}],
            }],
            'tell_us_about_you_data': [{'id': 'q-1', 'answers': {'brushing': 'twice'}}],
        })
        return image_id

    def test_patients_list_shows_demographics_and_counts(self, client, user, desktop_headers):
        self._seed_scan(client, user)

        response = client.get('/v1/desktop/patients', headers=desktop_headers)
        assert response.status_code == 200
        patients = response.get_json()['patients']
        me = next(p for p in patients if p['id'] == user['id'])
        assert me['age'] == 34
        assert me['gender'] == 'female'
        assert me['has_insurance'] is True
        assert me['recordCounts']['tooth_scan_history'] == 1
        assert me['recordCounts']['tell_us_about_you_data'] == 1

    def _register(self, client, name):
        from tests.conftest import unique
        response = client.post('/v1/auth/register', json={
            'username': unique('pg'), 'name': name,
            'email': f'{unique("pg")}@example.com', 'password': 'hunter22',
        })
        assert response.status_code == 201, response.get_json()
        return response.get_json()['id']

    def test_patients_search_and_pagination(self, client, desktop_headers):
        for name in ['Paging Alice', 'Paging Bob', 'Paging Carol']:
            self._register(client, name)

        response = client.get('/v1/desktop/patients?search=paging&page=1&pageSize=2',
                              headers=desktop_headers)
        assert response.status_code == 200
        body = response.get_json()
        assert body['total'] == 3
        assert body['page'] == 1 and body['pageSize'] == 2
        assert [p['name'] for p in body['patients']] == ['Paging Alice', 'Paging Bob']

        response = client.get('/v1/desktop/patients?search=paging&page=2&pageSize=2',
                              headers=desktop_headers)
        assert [p['name'] for p in response.get_json()['patients']] == ['Paging Carol']

        # Search also matches emails; unmatched queries return an empty page.
        response = client.get('/v1/desktop/patients?search=zzz-no-match&page=1',
                              headers=desktop_headers)
        assert response.get_json() == {
            'patients': [], 'total': 0, 'page': 1, 'pageSize': 50,
        }

        # Without `page` the legacy full-list shape is unchanged.
        legacy = client.get('/v1/desktop/patients', headers=desktop_headers).get_json()
        assert 'total' not in legacy and isinstance(legacy['patients'], list)

    def test_records_pagination_and_trend(self, client, user, desktop_headers):
        scans = [{
            'id': f'pg-scan-{i}', 'timestamp': 1700000000000 + i,
            'toothImages': [
                {'index': 0, 'cavityLabel': 'healthy', 'cavityConfidence': 0.9,
                 'plaqueLevel': 'mild', 'plaqueCoverage': 0.2},
                {'index': 1, 'cavityLabel': 'level_2', 'cavityConfidence': 0.8,
                 'plaqueLevel': 'healthy', 'plaqueCoverage': 0.0},
            ],
        } for i in range(3)]
        client.post('/v1/sync/sync2', headers=user['headers'],
                    json={'tooth_scan_history': scans})

        base = f"/v1/desktop/patients/{user['id']}/records?collection=tooth_scan_history"
        response = client.get(f'{base}&page=1&pageSize=2&includeTrend=1',
                              headers=desktop_headers)
        assert response.status_code == 200
        body = response.get_json()
        assert body['total'] == 3
        assert len(body['records']) == 2

        # Trend covers the whole history and derives summaries from the teeth
        # when the record never synced one.
        assert len(body['trend']) == 3
        row = body['trend'][0]
        assert row['summary'] == {'total': 2, 'healthy': 1, 'level1': 0, 'level2': 1}
        assert row['plaqueSummary']['total'] == 2
        assert row['plaqueSummary']['avgCoverage'] == pytest.approx(10.0)

        response = client.get(f'{base}&page=2&pageSize=2', headers=desktop_headers)
        body = response.get_json()
        assert len(body['records']) == 1
        assert 'trend' not in body

    def test_records_endpoint_with_import_status(self, client, user, desktop_headers):
        self._seed_scan(client, user)

        response = client.get(
            f"/v1/desktop/patients/{user['id']}/records?collection=tooth_scan_history",
            headers=desktop_headers)
        assert response.status_code == 200
        records = response.get_json()['records']
        assert len(records) == 1
        assert records[0]['id'] == 'scan-1'
        assert records[0]['importStatus'] == 'none'

    def test_records_time_filter(self, client, user, desktop_headers):
        self._seed_scan(client, user)

        base = f"/v1/desktop/patients/{user['id']}/records?collection=tooth_scan_history"
        # Window in the past → nothing (createdAt is server-now)
        response = client.get(f'{base}&to=2020-01-01T00:00:00Z', headers=desktop_headers)
        assert response.get_json()['records'] == []
        # Open-ended window from the past → the record
        response = client.get(f'{base}&from=2020-01-01T00:00:00Z', headers=desktop_headers)
        assert len(response.get_json()['records']) == 1

    def test_records_validation(self, client, user, desktop_headers):
        url = f"/v1/desktop/patients/{user['id']}/records"
        assert client.get(url, headers=desktop_headers).status_code == 400
        assert client.get(f'{url}?collection=users',
                          headers=desktop_headers).status_code == 400
        assert client.get(f'{url}?collection=plan_parent&from=garbage',
                          headers=desktop_headers).status_code == 400
        response = client.get(
            '/v1/desktop/patients/000000000000000000000000/records?collection=plan_parent',
            headers=desktop_headers)
        assert response.status_code == 404

    def test_image_fetch_scoped_to_patient(self, client, user, desktop_headers):
        image_id = self._seed_scan(client, user)

        response = client.post(f"/v1/desktop/patients/{user['id']}/images/fetch",
                               headers=desktop_headers, json={'imageIds': [image_id]})
        assert response.status_code == 200
        images = response.get_json()['images']
        assert len(images) == 1
        assert images[0]['base64'] == base64.b64encode(b'tooth').decode()


class TestImportProvenance:
    def test_import_link_lifecycle(self, client, user, desktop_headers):
        client.post('/v1/sync/sync2', headers=user['headers'], json={
            'tooth_scan_history': [{'id': 'scan-x', 'date': '2026-08-01 10:00'}],
        })

        # Register the import
        response = client.post('/v1/desktop/import-link', headers=desktop_headers, json={
            'sourceCollection': 'tooth_scan_history',
            'sourceUserId': user['id'],
            'sourceRecordId': 'scan-x',
            'deviceId': 'dev-A',
            'desktopEntryId': 'desk-entry-1',
        })
        assert response.status_code == 201
        link = response.get_json()['link']
        assert link['importedAt'] and link['lastEditedAt'] is None

        # Badge flips to 'imported'
        response = client.get(
            f"/v1/desktop/patients/{user['id']}/records?collection=tooth_scan_history",
            headers=desktop_headers)
        record = next(r for r in response.get_json()['records'] if r['id'] == 'scan-x')
        assert record['importStatus'] == 'imported'
        assert record['imports'][0]['deviceId'] == 'dev-A'

        # Desktop syncs the (edited) imported entry → badge flips to 'edited'
        client.post('/v1/desktop/sync', headers=desktop_headers, json={
            'deviceId': 'dev-A',
            'entries': [{
                'id': 'desk-entry-1', 'folderName': 'imported',
                'origin': {
                    'source': 'mobile', 'patientId': user['id'],
                    'sourceCollection': 'tooth_scan_history',
                    'sourceRecordId': 'scan-x',
                    'importedAt': '2026-08-16T10:00:00Z',
                    'editedAt': '2026-08-16T11:30:00Z',
                },
            }],
        })

        response = client.get(
            f"/v1/desktop/patients/{user['id']}/records?collection=tooth_scan_history",
            headers=desktop_headers)
        record = next(r for r in response.get_json()['records'] if r['id'] == 'scan-x')
        assert record['importStatus'] == 'edited'
        assert record['imports'][0]['lastEditedAt'] is not None

    def test_import_link_requires_fields(self, client, desktop_headers):
        response = client.post('/v1/desktop/import-link', headers=desktop_headers,
                               json={'sourceCollection': 'tooth_scan_history'})
        assert response.status_code == 400
        assert 'Missing fields' in response.get_json()['message']


class TestExport:
    def _open_zip(self, response):
        assert response.status_code == 200, response.get_data(as_text=True)
        assert response.mimetype == 'application/zip'
        zf = zipfile.ZipFile(io.BytesIO(response.data))
        names = zf.namelist()
        root = names[0].split('/')[0]
        manifest = json.loads(zf.read(f'{root}/manifest.json'))
        return zf, root, manifest, names

    def _read_jsonl(self, zf, path):
        raw = zf.read(path).decode()
        return [json.loads(line) for line in raw.splitlines() if line.strip()]

    def test_full_export_structure(self, client, user, desktop_headers):
        # Mobile: scan + questionnaire + image
        response = client.post('/v1/sync/images/upload', headers=user['headers'],
                               json={'image': {'base64': base64.b64encode(b'molar').decode(),
                                               'mimeType': 'image/jpeg'}})
        image_id = response.get_json()['imageId']
        client.post('/v1/sync/sync2', headers=user['headers'], json={
            'tooth_scan_history': [{'id': 'exp-scan',
                                    'toothImages': [{'imageId': image_id, 'index': 0}]}],
            'tell_us_about_you_data': [{'id': 'exp-q', 'answers': {'flossing': 'daily'}}],
        })
        # Desktop: entry + image
        client.post('/v1/desktop/sync', headers=desktop_headers, json={
            'deviceId': 'dev-EXP',
            'entries': [{'id': 'exp-e1', 'folderName': 'b',
                         'maskImageIds': ['exp-e1/i1/gum.png'], 'originalImageIds': []}],
        })
        client.post('/v1/desktop/images/upload', headers=desktop_headers, json={
            'deviceId': 'dev-EXP',
            'images': [{'id': 'exp-e1/i1/gum.png',
                        'base64': base64.b64encode(b'gum-mask').decode(),
                        'mimeType': 'image/png'}],
        })

        response = client.post('/v1/desktop/export', headers=desktop_headers,
                               json={'source': 'both'})
        zf, root, manifest, names = self._open_zip(response)

        assert manifest['schemaVersion'] == '1.0'
        assert manifest['counts']['mobile/patients.jsonl'] >= 1

        # Demographics present and joined
        patients = self._read_jsonl(zf, f'{root}/mobile/patients.jsonl')
        me = next(p for p in patients if p.get('email') == user['email'])
        assert me['age'] == 34 and me['gender'] == 'female'
        assert me['patientId'].startswith('p_')

        # Scan row: pseudo-id join key + denormalized demographics + image path
        scans = self._read_jsonl(zf, f'{root}/mobile/scans.jsonl')
        scan = next(s for s in scans if s['id'] == 'exp-scan')
        assert scan['patientId'] == me['patientId']
        assert scan['patient_age'] == 34
        tooth = scan['toothImages'][0]
        assert tooth['imagePath'].startswith(
            f"mobile/images/{me['patientId']}/exp-scan/tooth_0")
        assert zf.read(f"{root}/{tooth['imagePath']}") == b'molar'

        # Questionnaire data exported
        questionnaires = self._read_jsonl(
            zf, f'{root}/mobile/records/tell_us_about_you_data.jsonl')
        q = next(r for r in questionnaires if r['id'] == 'exp-q')
        assert q['answers'] == {'flossing': 'daily'}
        assert q['patientId'] == me['patientId']

        # Desktop side
        entries = self._read_jsonl(zf, f'{root}/desktop/entries.jsonl')
        assert any(e['id'] == 'exp-e1' for e in entries)
        assert zf.read(f'{root}/desktop/images/dev-EXP/exp-e1/i1/gum.png') == b'gum-mask'

        # Audit trail recorded the export
        # (actor asserted in TestApiKey; here just presence)

    def test_anonymize_strips_identity_keeps_demographics(self, client, user, desktop_headers):
        response = client.post('/v1/desktop/export', headers=desktop_headers,
                               json={'source': 'mobile', 'anonymize': True})
        zf, root, manifest, _ = self._open_zip(response)
        patients = self._read_jsonl(zf, f'{root}/mobile/patients.jsonl')
        assert patients, 'expected at least one patient'
        for p in patients:
            assert 'name' not in p and 'email' not in p
            assert 'age' in p and 'patientId' in p

    def test_source_and_time_filters(self, client, user, desktop_headers):
        response = client.post('/v1/desktop/export', headers=desktop_headers,
                               json={'source': 'desktop'})
        zf, root, manifest, names = self._open_zip(response)
        assert not any('/mobile/' in n for n in names)

        response = client.post('/v1/desktop/export', headers=desktop_headers,
                               json={'source': 'mobile', 'to': '2020-01-01T00:00:00Z'})
        zf, root, manifest, _ = self._open_zip(response)
        scans = self._read_jsonl(zf, f'{root}/mobile/scans.jsonl')
        assert scans == []

    def test_export_validation(self, client, desktop_headers):
        assert client.post('/v1/desktop/export', headers=desktop_headers,
                           json={'source': 'bogus'}).status_code == 400
        assert client.post('/v1/desktop/export', headers=desktop_headers,
                           json={'collections': ['users']}).status_code == 400
        assert client.post('/v1/desktop/export', headers=desktop_headers,
                           json={'from': 'not-a-date'}).status_code == 400


class TestExportPatientFilter:
    def test_patient_ids_limits_mobile_data(self, client, user, desktop_headers):
        import uuid as _uuid
        # A second patient with their own scan — must NOT appear in the export.
        response = client.post('/v1/auth/register', json={
            'username': f'other-{_uuid.uuid4().hex[:10]}',
            'name': 'Other User',
            'email': f'other-{_uuid.uuid4().hex[:10]}@example.com',
            'password': 'hunter22',
            'age': 40, 'gender': 'male', 'ethnicity': 'other',
            'has_insurance': False, 'last_doctor_visit': '2025-12-01',
        })
        assert response.status_code == 201
        other_headers = {'Authorization': f"Bearer {response.get_json()['token']}"}

        client.post('/v1/sync/sync2', headers=user['headers'], json={
            'tooth_scan_history': [{'id': 'mine-1'}]})
        client.post('/v1/sync/sync2', headers=other_headers, json={
            'tooth_scan_history': [{'id': 'theirs-1'}]})

        response = client.post('/v1/desktop/export', headers=desktop_headers,
                               json={'source': 'mobile', 'patientIds': [user['id']]})
        assert response.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(response.data))
        root = zf.namelist()[0].split('/')[0]

        patients = [json.loads(line) for line in
                    zf.read(f'{root}/mobile/patients.jsonl').decode().splitlines() if line.strip()]
        assert len(patients) == 1

        scans = [json.loads(line) for line in
                 zf.read(f'{root}/mobile/scans.jsonl').decode().splitlines() if line.strip()]
        scan_ids = [s.get('id') for s in scans]
        assert 'mine-1' in scan_ids
        assert 'theirs-1' not in scan_ids


class TestPatientDeletion:
    def _seed_scan(self, client, user, scan_id):
        """One synced scan referencing one uploaded image; returns the image id."""
        response = client.post('/v1/sync/images/upload', headers=user['headers'],
                               json={'image': {'base64': base64.b64encode(b'tooth').decode(),
                                               'mimeType': 'image/jpeg'}})
        image_id = response.get_json()['imageId']
        client.post('/v1/sync/sync2', headers=user['headers'], json={
            'tooth_scan_history': [{
                'id': scan_id, 'date': '2026-08-10 09:00',
                'toothImages': [{'imageId': image_id, 'index': 0,
                                 'cavityLabel': 'healthy', 'cavityConfidence': 0.97}],
            }],
        })
        return image_id

    def test_delete_selected_records_cascades(self, client, db, user, desktop_headers):
        from bson import ObjectId
        img1 = self._seed_scan(client, user, 'scan-1')
        img2 = self._seed_scan(client, user, 'scan-2')
        client.post('/v1/desktop/import-link', headers=desktop_headers, json={
            'sourceCollection': 'tooth_scan_history', 'sourceUserId': user['id'],
            'sourceRecordId': 'scan-1', 'deviceId': 'dev-A', 'desktopEntryId': 'e1'})

        response = client.delete(f"/v1/desktop/patients/{user['id']}/records",
                                 headers=desktop_headers,
                                 json={'collection': 'tooth_scan_history',
                                       'recordIds': ['scan-1']})
        assert response.status_code == 200
        removed = response.get_json()['removed']
        assert removed == {'records': 1, 'images': 1, 'importLinks': 1}

        # scan-2 and its image are untouched
        assert db['tooth_scan_history'].count_documents(
            {'userId': user['id'], 'id': 'scan-2'}) == 1
        assert db['images'].count_documents({'_id': ObjectId(img1)}) == 0
        assert db['images'].count_documents({'_id': ObjectId(img2)}) == 1

    def test_delete_records_purges_tombstoned(self, client, db, user, desktop_headers):
        self._seed_scan(client, user, 'scan-1')
        db['tooth_scan_history'].update_one(
            {'userId': user['id'], 'id': 'scan-1'},
            {'$set': {'isDeleted': True}})

        response = client.delete(f"/v1/desktop/patients/{user['id']}/records",
                                 headers=desktop_headers,
                                 json={'collection': 'tooth_scan_history',
                                       'recordIds': ['scan-1']})
        assert response.get_json()['removed']['records'] == 1
        assert db['tooth_scan_history'].count_documents(
            {'userId': user['id'], 'id': 'scan-1'}) == 0

    def test_delete_records_validation(self, client, user, desktop_headers):
        url = f"/v1/desktop/patients/{user['id']}/records"
        assert client.delete(url, headers=desktop_headers,
                             json={'recordIds': ['x']}).status_code == 400
        assert client.delete(url, headers=desktop_headers,
                             json={'collection': 'tooth_scan_history',
                                   'recordIds': []}).status_code == 400
        assert client.delete(url, headers=desktop_headers,
                             json={'collection': 'users',
                                   'recordIds': ['x']}).status_code == 400
        assert client.delete(
            '/v1/desktop/patients/000000000000000000000000/records',
            headers=desktop_headers,
            json={'collection': 'tooth_scan_history',
                  'recordIds': ['x']}).status_code == 404

    def test_delete_patient_data_keeps_account(self, client, db, user, desktop_headers):
        from bson import ObjectId
        self._seed_scan(client, user, 'scan-1')

        response = client.delete(f"/v1/desktop/patients/{user['id']}",
                                 headers=desktop_headers, json={})
        assert response.status_code == 200
        body = response.get_json()
        assert body['accountDeleted'] is False
        assert body['removed']['tooth_scan_history'] == 1
        assert body['removed']['images'] == 1

        assert db['tooth_scan_history'].count_documents({'userId': user['id']}) == 0
        assert db['images'].count_documents({'userId': user['id']}) == 0
        assert db['users'].count_documents({'_id': ObjectId(user['id'])}) == 1

    def test_delete_patient_with_account(self, client, db, user, desktop_headers):
        from bson import ObjectId
        self._seed_scan(client, user, 'scan-1')

        response = client.delete(f"/v1/desktop/patients/{user['id']}",
                                 headers=desktop_headers,
                                 json={'deleteAccount': True})
        assert response.status_code == 200
        assert response.get_json()['accountDeleted'] is True

        assert db['users'].count_documents({'_id': ObjectId(user['id'])}) == 0
        assert db['deleted_users'].count_documents({'_id': ObjectId(user['id'])}) == 0

        entry = db['audit_log'].find_one({'action': 'patient.delete'}, sort=[('ts', -1)])
        assert entry is not None
        assert entry['actor'] == 'desktop:testkey/test-device-1'
        assert entry['details']['deleteAccount'] is True

    def test_delete_patient_not_found(self, client, desktop_headers):
        response = client.delete('/v1/desktop/patients/000000000000000000000000',
                                 headers=desktop_headers, json={})
        assert response.status_code == 404
