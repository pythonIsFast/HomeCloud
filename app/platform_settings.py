"""Durable, administrator-managed platform policy.

Environment variables still own host wiring such as KVM paths, network ranges
and secrets. This module owns the safe application policies that should take
effect immediately across gunicorn workers without rewriting a unit file.
"""

import json

from flask import current_app

from . import db

MIB = 1024 * 1024


def _defaults():
    return {
        "allow_registration": bool(current_app.config["ALLOW_REGISTRATION"]),
        "jwt_ttl_hours": max(1, int(current_app.config["JWT_TTL_SECONDS"]) // 3600),
        "image_upload_max_mb": max(16, int(current_app.config["MAX_CONTENT_LENGTH"]) // MIB),
        "max_images_per_user": 10,
        "auth_rate_limit": 10,
        "auth_rate_window_seconds": 300,
        "write_rate_limit": 120,
        "api_rate_limit": 600,
    }


def values():
    """Return all effective policy values, filling absent rows with defaults."""
    result = _defaults()
    rows = db.query("SELECT key, value_json FROM platform_settings")
    for row in rows:
        if row["key"] not in result:
            continue
        try:
            result[row["key"]] = json.loads(row["value_json"])
        except (TypeError, json.JSONDecodeError):
            continue
    return result


def value(key):
    return values()[key]


def upload_limit_bytes():
    """The UI can reduce, never exceed, the host-configured upload ceiling."""
    configured = int(value("image_upload_max_mb")) * MIB
    return min(configured, int(current_app.config["MAX_CONTENT_LENGTH"]))


def update(incoming):
    """Validate and persist the supported administrator policy values."""
    if not isinstance(incoming, dict):
        raise ValueError("settings must be an object")
    current = values()
    unknown = set(incoming) - set(current)
    if unknown:
        raise ValueError("unknown setting: " + sorted(unknown)[0])

    result = dict(current)
    for key, raw in incoming.items():
        if key == "allow_registration":
            if not isinstance(raw, bool):
                raise ValueError("allow_registration must be true or false")
            result[key] = raw
            continue
        try:
            number = int(raw)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{key} must be a whole number") from error
        result[key] = number

    ranges = {
        "jwt_ttl_hours": (1, 24 * 30),
        "image_upload_max_mb": (16, max(16, int(current_app.config["MAX_CONTENT_LENGTH"]) // MIB)),
        "max_images_per_user": (1, 100),
        "auth_rate_limit": (1, 1000),
        "auth_rate_window_seconds": (1, 3600),
        "write_rate_limit": (10, 10000),
        "api_rate_limit": (10, 100000),
    }
    for key, (minimum, maximum) in ranges.items():
        if not minimum <= result[key] <= maximum:
            raise ValueError(f"{key} must be between {minimum} and {maximum}")

    for key in incoming:
        db.execute(
            "INSERT INTO platform_settings (key, value_json) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,"
            " updated_at = datetime('now')",
            (key, json.dumps(result[key])),
        )
    return result
