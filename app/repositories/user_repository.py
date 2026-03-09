from bson import ObjectId
from datetime import datetime
from app.config import db_instance
from app.models import User
from app.services.security_service import SecurityService


class UserRepository:
    def __init__(self):
        self.collection = db_instance.get_db()['users']
        self._create_indexes()

    def _create_indexes(self):
        self.collection.create_index('email_hash', unique=True)
        self.collection.create_index('username_hash', unique=True)

    def create(self, user):
        user_dict = user.to_dict()
        result = self.collection.insert_one(user_dict)
        user._id = result.inserted_id
        return user

    def find_by_id(self, user_id):
        if isinstance(user_id, str):
            user_id = ObjectId(user_id)

        user_data = self.collection.find_one({'_id': user_id})
        return User.from_dict(user_data) if user_data else None

    def find_by_email(self, email):
        email_hash = SecurityService.generate_blind_index(email)
        user_data = self.collection.find_one({'email_hash': email_hash})
        return User.from_dict(user_data) if user_data else None

    def find_by_username(self, username):
        username_hash = SecurityService.generate_blind_index(username)
        user_data = self.collection.find_one({'username_hash': username_hash})
        return User.from_dict(user_data) if user_data else None

    def update(self, user_id, update_data):
        if isinstance(user_id, str):
            user_id = ObjectId(user_id)

        if 'email' in update_data:
            update_data['email_hash'] = SecurityService.generate_blind_index(update_data['email'])
            update_data['email'] = SecurityService.encrypt(update_data['email'])

        if 'username' in update_data:
            update_data['username_hash'] = SecurityService.generate_blind_index(update_data['username'])
            update_data['username'] = SecurityService.encrypt(update_data['username'])

        if 'name' in update_data:
            update_data['name'] = SecurityService.encrypt(update_data['name'])

        update_data['updated_at'] = datetime.utcnow()

        result = self.collection.update_one(
            {'_id': user_id},
            {'$set': update_data}
        )
        return result.modified_count > 0

    def delete(self, user_id):
        if isinstance(user_id, str):
            user_id = ObjectId(user_id)
        result = self.collection.delete_one({'_id': user_id})
        return result.deleted_count > 0

    def exists_by_email(self, email):
        email_hash = SecurityService.generate_blind_index(email)
        return self.collection.count_documents({'email_hash': email_hash}) > 0

    def exists_by_username(self, username):

        username_hash = SecurityService.generate_blind_index(username)
        return self.collection.count_documents({'username_hash': username_hash}) > 0