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
STATUSES = (
    "pending",
    "creating",
    "running",
    "stopping",
    "stopped",
    "deleting",
    "error",
    "deleted",
)

# Page sizes for the keyset pagination below. MAX is a hard ceiling so a client
# cannot ask for "limit=1000000" and pull the whole table.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


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


def list_page(user_id, service_type=None, before_id=None, limit=DEFAULT_PAGE_SIZE):
    """One page of a user's resources, newest first.

    Keyset pagination, not OFFSET: the caller passes the smallest id it has
    already seen as ``before_id`` and we continue below it. OFFSET would make
    page 200 scan and discard 10 000 rows; a keyset query always touches only
    ``limit`` index entries, so page 200 costs exactly as much as page 1.
    """
    limit = max(1, min(int(limit), MAX_PAGE_SIZE))

    clauses = ["user_id = ?"]
    params = [user_id]
    if service_type is not None:
        clauses.append("service_type = ?")
        params.append(service_type)
    if before_id is not None:
        clauses.append("id < ?")
        params.append(int(before_id))

    # One row more than requested tells the caller whether a next page exists
    # without a second COUNT query.
    params.append(limit + 1)
    rows = db.query(
        "SELECT * FROM resources WHERE " + " AND ".join(clauses)
        + " ORDER BY id DESC LIMIT ?",
        tuple(params),
    )

    has_more = len(rows) > limit
    return list(rows[:limit]), has_more


def list_for_user(user_id, service_type=None):
    """All resources of a user, newest first.

    Only for internal callers that genuinely need every row (quota sums). API
    endpoints must use list_page() -- an unbounded list is a denial of service
    waiting for the first user with a few thousand resources.
    """
    if service_type is None:
        return db.query(
            "SELECT * FROM resources WHERE user_id = ? ORDER BY id DESC", (user_id,)
        )
    return db.query(
        "SELECT * FROM resources WHERE user_id = ? AND service_type = ?"
        " ORDER BY id DESC",
        (user_id, service_type),
    )


def count(user_id, service_type=None, exclude_status=()):
    """Number of resources of a user, for quota checks."""
    clauses = ["user_id = ?"]
    params = [user_id]
    if service_type is not None:
        clauses.append("service_type = ?")
        params.append(service_type)
    for status in exclude_status:
        clauses.append("status != ?")
        params.append(status)

    row = db.query(
        "SELECT COUNT(*) AS n FROM resources WHERE " + " AND ".join(clauses),
        tuple(params),
        one=True,
    )
    return row["n"]


def get(user_id, resource_id):
    """One resource, scoped to its owner so users cannot read each other's rows."""
    return db.query(
        "SELECT * FROM resources WHERE id = ? AND user_id = ?",
        (resource_id, user_id),
        one=True,
    )


def get_any(resource_id):
    """One resource regardless of owner -- for the privileged worker only."""
    return db.query("SELECT * FROM resources WHERE id = ?", (resource_id,), one=True)


def query_running(service_type):
    """Every resource of a service that claims to be running, across all users.

    Used by the VM worker to reconcile the registry with the processes that are
    actually alive. Worker-only, hence no user scoping.
    """
    return db.query(
        "SELECT * FROM resources WHERE service_type = ? AND status = 'running'"
        " ORDER BY id",
        (service_type,),
    )


def get_by_name(user_id, service_type, name):
    return db.query(
        "SELECT * FROM resources WHERE user_id = ? AND service_type = ? AND name = ?",
        (user_id, service_type, name),
        one=True,
    )


def set_status(resource_id, status):
    db.execute(
        "UPDATE resources SET status = ?, updated_at = datetime('now') WHERE id = ?",
        (status, resource_id),
    )


def transition(resource_id, expected_status, new_status):
    """Atomically move a resource from one status to another.

    Returns True only if the row really was in ``expected_status``. Two parallel
    "start" requests therefore produce exactly one job: the second one sees
    rowcount 0 and is rejected with a conflict. ``expected_status`` may be a
    string or a tuple of acceptable current values.
    """
    if isinstance(expected_status, str):
        expected_status = (expected_status,)

    placeholders = ", ".join("?" for _ in expected_status)
    changed = db.modify(
        "UPDATE resources SET status = ?, updated_at = datetime('now')"
        f" WHERE id = ? AND status IN ({placeholders})",
        (new_status, resource_id, *expected_status),
    )
    return changed == 1


def merge_config(resource_id, updates):
    """Read-modify-write a few keys of config_json, keeping the rest.

    Called by the worker to record what it allocated (pid, ip, tap, ...).
    Not safe against two concurrent writers by design -- only the single VM
    worker writes here.
    """
    row = get_any(resource_id)
    if row is None:
        return None
    try:
        config = json.loads(row["config_json"] or "{}")
    except json.JSONDecodeError:
        config = {}
    config.update(updates)
    set_config(resource_id, config)
    return config


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
