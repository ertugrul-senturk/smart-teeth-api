from app.repositories.sync_repository import SyncRepository, ImageRepository


class SyncService:

    def __init__(self, db):
        self.sync_repo = SyncRepository(db)
        self.image_repo = ImageRepository(db)

    def sync_step_1(self, user_id, client_data):
        missing_data = {}
        required_ids = {}

        for collection_key, client_ids in client_data.items():
            if not client_ids:
                server_data = self.sync_repo.find_missing_data(user_id, collection_key, [])
                if server_data:
                    missing_data[collection_key] = server_data
                continue

            existing_on_server = self.sync_repo.find_existing_ids(
                user_id, collection_key, client_ids
            )

            client_ids_set = set(str(cid) for cid in client_ids)
            ids_missing_on_server = list(client_ids_set - existing_on_server)

            if ids_missing_on_server:
                required_ids[collection_key] = ids_missing_on_server

            server_only_data = self.sync_repo.find_missing_data(
                user_id, collection_key, client_ids
            )

            if server_only_data:
                missing_data[collection_key] = server_only_data

        return {
            'missingData': missing_data,
            'requiredIds': required_ids
        }

    def sync_step_2(self, user_id, client_data):
        inserted = {}
        errors = {}

        for collection_key, documents in client_data.items():
            if not documents:
                continue

            try:
                inserted_ids = self.sync_repo.bulk_insert(
                    user_id, collection_key, documents
                )
                if inserted_ids:
                    inserted[collection_key] = inserted_ids
            except Exception as e:
                if collection_key not in errors:
                    errors[collection_key] = []
                errors[collection_key].append(str(e))

        return {
            'inserted': inserted,
            'errors': errors if errors else None
        }

    def upload_image(self, user_id, image_data):
        if not image_data:
            return {'imageId': None}

        image_id = self.image_repo.store_image(user_id, image_data)
        return {
            'imageId': image_id,
        }

    def download_images(self, user_id, image_ids):
        if not image_ids:
            return []

        return self.image_repo.get_images_by_ids(user_id, image_ids)

    def mark_delete(self, user_id, collection_key, ids):
        if not ids:
            return {'modified': 0}

        modified_count = self.sync_repo.mark_as_deleted(user_id, collection_key, ids)

        return {'modified': modified_count}


class ImageService:

    def __init__(self, db):
        self.image_repo = ImageRepository(db)

    def upload_images_from_base64(self, user_id, images):
        return self.image_repo.store_image(user_id, images)

    def get_images(self, user_id, image_ids):
        return self.image_repo.get_images_by_ids(user_id, image_ids)