"""Append-only audit trail.

Cross-cutting helper (like db.py): both auth and every future service write
here, so it lives at package level to keep the import graph one-directional
(auth -> audit, core -> audit, never the other way round).
"""

import json

import click

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


def recent(limit=50, user_id=None, before_id=None):
    """Newest audit entries, optionally filtered by user, keyset-paged."""
    limit = max(1, min(int(limit), 200))
    clauses = []
    params = []

    if user_id is not None:
        clauses.append("user_id = ?")
        params.append(user_id)
    if before_id is not None:
        clauses.append("id < ?")
        params.append(int(before_id))

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit + 1)

    rows = db.query(
        "SELECT * FROM audit_log" + where + " ORDER BY id DESC LIMIT ?",
        tuple(params),
    )
    has_more = len(rows) > limit
    return list(rows[:limit]), has_more


def prune(keep_days=90):
    """Delete audit entries older than keep_days. Returns rows removed.

    The table is append-only and grows with every login, so an installation with
    thousands of users accumulates millions of rows per year. There is no
    background scheduler in this project by design -- run this from cron.
    """
    return db.modify(
        "DELETE FROM audit_log WHERE created_at < datetime('now', ?)",
        (f"-{int(keep_days)} days",),
    )


@click.command("prune-audit")
@click.option("--keep-days", default=90, show_default=True,
              help="Delete audit entries older than this many days.")
def prune_audit_command(keep_days):
    """CLI: flask --app app prune-audit [--keep-days 90]"""
    removed = prune(keep_days)
    # Reclaim the freed pages. VACUUM refuses to run inside a transaction, and
    # it needs free disk space equal to the database size, so a failure here is
    # reported rather than fatal -- the rows are already gone either way.
    connection = db.get_db()
    connection.commit()
    try:
        connection.execute("VACUUM")
    except Exception as error:  # sqlite3.OperationalError and friends
        click.echo(f"note: VACUUM skipped ({error})")
    click.echo(f"removed {removed} audit rows older than {keep_days} days")


def register(app):
    app.cli.add_command(prune_audit_command)
