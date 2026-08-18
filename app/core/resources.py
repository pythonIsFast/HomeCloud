"""The generic resource registry -- shared by every HomeCloud service.

There is one table for all services (see app/schema.sql). A service does not
create its own table; it stores rows with its own ``service_type`` and puts
whatever it needs into ``config_json``.

Future services should use these functions instead of writing their own SQL,
so that audit logging and the JSON shape stay consistent everywhere.
"""

import json

from .. import db

# Lifecycle states used across services. A service may use a subset.
STATUSES = ("pending", "creating", "running", "stopped", "error", "deleted")


def to_dict(row):
    """Convert a resources row into a JSON-serializable dict.

    config_json is parsed into a real object so the frontend does not have to
    deal with a string containing JSON.
    """
    try:
        config = json.loads(row["config_json"] or "{}")
    except json.JSONDecodeError:
        # Never let one corrupt row break the whole listing.
        config = {"_invalid_config_json": row["config_json"]}

    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "service_type": row["service_type"],
        "name": row["name"],
        "status": row["status"],
        "config": config,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create(user_id, service_type, name, config=None, status="pending"):
    """Register a new resource and return its id."""
    return db.execute(
        "INSERT INTO resources (user_id, service_type, name, status, config_json)"
        " VALUES (?, ?, ?, ?, ?)",
        (user_id, service_type, name, status, json.dumps(config or {})),
    )


def list_for_user(user_id, service_type=None):
    """All resources of a user, optionally narrowed to one service type."""
    if service_type is None:
        return db.query(
            "SELECT * FROM resources WHERE user_id = ? ORDER BY id DESC", (user_id,)
        )
    return db.query(
        "SELECT * FROM resources WHERE user_id = ? AND service_type = ?"
        " ORDER BY id DESC",
        (user_id, service_type),
    )


def get(user_id, resource_id):
    """One resource, scoped to its owner so users cannot read each other's rows."""
    return db.query(
        "SELECT * FROM resources WHERE id = ? AND user_id = ?",
        (resource_id, user_id),
        one=True,
    )


def set_status(resource_id, status):
    db.execute(
        "UPDATE resources SET status = ?, updated_at = datetime('now') WHERE id = ?",
        (status, resource_id),
    )


def set_config(resource_id, config):
    """Replace the whole config object (read-modify-write in the caller)."""
    db.execute(
        "UPDATE resources SET config_json = ?, updated_at = datetime('now')"
        " WHERE id = ?",
        (json.dumps(config or {}), resource_id),
    )


def delete(user_id, resource_id):
    """Hard delete. Returns True if a row was removed."""
    database = db.get_db()
    cursor = database.execute(
        "DELETE FROM resources WHERE id = ? AND user_id = ?", (resource_id, user_id)
    )
    database.commit()
    deleted = cursor.rowcount > 0
    cursor.close()
    return deleted
