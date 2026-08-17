from app.repositories.sync_repository import SyncRepository, ImageRepository
from app.config import Config
from app.config.collections import ALLOWED_SYNC_COLLECTIONS


class ImageTooLargeError(ValueError):
    """Raised when an uploaded image exceeds Config.MAX_IMAGE_MB. Mapped to
    HTTP 413 by controllers so oversized uploads fail cleanly instead of
    hitting Mongo's 16MB document cap as a 500."""
    pass


def check_image_size(base64_data):
    """Validate a base64 payload against the per-image cap."""
    if not base64_data:
        return
    # Decoded size ≈ 3/4 of the base64 length.
    approx_bytes = (len(base64_data) * 3) // 4
    limit = Config.MAX_IMAGE_MB * 1024 * 1024
    if approx_bytes > limit:
        raise ImageTooLargeError(
            f'Image exceeds the {Config.MAX_IMAGE_MB}MB limit'
        )


class SyncService:

    def __init__(self, db):
        self.sync_repo = SyncRepository(db)
        self.image_repo = ImageRepository(db)

    def sync_step_1(self, user_id, client_data):
        missing_data = {}
        required_ids = {}
        deleted_ids = {}

        for collection_key, client_ids in client_data.items():
            # Collection keys come from client JSON — ignore anything outside
            # the sync allowlist so auth collections can't be read this way.
            if collection_key not in ALLOWED_SYNC_COLLECTIONS:
                continue

            if not client_ids:
                server_data = self.sync_repo.find_missing_data(user_id, collection_key, [])
                if server_data:
                    missing_data[collection_key] = server_data
                continue

            existing_on_server = self.sync_repo.find_existing_ids(
                user_id, collection_key, client_ids
            )

            # Ids the client holds that are tombstoned here — the client must
            # purge these locally, NOT re-upload them (they would otherwise
            # land in requiredIds and resurrect the deleted record).
            tombstoned = self.sync_repo.find_deleted_ids(
                user_id, collection_key, client_ids
            )
            if tombstoned:
                deleted_ids[collection_key] = tombstoned
            tombstoned_set = set(tombstoned)

            client_ids_set = set(str(cid) for cid in client_ids)
            ids_missing_on_server = list(
                client_ids_set - existing_on_server - tombstoned_set
            )

            if ids_missing_on_server:
                required_ids[collection_key] = ids_missing_on_server

            server_only_data = self.sync_repo.find_missing_data(
                user_id, collection_key, client_ids
            )

            if server_only_data:
                missing_data[collection_key] = server_only_data

        return {
            'missingData': missing_data,
            'requiredIds': required_ids,
            'deletedIds': deleted_ids,
        }

    def sync_step_2(self, user_id, client_data):
        inserted = {}
        skipped = {}
        errors = {}

        for collection_key, documents in client_data.items():
            if not documents:
                continue

            if collection_key not in ALLOWED_SYNC_COLLECTIONS:
                errors[collection_key] = ['Unknown collection']
                continue

            try:
                inserted_ids, skipped_ids = self.sync_repo.bulk_insert(
                    user_id, collection_key, documents
                )
                if inserted_ids:
                    inserted[collection_key] = inserted_ids
                if skipped_ids:
                    skipped[collection_key] = skipped_ids
            except Exception:
                if collection_key not in errors:
                    errors[collection_key] = []
                errors[collection_key].append('Failed to store documents')

        return {
            'inserted': inserted,
            # Deleted-on-server or stale — the client should stop re-sending
            # these (clear them from its dirty queue).
            'skipped': skipped if skipped else None,
            'errors': errors if errors else None
        }

    def upload_image(self, user_id, image_data):
        if not image_data:
            return {'imageId': None}

        check_image_size(image_data.get('base64', ''))
        image_id = self.image_repo.store_image(user_id, image_data)
        return {
            'imageId': image_id,
        }

    def download_images(self, user_id, image_ids):
        if not image_ids:
            return []

        return self.image_repo.get_images_by_ids(user_id, image_ids)

    def mark_delete(self, user_id, collection_key, ids):
        if collection_key not in ALLOWED_SYNC_COLLECTIONS:
            raise ValueError('Unknown collection')

        if not ids:
            return {'modified': 0}

        modified_count = self.sync_repo.mark_as_deleted(user_id, collection_key, ids)

        return {'modified': modified_count}