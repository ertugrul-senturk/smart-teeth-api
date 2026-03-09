from datetime import datetime
from bson import ObjectId

class DataRecord:
    def __init__(self, user_id, key, data, _id=None, deleted=False, created_at=None, updated_at=None):
        self._id = _id if _id else ObjectId()
        self.user_id = user_id
        self.key = key
        self.data = data
        self.deleted = deleted
        self.created_at = created_at if created_at else datetime.utcnow()
        self.updated_at = updated_at if updated_at else datetime.utcnow()

    def to_dict(self):
        return {
            '_id': self._id,
            'user_id': self.user_id,
            'key': self.key,
            'data': self.data,
            'deleted': self.deleted,
            'created_at': self.created_at,
            'updated_at': self.updated_at
        }

    def to_json(self):
        return {
            '_id': str(self._id),
            'key': self.key,
            'data': self.data,
            'deleted': self.deleted,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }

    @staticmethod
    def from_dict(data):
        return DataRecord(
            _id=data.get('_id'),
            user_id=data.get('user_id'),
            key=data.get('key'),
            data=data.get('data'),
            deleted=data.get('deleted', False),
            created_at=data.get('created_at'),
            updated_at=data.get('updated_at')
        )


