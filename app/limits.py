"""Per-user quota.

One row in ``limits`` with ``user_id IS NULL`` is the installation default;
a row with a user_id overrides it for that user. Admins edit both in the
console (#admin view).

The point of quota here is not billing, it is that a microVM reserves real
memory and real disk on the host. Ten users with unlimited "create" would take
the box down, so every create is checked against both the count and the sums.
"""

from . import db

FIELDS = ("max_vms", "max_vcpu", "max_memory_mb", "max_disk_gb")


def defaults():
    """The installation-wide default row (seeded by schema.sql)."""
    row = db.query("SELECT * FROM limits WHERE user_id IS NULL", one=True)
    if row is not None:
        return dict(row)
    # Only reachable if someone deleted the seeded row.
    return {"id": None, "user_id": None, "max_vms": 2, "max_vcpu": 4,
            "max_memory_mb": 2048, "max_disk_gb": 20}


def override_for(user_id):
    return db.query("SELECT * FROM limits WHERE user_id = ?", (user_id,), one=True)


def effective(user_id):
    """Limits that actually apply to a user, plus where each value came from."""
    base = defaults()
    override = override_for(user_id)

    values = {field: base[field] for field in FIELDS}
    source = "default"
    if override is not None:
        source = "override"
        for field in FIELDS:
            values[field] = override[field]

    values["source"] = source
    return values


def set_override(user_id, values):
    """Create or replace a user's override. Values not given fall back to the default."""
    base = defaults()
    row = {field: int(values.get(field, base[field])) for field in FIELDS}
    for field in FIELDS:
        if row[field] < 0:
            raise ValueError(f"{field} must not be negative")

    db.execute(
        "INSERT INTO limits (user_id, max_vms, max_vcpu, max_memory_mb, max_disk_gb)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(user_id) DO UPDATE SET"
        "   max_vms = excluded.max_vms,"
        "   max_vcpu = excluded.max_vcpu,"
        "   max_memory_mb = excluded.max_memory_mb,"
        "   max_disk_gb = excluded.max_disk_gb,"
        "   updated_at = datetime('now')",
        (user_id, row["max_vms"], row["max_vcpu"], row["max_memory_mb"],
         row["max_disk_gb"]),
    )


def clear_override(user_id):
    return db.modify("DELETE FROM limits WHERE user_id = ?", (user_id,)) == 1


def set_defaults(values):
    base = defaults()
    row = {field: int(values.get(field, base[field])) for field in FIELDS}
    db.execute(
        "UPDATE limits SET max_vms = ?, max_vcpu = ?, max_memory_mb = ?,"
        " max_disk_gb = ?, updated_at = datetime('now') WHERE user_id IS NULL",
        (row["max_vms"], row["max_vcpu"], row["max_memory_mb"], row["max_disk_gb"]),
    )


def usage(user_id, service_type="compute"):
    """What the user has already reserved.

    Deleted resources do not count. The sums come from json_extract on
    config_json -- SQLite has had JSON1 built in since 3.38, so this needs no
    extension. If a service ever stores its sizing under different keys, add a
    COALESCE here rather than a second table.
    """
    row = db.query(
        "SELECT COUNT(*) AS vms,"
        "       COALESCE(SUM(json_extract(config_json, '$.vcpu')), 0) AS vcpu,"
        "       COALESCE(SUM(json_extract(config_json, '$.memory_mb')), 0) AS memory_mb,"
        "       COALESCE(SUM(json_extract(config_json, '$.disk_gb')), 0) AS disk_gb"
        " FROM resources"
        " WHERE user_id = ? AND service_type = ? AND status != 'deleted'",
        (user_id, service_type),
        one=True,
    )
    return {
        "vms": row["vms"],
        "vcpu": int(row["vcpu"] or 0),
        "memory_mb": int(row["memory_mb"] or 0),
        "disk_gb": int(row["disk_gb"] or 0),
    }


def check_new_vm(user_id, vcpu, memory_mb, disk_gb, service_type="compute"):
    """Return None if the VM fits the user's quota, otherwise a message.

    Message is returned rather than raised so the route can turn it into a 409
    with a readable reason -- "would exceed your memory quota (2048 MB)".
    """
    allowed = effective(user_id)
    used = usage(user_id, service_type)

    if used["vms"] + 1 > allowed["max_vms"]:
        return (
            f"instance limit reached: {used['vms']}/{allowed['max_vms']} in use"
        )
    if used["vcpu"] + vcpu > allowed["max_vcpu"]:
        return (
            f"vCPU quota exceeded: {used['vcpu']} + {vcpu} > {allowed['max_vcpu']}"
        )
    if used["memory_mb"] + memory_mb > allowed["max_memory_mb"]:
        return (
            f"memory quota exceeded: {used['memory_mb']} + {memory_mb} MB"
            f" > {allowed['max_memory_mb']} MB"
        )
    if used["disk_gb"] + disk_gb > allowed["max_disk_gb"]:
        return (
            f"disk quota exceeded: {used['disk_gb']} + {disk_gb} GB"
            f" > {allowed['max_disk_gb']} GB"
        )
    return None
