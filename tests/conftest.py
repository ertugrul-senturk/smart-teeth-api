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
os.environ['DESKTOP_API_KEYS'] = 'testkey:test-api-key-123'
os.environ.setdefault('SECRET_KEY', 'test-secret-key-0123456789abcdef-0123456789abcdef')
os.environ.setdefault('MONGODB_URI', 'mongodb://localhost:27017')

import pytest  # noqa: E402

API_KEY = 'test-api-key-123'


@pytest.fixture(scope='session')
def app():
    from pymongo import MongoClient
    from app.config import Config

    MongoClient(Config.MONGODB_URI).drop_database(Config.DATABASE_NAME)

    from app import create_app
    flask_app = create_app()
    flask_app.config['TESTING'] = True

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
