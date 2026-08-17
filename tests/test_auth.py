from tests.conftest import unique


class TestRegister:
    def test_register_returns_user_and_token(self, user):
        assert user['token']
        assert user['id']

    def test_register_rejects_duplicate_email(self, client, user):
        response = client.post('/v1/auth/register', json={
            'username': unique('other'),
            'name': 'Other',
            'email': user['email'],
            'password': 'hunter22',
        })
        assert response.status_code == 400
        assert 'Email' in response.get_json()['message']

    def test_register_rejects_duplicate_username(self, client, user):
        response = client.post('/v1/auth/register', json={
            'username': user['username'],
            'name': 'Other',
            'email': f'{unique("other")}@example.com',
            'password': 'hunter22',
        })
        assert response.status_code == 400

    def test_register_rejects_short_password(self, client):
        response = client.post('/v1/auth/register', json={
            'username': unique('u'),
            'name': 'X',
            'email': f'{unique("u")}@example.com',
            'password': 'abc',
        })
        assert response.status_code == 400
        assert 'Password' in response.get_json()['message']

    def test_register_rejects_bad_email(self, client):
        response = client.post('/v1/auth/register', json={
            'username': unique('u'),
            'name': 'X',
            'email': 'not-an-email',
            'password': 'hunter22',
        })
        assert response.status_code == 400


class TestLogin:
    def test_login_with_username(self, client, user):
        response = client.post('/v1/auth/login', json={
            'username': user['username'], 'password': user['password'],
        })
        assert response.status_code == 200
        assert response.get_json()['token']

    def test_login_with_email(self, client, user):
        response = client.post('/v1/auth/login', json={
            'email': user['email'], 'password': user['password'],
        })
        assert response.status_code == 200

    def test_login_wrong_password(self, client, user):
        response = client.post('/v1/auth/login', json={
            'username': user['username'], 'password': 'wrong-pass',
        })
        assert response.status_code == 401

    def test_profile_fields_survive_roundtrip(self, client, user):
        response = client.post('/v1/auth/login', json={
            'email': user['email'], 'password': user['password'],
        })
        body = response.get_json()
        assert body['age'] == 34
        assert body['gender'] == 'female'
        assert body['has_insurance'] is True


class TestSuspendReactivate:
    def test_suspend_then_login_flags_suspension(self, client, user):
        assert client.post('/v1/auth/suspend', headers=user['headers']).status_code == 200

        response = client.post('/v1/auth/login', json={
            'username': user['username'], 'password': user['password'],
        })
        assert response.status_code == 403
        assert response.get_json().get('suspended') is True

        # Token auth is blocked while suspended
        response = client.put('/v1/auth/update-profile', headers=user['headers'],
                              json={'name': 'New Name'})
        assert response.status_code == 403

        # Reactivation with credentials returns a fresh session
        response = client.post('/v1/auth/reactivate', json={
            'username': user['username'], 'password': user['password'],
        })
        assert response.status_code == 200
        assert response.get_json()['status'] == 'active'


class TestDeleteAccount:
    def test_delete_purges_sync_data_and_frees_identity(self, client, db, user):
        # Seed sync data + an image for this user
        response = client.post('/v1/sync/sync2', headers=user['headers'], json={
            'plan_parent': [{'id': 'plan-1', 'title': 'Brush twice'}],
        })
        assert response.status_code == 200
        response = client.post('/v1/sync/images/upload', headers=user['headers'],
                               json={'image': {'base64': 'aGVsbG8=', 'mimeType': 'image/png'}})
        assert response.status_code == 200

        assert client.delete('/v1/auth/account', headers=user['headers']).status_code == 200

        # Sync data + images hard-deleted
        assert db['plan_parent'].count_documents({'userId': user['id']}) == 0
        assert db['images'].count_documents({'userId': user['id']}) == 0
        # User archived
        assert db['deleted_users'].count_documents({}) >= 1

        # Old token is dead
        response = client.post('/v1/sync/sync1', headers=user['headers'], json={})
        assert response.status_code == 401

        # Identity freed for a new registration
        response = client.post('/v1/auth/register', json={
            'username': user['username'], 'name': 'Again',
            'email': user['email'], 'password': 'hunter22',
        })
        assert response.status_code == 201


class TestPasswordReset:
    def _reset_token_for(self, user):
        from app.repositories import UserRepository
        from app.utils import generate_reset_token
        record = UserRepository().find_by_email(user['email'])
        return generate_reset_token(record._id, record.password)

    def test_reset_flow_and_single_use(self, client, user):
        token = self._reset_token_for(user)

        response = client.post('/v1/auth/reset-password', json={
            'token': token, 'password': 'newpass99',
        })
        assert response.status_code == 200

        # New password works, old one doesn't
        assert client.post('/v1/auth/login', json={
            'email': user['email'], 'password': 'newpass99'}).status_code == 200
        assert client.post('/v1/auth/login', json={
            'email': user['email'], 'password': user['password']}).status_code == 401

        # The SAME token is now dead (single use)
        response = client.post('/v1/auth/reset-password', json={
            'token': token, 'password': 'thirdpass7',
        })
        assert response.status_code == 400

    def test_normal_auth_token_rejected_as_reset_token(self, client, user):
        response = client.post('/v1/auth/reset-password', json={
            'token': user['token'], 'password': 'newpass99',
        })
        assert response.status_code == 400

    def test_forgot_password_response_is_uniform(self, client, user):
        real = client.post('/v1/auth/forgot-password', json={'email': user['email']})
        fake = client.post('/v1/auth/forgot-password', json={'email': 'ghost@example.com'})
        assert real.status_code == fake.status_code == 200
        assert real.get_json() == fake.get_json()


class TestTokenAuth:
    def test_missing_token(self, client):
        assert client.post('/v1/sync/sync1', json={}).status_code == 401

    def test_garbage_token(self, client):
        response = client.post('/v1/sync/sync1', json={},
                               headers={'Authorization': 'Bearer garbage'})
        assert response.status_code == 401


class TestRateLimit:
    def test_login_rate_limited_when_enabled(self, client, user):
        from app.utils.rate_limit import limiter
        limiter.enabled = True
        try:
            last = None
            for _ in range(11):  # default limit: 10 per minute
                last = client.post('/v1/auth/login', json={
                    'username': user['username'], 'password': 'wrong',
                })
            assert last.status_code == 429
            assert 'message' in last.get_json()
        finally:
            limiter.enabled = False
