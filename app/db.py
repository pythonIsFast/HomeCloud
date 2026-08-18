"""Database access layer.

Everything goes through the standard library sqlite3 module with plain SQL.
No ORM, no Flask-SQLAlchemy (see CLAUDE.md, dependency policy).

The connection is cached on Flask's request-scoped ``g`` object, so every
request opens at most one connection and closes it when the request ends.
"""

import sqlite3

import click
from flask import current_app, g


def get_db():
    """Return the sqlite3 connection for the current request (opening it lazily)."""
    if "db" not in g:
        g.db = sqlite3.connect(
            current_app.config["DATABASE"],
            # Let sqlite3 parse DATE/TIMESTAMP declared columns; we mostly use TEXT.
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        # Rows behave like dicts: row["email"] instead of row[1].
        g.db.row_factory = sqlite3.Row
        # SQLite does not enforce foreign keys unless asked to, per connection.
        g.db.execute("PRAGMA foreign_keys = ON")
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
