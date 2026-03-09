from datetime import datetime
from bson import ObjectId

class ScanRecord:
    def __init__(self, user_id, local_id, timestamp, summary, detection_count,
                 main_image=None, tooth_images=None, _id=None, deleted=False,
                 created_at=None, updated_at=None):
        self._id = _id if _id else ObjectId()
        self.user_id = user_id
        self.local_id = local_id
        self.timestamp = timestamp
        self.summary = summary
        self.detection_count = detection_count
        self.main_image = main_image
        self.tooth_images = tooth_images or []
        self.deleted = deleted
        self.created_at = created_at if created_at else datetime.utcnow()
        self.updated_at = updated_at if updated_at else datetime.utcnow()

    def to_dict(self):
        return {
            '_id': self._id,
            'user_id': self.user_id,
            'local_id': self.local_id,
            'timestamp': self.timestamp,
            'summary': self.summary,
            'detection_count': self.detection_count,
            'main_image': self.main_image,
            'tooth_images': self.tooth_images,
            'deleted': self.deleted,
            'created_at': self.created_at,
            'updated_at': self.updated_at
        }

    def to_json(self):
        return {
            '_id': str(self._id),
            'local_id': self.local_id,
            'timestamp': self.timestamp,
            'summary': self.summary,
            'detection_count': self.detection_count,
            'deleted': self.deleted,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }

    @staticmethod
    def from_dict(data):
        return ScanRecord(
            _id=data.get('_id'),
            user_id=data.get('user_id'),
            local_id=data.get('local_id'),
            timestamp=data.get('timestamp'),
            summary=data.get('summary'),
            detection_count=data.get('detection_count'),
            main_image=data.get('main_image'),
            tooth_images=data.get('tooth_images', []),
            deleted=data.get('deleted', False),
            created_at=data.get('created_at'),
            updated_at=data.get('updated_at')
        )
