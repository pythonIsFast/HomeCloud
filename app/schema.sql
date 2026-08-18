-- HomeCloud database schema (plain SQLite DDL, no ORM).
--
-- Design note: there is intentionally NO table per service. Every future
-- service (compute, storage, database, ...) stores its objects as rows in
-- "resources" with its own service_type and a JSON blob in config_json.
-- See CLAUDE.md for the reasoning.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- users: local accounts. Passwords are stored as werkzeug hashes only.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    role          TEXT    NOT NULL DEFAULT 'user',   -- 'admin' | 'user'
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- api_keys: long-lived credentials for machine access (CLI, scripts).
-- Only the SHA-256 hash of the key is stored; the plaintext is shown once.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS api_keys (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    key_hash     TEXT    NOT NULL UNIQUE,
    label        TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    last_used_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys (user_id);

-- ---------------------------------------------------------------------------
-- resources: the generic resource registry shared by ALL services.
--   service_type -> which service owns the row ('compute', 'storage', ...)
--   status       -> service specific lifecycle state ('running', 'stopped', ...)
--   config_json  -> service specific payload, serialized JSON object
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS resources (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    service_type TEXT    NOT NULL,
    name         TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending',
    config_json  TEXT    NOT NULL DEFAULT '{}',
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_resources_user_service
    ON resources (user_id, service_type);

-- A resource name is unique per user and per service type.
CREATE UNIQUE INDEX IF NOT EXISTS idx_resources_unique_name
    ON resources (user_id, service_type, name);

-- ---------------------------------------------------------------------------
-- audit_log: append-only trail of everything that changes state.
-- resource_id is nullable (e.g. login events are not tied to a resource).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER,
    action       TEXT    NOT NULL,
    resource_id  INTEGER,
    details_json TEXT    NOT NULL DEFAULT '{}',
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id)     REFERENCES users (id)     ON DELETE SET NULL,
    FOREIGN KEY (resource_id) REFERENCES resources (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log (created_at);
