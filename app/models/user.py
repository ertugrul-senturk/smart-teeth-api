from datetime import datetime
from bson import ObjectId
import bcrypt

from app.services.security_service import SecurityService


class User:
    def __init__(self, username, name, email, password, _id=None, created_at=None, updated_at=None):
        self._id = _id if _id else ObjectId()
        self.username = username
        self.name = name
        self.email = email
        self.password = password
        self.created_at = created_at if created_at else datetime.utcnow()
        self.updated_at = updated_at if updated_at else datetime.utcnow()

    def set_password(self, raw_password):
        salt = bcrypt.gensalt()
        self.password = bcrypt.hashpw(raw_password.encode(), salt).decode()

    def check_password(self, raw_password):
        return bcrypt.checkpw(raw_password.encode(), self.password.encode())

    def to_dict(self):
        return {
            '_id': self._id,
            'username': SecurityService.encrypt(self.username),
            'name': SecurityService.encrypt(self.name),
            'email': SecurityService.encrypt(self.email),
            'password': self.password,  # Assumed already hashed via set_password

            'username_hash': SecurityService.generate_blind_index(self.username),
            'email_hash': SecurityService.generate_blind_index(self.email),

            'created_at': self.created_at,
            'updated_at': self.updated_at
        }

    @staticmethod
    def from_dict(data):
        """
        Decrypts fields when reading from DB.
        """
        if not data:
            return None

        return User(
            _id=data.get('_id'),
            # Decrypt fields to restore original values in the object
            username=SecurityService.decrypt(data.get('username')),
            name=SecurityService.decrypt(data.get('name')),
            email=SecurityService.decrypt(data.get('email')),
            password=data.get('password'),
            created_at=data.get('created_at'),
            updated_at=data.get('updated_at')
        )

    def to_json(self):
        # This remains the same, exposing decrypted data to your Frontend API
        return {
            'id': str(self._id),
            'username': self.username,
            'name': self.name,
            'email': self.email,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }