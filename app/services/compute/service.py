"""Compute domain logic: validation, state machine, quota, job enqueueing.

Deliberately free of Flask and of anything host-related. Routes call in here,
this module writes the registry row plus a job, and the privileged worker does
the actual work. Nothing in this file touches KVM, tap devices or images.
"""

import re

from ... import audit, jobs, limits
from ...core import resources
from . import flavors

SERVICE_TYPE = "compute"

# Instance names end up in a tap device name and a directory, so keep them tame.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$")

# Which action is allowed in which state, and what the row moves to while the
# worker is busy. Keeping this in one table is what stops the "start a deleting
# VM" class of bug -- routes never compare statuses by hand.
TRANSITIONS = {
    "start":   {"from": ("stopped", "error"), "busy": "pending"},
    "stop":    {"from": ("running",),          "busy": "stopping"},
    "restart": {"from": ("running", "stopped"), "busy": "pending"},
    "delete":  {"from": ("pending", "creating", "running", "stopping",
                         "stopped", "error"), "busy": "deleting"},
}


class ComputeError(Exception):
    """Domain rejection with an HTTP status attached."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# --- helpers ----------------------------------------------------------------


def public_view(row, include_secrets=False):
    """Registry row -> API shape.

    Internal paths and the pid are noise for clients, so they only appear for
    admins/debugging.
    """
    data = resources.to_dict(row)
    config = data.pop("config", {}) or {}

    view = {
        "id": data["id"],
        "name": data["name"],
        "status": data["status"],
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
        "flavor": config.get("flavor"),
        "vcpu": config.get("vcpu"),
        "memory_mb": config.get("memory_mb"),
        "disk_gb": config.get("disk_gb"),
        "ip": config.get("ip"),
        "usage": config.get("usage"),
        "last_error": config.get("last_error"),
    }
    if include_secrets:
        view["internal"] = {
            "pid": config.get("pid"),
            "tap": config.get("tap"),
            "mac": config.get("mac"),
            "rootfs": config.get("rootfs"),
        }
    return view


def validate_name(name):
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise ComputeError(
            "name must be 3-32 characters, lowercase letters, digits or dashes, "
            "and may not start or end with a dash"
        )
    return name


# --- create -----------------------------------------------------------------


def create_instance(user, name, flavor_name):
    """Validate, reserve quota, write the registry row, queue the create job."""
    name = validate_name(name)
    flavor = flavors.get(flavor_name or flavors.DEFAULT_FLAVOR)
    if flavor is None:
        raise ComputeError(f"unknown flavor: {flavor_name!r}")

    if resources.get_by_name(user["id"], SERVICE_TYPE, name) is not None:
        raise ComputeError(f"you already have an instance named {name!r}", status=409)

    denial = limits.check_new_vm(
        user["id"], flavor["vcpu"], flavor["memory_mb"], flavor["disk_gb"]
    )
    if denial:
        raise ComputeError(denial, status=409)

    resource_id = resources.create(
        user["id"],
        SERVICE_TYPE,
        name,
        config={
            "flavor": flavor["name"],
            "vcpu": flavor["vcpu"],
            "memory_mb": flavor["memory_mb"],
            "disk_gb": flavor["disk_gb"],
            "pid": None,
            "ip": None,
        },
        status="pending",
    )

    jobs.enqueue("create", resource_id=resource_id, user_id=user["id"])
    audit.log_action(user["id"], "compute.requested", resource_id,
                     {"name": name, "flavor": flavor["name"]})

    return resources.get(user["id"], resource_id)


# --- actions ----------------------------------------------------------------


def perform_action(user, resource_id, action):
    """Queue start/stop/restart/delete for one instance.

    The state change is an atomic UPDATE with the allowed source states in the
    WHERE clause, so two parallel requests cannot both queue a job: the loser
    gets a 409.
    """
    rule = TRANSITIONS.get(action)
    if rule is None:
        raise ComputeError(f"unknown action: {action!r}")

    row = resources.get(user["id"], resource_id)
    if row is None:
        raise ComputeError("instance not found", status=404)
    if row["status"] == "deleted":
        raise ComputeError("instance is deleted", status=409)

    if not resources.transition(resource_id, rule["from"], rule["busy"]):
        current = resources.get(user["id"], resource_id)
        raise ComputeError(
            f"cannot {action} an instance in state {current['status']!r}",
            status=409,
        )

    jobs.enqueue(action, resource_id=resource_id, user_id=user["id"])
    audit.log_action(user["id"], f"compute.{action}_requested", resource_id)

    return resources.get(user["id"], resource_id)


# --- read -------------------------------------------------------------------


def list_page(user, before_id=None, limit=None):
    rows, has_more = resources.list_page(
        user["id"],
        SERVICE_TYPE,
        before_id=before_id,
        limit=limit or resources.DEFAULT_PAGE_SIZE,
    )
    instances = [public_view(row) for row in rows]
    return {
        "instances": instances,
        # Cursor for the next page: the smallest id on this one.
        "next_before_id": instances[-1]["id"] if (instances and has_more) else None,
        "has_more": has_more,
    }


def get_instance(user, resource_id, include_secrets=False):
    row = resources.get(user["id"], resource_id)
    if row is None:
        raise ComputeError("instance not found", status=404)
    return public_view(row, include_secrets=include_secrets)


def quota_summary(user):
    return {
        "limits": limits.effective(user["id"]),
        "usage": limits.usage(user["id"], SERVICE_TYPE),
    }
