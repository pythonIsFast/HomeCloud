"""Administrator-managed instance size catalogue."""

import re

from ... import db

NAME_RE = re.compile(r"^[a-z][a-z0-9.-]{1,30}$")


def get(name):
    """Return a flavor dict, or None if the name is unknown."""
    flavor = db.query(
        "SELECT name, vcpu, memory_mb, disk_gb FROM compute_flavors"
        " WHERE name = ? AND enabled = 1",
        (name,), one=True,
    )
    if flavor is None:
        return None
    return dict(flavor)


def catalogue(include_disabled=False):
    """All flavours, smallest first; disabled entries stay admin-visible."""
    where = "" if include_disabled else " WHERE enabled = 1"
    rows = db.query(
        "SELECT name, vcpu, memory_mb, disk_gb, enabled, is_default"
        " FROM compute_flavors" + where + " ORDER BY memory_mb, vcpu, disk_gb, name"
    )
    return [dict(row) for row in rows]


def default_name():
    row = db.query(
        "SELECT name FROM compute_flavors WHERE enabled = 1 AND is_default = 1"
        " ORDER BY name LIMIT 1", one=True,
    )
    if row is None:
        row = db.query(
            "SELECT name FROM compute_flavors WHERE enabled = 1"
            " ORDER BY memory_mb, vcpu, disk_gb, name LIMIT 1", one=True,
        )
    return row["name"] if row else None


def save(name, values, creating=False):
    """Create or update one flavour. Changes affect future resize/create work."""
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise ValueError("flavor name must be 2-31 lowercase letters, digits, dots or dashes")
    try:
        vcpu = int(values.get("vcpu"))
        memory_mb = int(values.get("memory_mb"))
        disk_gb = int(values.get("disk_gb"))
    except (TypeError, ValueError) as error:
        raise ValueError("vCPU, memory and disk must be whole numbers") from error
    if not 1 <= vcpu <= 64 or not 128 <= memory_mb <= 262144 or not 1 <= disk_gb <= 2048:
        raise ValueError("flavor values are outside the supported range")
    existing = db.query("SELECT name, is_default FROM compute_flavors WHERE name = ?", (name,), one=True)
    if creating and existing is not None:
        raise ValueError("a flavor with that name already exists")
    if existing is None and not creating:
        raise ValueError("flavor not found")
    enabled = bool(values.get("enabled", True))
    make_default = bool(values.get("is_default", existing["is_default"] if existing else False))
    if existing and existing["is_default"] and not enabled and not make_default:
        raise ValueError("choose another default flavor before disabling this one")
    if make_default and not enabled:
        raise ValueError("the default flavor must be enabled")
    if make_default:
        db.modify("UPDATE compute_flavors SET is_default = 0 WHERE is_default = 1")
    db.execute(
        "INSERT INTO compute_flavors (name, vcpu, memory_mb, disk_gb, enabled, is_default)"
        " VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(name) DO UPDATE SET vcpu = excluded.vcpu,"
        " memory_mb = excluded.memory_mb, disk_gb = excluded.disk_gb,"
        " enabled = excluded.enabled, is_default = excluded.is_default,"
        " updated_at = datetime('now')",
        (name, vcpu, memory_mb, disk_gb, int(enabled), int(make_default)),
    )
    if default_name() is None:
        db.modify("UPDATE compute_flavors SET enabled = 1, is_default = 1 WHERE name = ?", (name,))
    return db.query("SELECT name, vcpu, memory_mb, disk_gb, enabled, is_default"
                    " FROM compute_flavors WHERE name = ?", (name,), one=True)
