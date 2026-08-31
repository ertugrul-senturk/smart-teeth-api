from app.config.collections import ALLOWED_SYNC_COLLECTIONS
from app.repositories.desktop_repository import DesktopRepository
from app.repositories.patient_repository import PatientRepository, ImportLinkRepository
from app.repositories.sync_repository import ImageRepository, _parse_client_ts
from app.services.sync_service import check_image_size


class DesktopSyncService:

    def __init__(self, db):
        self.desktop_repo = DesktopRepository(db)
        self.patient_repo = PatientRepository(db)
        self.link_repo = ImportLinkRepository(db)
        self.image_repo = ImageRepository(db)

    def sync_entries(self, device_id, entries):
        synced_ids, errors = self.desktop_repo.upsert_entries(device_id, entries)

        # Entries imported from mobile carry an origin block; when the
        # desktop stamps origin.editedAt (dentist edited after import), the
        # provenance ledger records it so every install sees "edited".
        synced_set = set(synced_ids)
        for entry in entries:
            if not isinstance(entry, dict) or entry.get('id') not in synced_set:
                continue
            origin = entry.get('origin')
            if not isinstance(origin, dict):
                continue
            edited_at = _parse_client_ts(origin.get('editedAt'))
            if edited_at:
                self.link_repo.mark_edited(device_id, entry['id'], edited_at)

        return {
            'synced': synced_ids,
            'errors': errors if errors else None,
        }

    def upload_images(self, device_id, images):
        # Reject before any write: one oversized image fails the whole
        # request (413) so the desktop app reports it per-entry.
        for image in images:
            if isinstance(image, dict):
                check_image_size(image.get('base64', ''))
        image_ids = self.desktop_repo.upsert_images(device_id, images)
        return {'imageIds': image_ids}

    # ── Patient browsing (dentist reads of mobile data) ──────────────────

    def list_patients(self, search=None, page=None, page_size=None):
        """Legacy full list without `page`; a name-sorted, searchable page
        (plus the total) with it — the shape the desktop's paginated patient
        browser reads."""
        if page is None:
            return {'patients': self.patient_repo.list_patients()}
        patients, total = self.patient_repo.search_patients(search, page, page_size)
        return {
            'patients': patients,
            'total': total,
            'page': page,
            'pageSize': page_size,
        }

    def patient_records(self, user_id, collection_key, date_from=None, date_to=None,
                        page=None, page_size=None, include_trend=False):
        """Legacy full list without `page`; with it, one newest-first page plus
        the filtered total — and, with include_trend, lightweight whole-history
        metric rows for the progress sparklines."""
        if collection_key not in ALLOWED_SYNC_COLLECTIONS:
            raise ValueError('Unknown collection')
        if not self.patient_repo.get_patient(user_id):
            raise LookupError('Patient not found')

        skip = (page - 1) * page_size if page else None
        records = self.patient_repo.get_records(
            user_id, collection_key, date_from, date_to,
            skip=skip, limit=page_size if page else None,
        )
        links = self.link_repo.links_for_user(user_id, collection_key)

        for record in records:
            record_id = str(record.get('id') or record.get('_id'))
            record_links = links.get(record_id, [])
            if not record_links:
                record['importStatus'] = 'none'
                continue
            edited = any(link.get('lastEditedAt') for link in record_links)
            record['importStatus'] = 'edited' if edited else 'imported'
            record['imports'] = [{
                'deviceId': link.get('deviceId'),
                'keyName': link.get('keyName'),
                'desktopEntryId': link.get('desktopEntryId'),
                'importedAt': link['importedAt'].isoformat() if link.get('importedAt') else None,
                'lastEditedAt': link['lastEditedAt'].isoformat() if link.get('lastEditedAt') else None,
            } for link in record_links]

        if page is None:
            return {'records': records}

        result = {
            'records': records,
            'total': self.patient_repo.count_records(
                user_id, collection_key, date_from, date_to,
            ),
            'page': page,
            'pageSize': page_size,
        }
        if include_trend:
            result['trend'] = self.patient_repo.get_trend(user_id, collection_key)
        return result

    def fetch_patient_images(self, user_id, image_ids):
        """Batch image fetch, scoped to one patient — the repo query is
        (userId AND _id in ids), so ids belonging to other users return
        nothing rather than leaking."""
        if not self.patient_repo.get_patient(user_id):
            raise LookupError('Patient not found')
        images = self.image_repo.get_images_by_ids(user_id, image_ids or [])
        return {'images': images}

    # ── Permanent deletion (dentist-initiated, desktop app only) ──────────

    def delete_patient_records(self, user_id, collection_key, record_ids):
        if collection_key not in ALLOWED_SYNC_COLLECTIONS:
            raise ValueError('Unknown collection')
        if not self.patient_repo.get_patient(user_id):
            raise LookupError('Patient not found')
        removed = self.patient_repo.delete_records(user_id, collection_key, record_ids)
        return {'removed': removed}

    def delete_patient(self, user_id, delete_account=False):
        if not self.patient_repo.get_patient(user_id):
            raise LookupError('Patient not found')
        removed, account_deleted = self.patient_repo.purge_patient(user_id, delete_account)
        return {'removed': removed, 'accountDeleted': account_deleted}

    def register_import(self, source_collection, source_user_id, source_record_id,
                        device_id, desktop_entry_id, key_name=None):
        if source_collection not in ALLOWED_SYNC_COLLECTIONS:
            raise ValueError('Unknown collection')
        if not self.patient_repo.get_patient(source_user_id):
            raise LookupError('Patient not found')

        link = self.link_repo.register(
            source_collection, source_user_id, source_record_id,
            device_id, desktop_entry_id, key_name,
        )
        return {
            'link': {
                'sourceCollection': link['sourceCollection'],
                'sourceUserId': link['sourceUserId'],
                'sourceRecordId': link['sourceRecordId'],
                'deviceId': link['deviceId'],
                'desktopEntryId': link['desktopEntryId'],
                'importedAt': link['importedAt'].isoformat() if link.get('importedAt') else None,
                'lastEditedAt': link['lastEditedAt'].isoformat() if link.get('lastEditedAt') else None,
            }
        }
