from datetime import datetime, timezone

from app.config.collections import (
    DESKTOP_HISTORY_COLLECTION,
    DESKTOP_IMAGES_COLLECTION,
)


class DesktopRepository:
    """Storage for the desktop app. Documents are keyed on (deviceId, id) —
    the desktop app has no user accounts, so a persistent per-install device
    UUID takes the place of userId."""

    def __init__(self, db):
        self.history = db[DESKTOP_HISTORY_COLLECTION]
        self.images = db[DESKTOP_IMAGES_COLLECTION]

    def upsert_entries(self, device_id, entries):
        synced_ids = []
        errors = []

        for entry in entries:
            if not isinstance(entry, dict) or not entry.get('id'):
                errors.append({'id': None, 'message': 'Entry is missing an id'})
                continue

            entry = dict(entry)
            entry.pop('_id', None)
            entry['deviceId'] = device_id
            entry['updatedAt'] = datetime.now(timezone.utc)

            try:
                self.history.update_one(
                    {'deviceId': device_id, 'id': entry['id']},
                    {
                        '$set': entry,
                        '$setOnInsert': {'createdAt': datetime.now(timezone.utc)},
                    },
                    upsert=True
                )
                synced_ids.append(entry['id'])
            except Exception as e:
                errors.append({'id': entry.get('id'), 'message': str(e)})

        return synced_ids, errors

    def upsert_images(self, device_id, images):
        image_ids = []

        for image in images:
            if not isinstance(image, dict) or not image.get('id'):
                continue

            self.images.update_one(
                {'deviceId': device_id, 'id': image['id']},
                {
                    '$set': {
                        'deviceId': device_id,
                        'id': image['id'],
                        'base64Data': image.get('base64', ''),
                        'mimeType': image.get('mimeType', 'image/png'),
                        'updatedAt': datetime.now(timezone.utc),
                    },
                    '$setOnInsert': {'createdAt': datetime.now(timezone.utc)},
                },
                upsert=True
            )
            image_ids.append(image['id'])

        return image_ids
