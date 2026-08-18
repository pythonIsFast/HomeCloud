"""Append-only audit trail.

Cross-cutting helper (like db.py): both auth and every future service write
here, so it lives at package level to keep the import graph one-directional
(auth -> audit, core -> audit, never the other way round).
"""

import json

from . import db


def log_action(user_id, action, resource_id=None, details=None):
    """Insert one row into audit_log.

    user_id     -- acting user, may be None for anonymous events
    action      -- short machine-readable verb, e.g. "user.login"
    resource_id -- related resources.id, if any
    details     -- dict, serialized to JSON (never put secrets in here)
    """
    db.execute(
        "INSERT INTO audit_log (user_id, action, resource_id, details_json)"
        " VALUES (?, ?, ?, ?)",
        (user_id, action, resource_id, json.dumps(details or {})),
    )


def recent(limit=50, user_id=None):
    """Return the newest audit entries, optionally filtered by user."""
    if user_id is None:
        return db.query(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        )
    return db.query(
        "SELECT * FROM audit_log WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    )
