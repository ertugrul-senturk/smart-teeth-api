"""Test bootstrap.

Environment MUST be pinned before any app module is imported — Config reads
env at import time. Tests run against a dedicated database (smart_teeth_test)
on the local Mongo instance and drop it before and after the session.
"""

import os
import uuid

os.environ['DATABASE_NAME'] = 'smart_teeth_test'
os.environ['RATELIMIT_ENABLED'] = 'False'
os.environ['MAX_IMAGE_MB'] = '1'          # small cap so oversize tests are cheap
os.environ['MASTER_REGISTRATION_KEY'] = 'AAAAA-BBBBB-CCCCC-DDDDD-EEEEE'
os.environ.setdefault('SECRET_KEY', 'test-secret-key-0123456789abcdef-0123456789abcdef')
os.environ.setdefault('MONGODB_URI', 'mongodb://localhost:27017')

import pytest  # noqa: E402

API_KEY = 'test-api-key-123'
MASTER_KEY = 'AAAAA-BBBBB-CCCCC-DDDDD-EEEEE'


def _seed_desktop_key(flask_app):
    """Desktop endpoints only accept managed registration keys (or the master
    key) — seed one with the known API_KEY plaintext so fixtures can use it."""
    from datetime import datetime, timedelta, timezone
    from app.repositories.registration_key_repository import RegistrationKeyRepository
    from app.services.security_service import SecurityService

    now = datetime.now(timezone.utc)
    RegistrationKeyRepository(flask_app.config['DB']).keys.insert_one({
        'name': 'testkey',
        'keyHash': SecurityService.generate_blind_index(API_KEY),
        'keyEnc': SecurityService.encrypt(API_KEY),
        'keyHint': API_KEY.rsplit('-', 1)[-1],
        'status': 'active',
        'startsAt': now - timedelta(days=1),
        'expiresAt': now + timedelta(days=365),
        'createdAt': now,
        'expiredAt': None,
    })


@pytest.fixture(scope='session')
def app():
    from pymongo import MongoClient
    from app.config import Config

    MongoClient(Config.MONGODB_URI).drop_database(Config.DATABASE_NAME)

    from app import create_app
    flask_app = create_app()
    flask_app.config['TESTING'] = True
    _seed_desktop_key(flask_app)

    yield flask_app

    MongoClient(Config.MONGODB_URI).drop_database(Config.DATABASE_NAME)


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def db(app):
    return app.config['DB']


def unique(prefix):
    return f'{prefix}-{uuid.uuid4().hex[:10]}'


@pytest.fixture()
def user(client):
    """A fresh registered user: returns dict with token, id, email, etc."""
    email = f'{unique("user")}@example.com'
    username = unique('user')
    response = client.post('/v1/auth/register', json={
        'username': username,
        'name': 'Test User',
        'email': email,
        'password': 'hunter22',
        'age': 34,
        'gender': 'female',
        'ethnicity': 'other',
        'has_insurance': True,
        'last_doctor_visit': '2026-01-15',
    })
    assert response.status_code == 201, response.get_json()
    body = response.get_json()
    return {
        'id': body['id'],
        'token': body['token'],
        'email': email,
        'username': username,
        'password': 'hunter22',
        'headers': {'Authorization': f"Bearer {body['token']}"},
    }


@pytest.fixture()
def desktop_headers():
    return {'X-API-Key': API_KEY, 'X-Device-Id': 'test-device-1'}


@pytest.fixture()
def master_headers():
    return {'X-Master-Key': MASTER_KEY}
