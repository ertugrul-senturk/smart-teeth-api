from bson import ObjectId
from datetime import datetime, timezone
import json

from app.config import db_instance
from app.models import User
from app.services.security_service import SecurityService


class UserRepository:
    def __init__(self):
        self.collection = db_instance.get_db()['users']
        self._create_indexes()

    def _create_indexes(self):
        self.collection.create_index('eh', unique=True)
        self.collection.create_index('uh', unique=True)

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
        user_data = self.collection.find_one({'eh': email_hash})
        return User.from_dict(user_data) if user_data else None

    def find_by_username(self, username):
        username_hash = SecurityService.generate_blind_index(username)
        user_data = self.collection.find_one({'uh': username_hash})
        return User.from_dict(user_data) if user_data else None

    def update(self, user_id, update_data):
        """
        update_data uses logical field names (name, email, username, password, profile_data).
        This method encrypts and maps them to the short DB field names.
        """
        if isinstance(user_id, str):
            user_id = ObjectId(user_id)

        db_update = {}

        if 'email' in update_data:
            db_update['eh'] = SecurityService.generate_blind_index(update_data['email'])
            db_update['e'] = SecurityService.encrypt(update_data['email'])

        if 'username' in update_data:
            db_update['uh'] = SecurityService.generate_blind_index(update_data['username'])
            db_update['u'] = SecurityService.encrypt(update_data['username'])

        if 'name' in update_data:
            db_update['n'] = SecurityService.encrypt(update_data['name'])

        if 'password' in update_data:
            db_update['p'] = update_data['password']

        if 'profile_data' in update_data:
            pd = update_data['profile_data']
            if pd:
                db_update['pd'] = SecurityService.encrypt(json.dumps(pd))
            else:
                db_update['pd'] = None

        db_update['ua'] = datetime.now(timezone.utc)

        result = self.collection.update_one(
            {'_id': user_id},
            {'$set': db_update}
        )
        return result.modified_count > 0

    def delete(self, user_id):
        if isinstance(user_id, str):
            user_id = ObjectId(user_id)
        result = self.collection.delete_one({'_id': user_id})
        return result.deleted_count > 0

    def exists_by_email(self, email):
        email_hash = SecurityService.generate_blind_index(email)
        return self.collection.count_documents({'eh': email_hash}) > 0

    def exists_by_username(self, username):
        username_hash = SecurityService.generate_blind_index(username)
        return self.collection.count_documents({'uh': username_hash}) > 0