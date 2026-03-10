from datetime import datetime, timezone
from bson import ObjectId
import bcrypt
import json

from app.services.security_service import SecurityService


class User:
    def __init__(self, username, name, email, password, _id=None, created_at=None, updated_at=None,
                 age=None, gender=None, ethnicity=None, has_insurance=None, last_doctor_visit=None):
        self._id = _id if _id else ObjectId()
        self.username = username
        self.name = name
        self.email = email
        self.password = password
        self.age = age
        self.gender = gender
        self.ethnicity = ethnicity
        self.has_insurance = has_insurance
        self.last_doctor_visit = last_doctor_visit
        self.created_at = created_at if created_at else datetime.now(timezone.utc)
        self.updated_at = updated_at if updated_at else datetime.now(timezone.utc)

    def set_password(self, raw_password):
        salt = bcrypt.gensalt()
        self.password = bcrypt.hashpw(raw_password.encode(), salt).decode()

    def check_password(self, raw_password):
        return bcrypt.checkpw(raw_password.encode(), self.password.encode())

    def _build_profile_data(self):
        """Bundle optional demographic fields into a dict (only non-None values)."""
        profile = {}
        if self.age is not None:
            profile['a'] = self.age
        if self.gender is not None:
            profile['g'] = self.gender
        if self.ethnicity is not None:
            profile['e'] = self.ethnicity
        if self.has_insurance is not None:
            profile['i'] = self.has_insurance
        if self.last_doctor_visit is not None:
            profile['d'] = self.last_doctor_visit
        return profile

    @staticmethod
    def _parse_profile_data(profile):
        """Extract optional demographic fields from decrypted profile dict."""
        if not profile:
            return {}
        return {
            'age': profile.get('a'),
            'gender': profile.get('g'),
            'ethnicity': profile.get('e'),
            'has_insurance': profile.get('i'),
            'last_doctor_visit': profile.get('d'),
        }

    def to_dict(self):
        doc = {
            '_id': self._id,
            'u': SecurityService.encrypt(self.username),
            'n': SecurityService.encrypt(self.name),
            'e': SecurityService.encrypt(self.email),
            'p': self.password,

            'uh': SecurityService.generate_blind_index(self.username),
            'eh': SecurityService.generate_blind_index(self.email),

            'ca': self.created_at,
            'ua': self.updated_at,
        }

        profile_data = self._build_profile_data()
        if profile_data:
            doc['pd'] = SecurityService.encrypt(json.dumps(profile_data))

        return doc

    @staticmethod
    def from_dict(data):
        if not data:
            return None

        # Decrypt the profile blob
        profile = {}
        pd_encrypted = data.get('pd')
        if pd_encrypted:
            try:
                pd_json = SecurityService.decrypt(pd_encrypted)
                profile = json.loads(pd_json)
            except Exception:
                profile = {}

        parsed = User._parse_profile_data(profile)

        return User(
            _id=data.get('_id'),
            username=SecurityService.decrypt(data.get('u')),
            name=SecurityService.decrypt(data.get('n')),
            email=SecurityService.decrypt(data.get('e')),
            password=data.get('p'),
            age=parsed.get('age'),
            gender=parsed.get('gender'),
            ethnicity=parsed.get('ethnicity'),
            has_insurance=parsed.get('has_insurance'),
            last_doctor_visit=parsed.get('last_doctor_visit'),
            created_at=data.get('ca'),
            updated_at=data.get('ua'),
        )

    def to_json(self):
        return {
            'id': str(self._id),
            'username': self.username,
            'name': self.name,
            'email': self.email,
            'age': self.age,
            'gender': self.gender,
            'ethnicity': self.ethnicity,
            'has_insurance': self.has_insurance,
            'last_doctor_visit': self.last_doctor_visit,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }