"""Job queue for work that must not run inside a web request.

Why this exists: creating a microVM takes seconds, needs root (tap device, NAT
rule) and outlives the HTTP request that asked for it. A gunicorn worker is the
wrong place for that -- it is unprivileged, it gets recycled, and its child
processes would be orphaned.

So Flask only writes a row here and answers immediately. The privileged VM
worker (``python -m app.vmm``) claims rows and does the work. The queue is a
plain SQLite table; there is no Redis and no Celery, and none is needed for a
few jobs per second.

Claiming is safe against several workers because the UPDATE carries the
expected status: ``WHERE id = ? AND status = 'queued'``. Exactly one worker
gets rowcount 1, the others move on.
"""

import json
import socket

from . import db

ACTIONS = ("create", "start", "stop", "restart", "delete", "firewall")

# Give up after this many failed attempts so a permanently broken job does not
# spin forever.
MAX_ATTEMPTS = 3


def local_host_name():
    """Identifier this worker claims jobs under. One worker per compute host."""
    return socket.gethostname()


def enqueue(action, resource_id=None, user_id=None, payload=None, host=None):
    """Queue one job and return its id.

    ``host=None`` means any worker may take it. Once there is more than one
    compute host, the service pins a job to the host that owns the VM.
    """
    if action not in ACTIONS:
        raise ValueError(f"unknown job action: {action!r}")

    return db.execute(
        "INSERT INTO jobs (resource_id, user_id, action, payload_json, host)"
        " VALUES (?, ?, ?, ?, ?)",
        (resource_id, user_id, action, json.dumps(payload or {}), host),
    )


def claim_next(host):
    """Take the oldest runnable job for this host, or return None.

    Two steps on purpose: SELECT the candidate, then UPDATE it with the status
    still in the WHERE clause. If another worker won the race the UPDATE hits
    zero rows and we simply try again on the next poll.
    """
    row = db.query(
        "SELECT * FROM jobs WHERE status = 'queued' AND (host IS NULL OR host = ?)"
        " ORDER BY id LIMIT 1",
        (host,),
        one=True,
    )
    if row is None:
        return None

    changed = db.modify(
        "UPDATE jobs SET status = 'running', host = ?, attempts = attempts + 1,"
        " claimed_at = datetime('now') WHERE id = ? AND status = 'queued'",
        (host, row["id"]),
    )
    if changed != 1:
        return None  # someone else got it

    return db.query("SELECT * FROM jobs WHERE id = ?", (row["id"],), one=True)


def payload_of(job_row):
    try:
        return json.loads(job_row["payload_json"] or "{}")
    except json.JSONDecodeError:
        return {}


def finish(job_id):
    db.execute(
        "UPDATE jobs SET status = 'done', error = NULL, finished_at = datetime('now')"
        " WHERE id = ?",
        (job_id,),
    )


def fail(job_id, message):
    """Mark a job failed, or put it back in the queue if attempts are left."""
    row = db.query("SELECT attempts FROM jobs WHERE id = ?", (job_id,), one=True)
    attempts = row["attempts"] if row else MAX_ATTEMPTS

    if attempts < MAX_ATTEMPTS:
        db.execute(
            "UPDATE jobs SET status = 'queued', error = ? WHERE id = ?",
            (str(message)[:1000], job_id),
        )
        return False

    db.execute(
        "UPDATE jobs SET status = 'failed', error = ?, finished_at = datetime('now')"
        " WHERE id = ?",
        (str(message)[:1000], job_id),
    )
    return True


def for_resource(resource_id, limit=20):
    return db.query(
        "SELECT * FROM jobs WHERE resource_id = ? ORDER BY id DESC LIMIT ?",
        (resource_id, limit),
    )


def pending_for_resource(resource_id):
    """True while a job for this resource is still queued or running."""
    row = db.query(
        "SELECT COUNT(*) AS n FROM jobs WHERE resource_id = ?"
        " AND status IN ('queued', 'running')",
        (resource_id,),
        one=True,
    )
    return row["n"] > 0


def reset_stale_running(older_than_minutes=30):
    """Requeue jobs that a crashed worker left in 'running'.

    Called once at worker start-up. Without this a worker that was killed mid
    job would leave that VM stuck forever.
    """
    return db.modify(
        "UPDATE jobs SET status = 'queued' WHERE status = 'running'"
        " AND claimed_at < datetime('now', ?)",
        (f"-{int(older_than_minutes)} minutes",),
    )
