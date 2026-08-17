from datetime import datetime, timezone

from app.config import db_instance, AUDIT_LOG_COLLECTION


def audit(action, actor, **details):
    """Append an entry to the audit trail.

    `actor` identifies who acted: for desktop endpoints use the API key name
    plus deviceId (e.g. "desktop:default/abc-123"), for user endpoints the
    user id. Auditing must never break the request it documents — failures
    are swallowed after a console warning.
    """
    try:
        db_instance.get_db()[AUDIT_LOG_COLLECTION].insert_one({
            'ts': datetime.now(timezone.utc),
            'action': action,
            'actor': actor,
            'details': details or {},
        })
    except Exception as e:
        print(f"WARNING: audit write failed for '{action}': {e}")
