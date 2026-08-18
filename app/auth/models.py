"""Data access for users and API keys -- plain SQL, no ORM.

Password hashing uses werkzeug.security, which ships with Flask and therefore
does not add a dependency. API keys are hashed with SHA-256 instead: they are
randomly generated 256-bit secrets (no brute-force risk that would justify a
slow KDF) and we need a *deterministic* hash to look the key up in one query.
"""

import hashlib
import secrets

from werkzeug.security import check_password_hash, generate_password_hash

from .. import db

# Prefix so a leaked key is recognizable as a HomeCloud credential.
API_KEY_PREFIX = "hc_"


# --- users ------------------------------------------------------------------


def create_user(email, password, role="user"):
    """Insert a new user and return its id. Caller must ensure email is free."""
    return db.execute(
        "INSERT INTO users (email, password_hash, role) VALUES (?, ?, ?)",
        (email, generate_password_hash(password), role),
    )


def get_user_by_email(email):
    return db.query("SELECT * FROM users WHERE email = ?", (email,), one=True)


def get_user_by_id(user_id):
    return db.query("SELECT * FROM users WHERE id = ?", (user_id,), one=True)


def count_users():
    """Used to make the very first registered account an admin."""
    row = db.query("SELECT COUNT(*) AS n FROM users", one=True)
    return row["n"]


def verify_password(user_row, password):
    """Constant-time-ish password check via werkzeug."""
    return check_password_hash(user_row["password_hash"], password)


# --- api keys ---------------------------------------------------------------


def _hash_key(plaintext):
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def create_api_key(user_id, label=""):
    """Generate a new API key.

    Returns (row_id, plaintext). The plaintext is the only time the caller can
    see the key -- the database stores just its hash.
    """
    plaintext = API_KEY_PREFIX + secrets.token_urlsafe(32)
    row_id = db.execute(
        "INSERT INTO api_keys (user_id, key_hash, label) VALUES (?, ?, ?)",
        (user_id, _hash_key(plaintext), label),
    )
    return row_id, plaintext


def get_user_by_api_key(plaintext):
    """Resolve an API key to its owner, or None. Also stamps last_used_at."""
    if not plaintext:
        return None
    row = db.query(
        "SELECT * FROM api_keys WHERE key_hash = ?", (_hash_key(plaintext),), one=True
    )
    if row is None:
        return None
    db.execute(
        "UPDATE api_keys SET last_used_at = datetime('now') WHERE id = ?", (row["id"],)
    )
    return get_user_by_id(row["user_id"])


def list_api_keys(user_id):
    """Metadata only -- there is no way to recover the key itself."""
    return db.query(
        "SELECT id, label, created_at, last_used_at FROM api_keys"
        " WHERE user_id = ? ORDER BY id DESC",
        (user_id,),
    )


def delete_api_key(user_id, key_id):
    """Delete one key owned by user_id. Returns True if a row was removed."""
    database = db.get_db()
    cursor = database.execute(
        "DELETE FROM api_keys WHERE id = ? AND user_id = ?", (key_id, user_id)
    )
    database.commit()
    deleted = cursor.rowcount > 0
    cursor.close()
    return deleted
