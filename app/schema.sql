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

-- The console filters by user and pages by descending id, so the index has to
-- cover both or SQLite falls back to a full scan plus a sort.
CREATE INDEX IF NOT EXISTS idx_audit_log_user_id ON audit_log (user_id, id DESC);

-- Keyset pagination over resources reads (user_id, service_type) in id order.
CREATE INDEX IF NOT EXISTS idx_resources_page
    ON resources (user_id, service_type, id DESC);

-- ===========================================================================
-- Platform infrastructure below this line.
--
-- These are NOT service object tables -- the "one shared resources table" rule
-- applies to the things a service manages (VMs, buckets, databases), not to
-- the plumbing the platform itself needs. See CLAUDE.md section 2.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- jobs: work that must not happen inside a web request.
--
-- Creating a microVM takes seconds and needs root (tap device, NAT), so the
-- Flask process only enqueues here and the privileged worker executes.
-- "host" lets a second compute host later claim only its own jobs; NULL means
-- "any worker may take it".
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id  INTEGER,
    user_id      INTEGER,
    action       TEXT    NOT NULL,              -- compute actions | update
    payload_json TEXT    NOT NULL DEFAULT '{}',
    status       TEXT    NOT NULL DEFAULT 'queued',  -- queued|running|done|failed
    host         TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    claimed_at   TEXT,
    finished_at  TEXT,
    FOREIGN KEY (resource_id) REFERENCES resources (id) ON DELETE CASCADE,
    FOREIGN KEY (user_id)     REFERENCES users (id)     ON DELETE SET NULL
);

-- The worker polls exactly this: oldest queued job for its host.
CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs (status, host, id);
CREATE INDEX IF NOT EXISTS idx_jobs_resource ON jobs (resource_id, id DESC);

-- ---------------------------------------------------------------------------
-- limits: quota per user, plus the installation-wide default.
--
-- The row with user_id IS NULL is the default that applies to everyone; a row
-- with a user_id overrides it for that user. Admins edit both in the console.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS limits (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER UNIQUE,       -- NULL = installation default
    max_vms        INTEGER NOT NULL DEFAULT 2,
    max_vcpu       INTEGER NOT NULL DEFAULT 4,
    max_memory_mb  INTEGER NOT NULL DEFAULT 2048,
    max_disk_gb    INTEGER NOT NULL DEFAULT 20,
    updated_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
);

-- Seed the installation default once. INSERT OR IGNORE keeps init-db idempotent
-- and never overwrites limits an admin has already changed.
INSERT OR IGNORE INTO limits (id, user_id) VALUES (1, NULL);

-- ---------------------------------------------------------------------------
-- platform_settings and compute_flavors: installation policy, not service
-- objects. They deliberately live outside resources because they configure
-- HomeCloud itself rather than representing something a user owns.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS platform_settings (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS compute_flavors (
    name       TEXT PRIMARY KEY,
    vcpu       INTEGER NOT NULL,
    memory_mb  INTEGER NOT NULL,
    disk_gb    INTEGER NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    is_default INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- These rows seed a fresh installation only. Administrators may adjust the
-- catalogue later without an update overwriting their policy.
INSERT OR IGNORE INTO compute_flavors (name, vcpu, memory_mb, disk_gb, enabled, is_default) VALUES
    ('hc.nano',   1,  256,  1, 1, 0),
    ('hc.micro',  1,  512,  2, 1, 1),
    ('hc.small',  1, 1024,  5, 1, 0),
    ('hc.medium', 2, 2048, 10, 1, 0),
    ('hc.large',  4, 4096, 20, 1, 0);
