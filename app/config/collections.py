# Collections the mobile sync endpoints are allowed to touch. Collection keys
# arrive in client JSON, so anything outside this set (e.g. 'users') is
# rejected before it reaches the database.
ALLOWED_SYNC_COLLECTIONS = frozenset({
    'parents_cavity_risk',
    'baby_cavity_risk',
    'plan_parent',
    'plan_baby',
    'tooth_scan_history',
    'tell_us_about_you_data',
})

# Desktop app data lives in its own collections, keyed by deviceId instead of
# userId (the desktop app has no login).
DESKTOP_HISTORY_COLLECTION = 'desktop_history'
DESKTOP_IMAGES_COLLECTION = 'desktop_images'

# Mobile app images (uploaded via /v1/sync/images), keyed by userId.
IMAGES_COLLECTION = 'images'

# Provenance ledger: which mobile records have been imported into which
# desktop install, and whether they've been edited since. Powers the
# "already moved to desktop" badges.
IMPORT_LINKS_COLLECTION = 'import_links'

# Immutable trail of desktop-side reads/imports/exports of patient data.
AUDIT_LOG_COLLECTION = 'audit_log'

# Registration keys (licensing): keys the master user creates from the desktop
# admin tab. The key itself is stored as a blind-index hash (lookup) plus an
# encrypted copy (reveal); see app/repositories/registration_key_repository.py.
REGISTRATION_KEYS_COLLECTION = 'registration_keys'

# One row per (key, device) install: who registered with the key (labeler
# name), when it was first/last seen, and how many sessions (app starts).
KEY_INSTALLS_COLLECTION = 'key_installs'

# OTA app updates: releases published by the master user from the desktop
# admin tab. Installer binaries are stored in our own chunk store (Mongo's
# 16MB document cap → 8MB chunks) — see app/repositories/update_repository.py.
APP_UPDATES_COLLECTION = 'app_updates'
UPDATE_ASSETS_COLLECTION = 'update_assets'
UPDATE_ASSET_CHUNKS_COLLECTION = 'update_asset_chunks'

# Shared dataset workspace (server-authoritative, synchronized across every
# desktop installation). Items/datasets are versioned + tombstoned; images
# live content-addressed by sha256, masks in per-item docs — see
# app/repositories/dataset_repository.py.
DATASETS_COLLECTION = 'datasets'
DATASET_ITEMS_COLLECTION = 'dataset_items'
DATASET_ASSETS_COLLECTION = 'dataset_assets'
DATASET_MASKS_COLLECTION = 'dataset_masks'
DATASET_EVENTS_COLLECTION = 'dataset_label_events'
DATASET_EXPORTS_COLLECTION = 'dataset_exports'
