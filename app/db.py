"""Database access layer.

Everything goes through the standard library sqlite3 module with plain SQL.
No ORM, no Flask-SQLAlchemy (see CLAUDE.md, dependency policy).

The connection is cached on Flask's request-scoped ``g`` object, so every
request opens at most one connection and closes it when the request ends.
"""

import sqlite3

import click
from flask import current_app, g


def connect(path, timeout=5.0):
    """Open a tuned SQLite connection.

    Used both by the request-scoped helper below and by the privileged VM
    worker, which runs outside the Flask app context.

    The PRAGMAs matter more than anything else in this file:

      journal_mode=WAL  Readers no longer block the writer and vice versa. In
                        the default rollback journal a single INSERT freezes
                        every concurrent SELECT. WAL is persisted in the
                        database file, so setting it repeatedly is harmless.
      busy_timeout      Without it a lock conflict fails instantly with
                        "database is locked"; with it the connection waits.
      synchronous=NORMAL In WAL mode this is the documented safe setting: it
                        can lose the last transactions on a power cut, but
                        cannot corrupt the database.
      foreign_keys=ON   SQLite ignores foreign keys unless asked, per
                        connection.
    """
    connection = sqlite3.connect(
        path,
        timeout=timeout,
        # Let sqlite3 parse DATE/TIMESTAMP declared columns; we mostly use TEXT.
        detect_types=sqlite3.PARSE_DECLTYPES,
    )
    # Rows behave like dicts: row["email"] instead of row[1].
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def get_db():
    """Return the sqlite3 connection for the current request (opening it lazily)."""
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(exception=None):
    """Close the request connection. Registered as a teardown handler."""
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query(sql, params=(), *, one=False):
    """Run a SELECT and return a list of rows (or a single row / None if one=True)."""
    cursor = get_db().execute(sql, params)
    rows = cursor.fetchall()
    cursor.close()
    if one:
        return rows[0] if rows else None
    return rows


def execute(sql, params=()):
    """Run an INSERT/UPDATE/DELETE, commit, and return the last inserted row id."""
    db = get_db()
    cursor = db.execute(sql, params)
    db.commit()
    last_id = cursor.lastrowid
    cursor.close()
    return last_id


def modify(sql, params=()):
    """Like execute(), but returns how many rows changed.

    This is how every state transition is made safe against two concurrent
    requests: the UPDATE carries the expected old value in its WHERE clause and
    the caller checks whether it actually hit a row. Exactly one of two racing
    "start this VM" requests gets rowcount 1.
    """
    db = get_db()
    cursor = db.execute(sql, params)
    db.commit()
    changed = cursor.rowcount
    cursor.close()
    return changed


def init_db():
    """Create all tables from schema.sql. Safe to run repeatedly (IF NOT EXISTS)."""
    db = get_db()
    with current_app.open_resource("schema.sql") as f:
        db.executescript(f.read().decode("utf-8"))
    db.commit()


@click.command("init-db")
def init_db_command():
    """CLI entry point: ``flask --app app init-db``."""
    init_db()
    click.echo(f"Initialized database at {current_app.config['DATABASE']}")


def register(app):
    """Wire the database into the Flask app (teardown handler + CLI command)."""
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
