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
