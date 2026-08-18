"""Compute domain logic: validation, state machine, quota, job enqueueing.

Deliberately free of Flask and of anything host-related. Routes call in here,
this module writes the registry row plus a job, and the privileged worker does
the actual work. Nothing in this file touches KVM, tap devices or images.
"""

import re
import ipaddress

from ... import audit, jobs, limits
from ...core import resources
from . import flavors

SERVICE_TYPE = "compute"
IMAGE_TYPE = "compute_image"
MAX_IMAGES_PER_USER = 10

# Instance names end up in a tap device name and a directory, so keep them tame.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$")
SUBDOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

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
        "firewall": config.get("firewall", []),
        "serveo": {
            key: value for key, value in (config.get("serveo") or {}).items()
            if key in ("enabled", "status", "port", "subdomain", "url", "error")
        },
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


def validate_firewall(rules):
    """Validate untrusted firewall input without importing privileged VMM code."""
    if not isinstance(rules, list) or len(rules) > 32:
        raise ComputeError("firewall must contain at most 32 rules")
    normalized = []
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("protocol") not in ("tcp", "udp"):
            raise ComputeError("firewall protocol must be tcp or udp")
        try:
            port = int(rule.get("port"))
            source = str(ipaddress.IPv4Network(rule.get("source", "0.0.0.0/0"), strict=False))
        except (TypeError, ValueError) as error:
            raise ComputeError("firewall source must be an IPv4 CIDR and port must be numeric") from error
        if not 1 <= port <= 65535:
            raise ComputeError("firewall port must be 1-65535")
        normalized.append({"protocol": rule["protocol"], "port": port, "source": source})
    return normalized


# --- create -----------------------------------------------------------------


def create_instance(user, name, flavor_name, image_id=None):
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

    try:
        image_id = int(image_id) if image_id not in (None, "") else None
    except (TypeError, ValueError) as error:
        raise ComputeError("image id must be numeric") from error
    if image_id:
        image = resources.get(user["id"], image_id)
        if image is None or image["service_type"] != IMAGE_TYPE or image["status"] != "ready":
            raise ComputeError("selected image is not available", status=409)
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
            "firewall": [],
            "image_id": image_id,
        },
        status="pending",
    )

    jobs.enqueue("create", resource_id=resource_id, user_id=user["id"])
    audit.log_action(user["id"], "compute.requested", resource_id,
                     {"name": name, "flavor": flavor["name"]})

    return resources.get(user["id"], resource_id)


def image_view(row):
    data = resources.to_dict(row)
    config = data["config"]
    return {"id": data["id"], "name": data["name"], "status": data["status"],
            "created_at": data["created_at"], "size_bytes": config.get("size_bytes", 0),
            "sha256": config.get("sha256"), "source_instance_id": config.get("source_instance_id"),
            "source": config.get("source", "snapshot"), "verified": config.get("verified", False),
            "last_error": config.get("last_error")}


def list_images(user):
    return [image_view(row) for row in resources.list_for_user(user["id"], IMAGE_TYPE)]


def snapshot(user, source_id, name):
    source = resources.get(user["id"], source_id)
    if source is None or source["service_type"] != SERVICE_TYPE:
        raise ComputeError("instance not found", status=404)
    if source["status"] != "stopped":
        raise ComputeError("stop the instance before creating a consistent snapshot", status=409)
    name = validate_name(name)
    if resources.get_by_name(user["id"], IMAGE_TYPE, name):
        raise ComputeError(f"you already have an image named {name!r}", status=409)
    image_id = resources.create(user["id"], IMAGE_TYPE, name,
        {"source": "snapshot", "source_instance_id": source_id, "verified": False},
        status="pending")
    jobs.enqueue("snapshot", resource_id=image_id, user_id=user["id"], payload={"source_id": source_id})
    audit.log_action(user["id"], "compute.snapshot_requested", image_id, {"source": source_id})
    return resources.get(user["id"], image_id)


def validate_image_import(user, name):
    name = validate_name(name)
    if resources.get_by_name(user["id"], IMAGE_TYPE, name):
        raise ComputeError(f"you already have an image named {name!r}", status=409)
    active = [row for row in resources.list_for_user(user["id"], IMAGE_TYPE)
              if row["status"] not in ("deleted", "error")]
    if len(active) >= MAX_IMAGES_PER_USER:
        raise ComputeError(f"image limit reached ({MAX_IMAGES_PER_USER})", status=409)
    return name


def import_image(user, name, staged_path, filename, size_bytes, checksum):
    """Register an already bounded upload for asynchronous host validation."""
    name = validate_image_import(user, name)
    image_id = resources.create(user["id"], IMAGE_TYPE, name, {
        "source": "upload", "original_filename": filename,
        "staged_path": staged_path, "size_bytes": int(size_bytes),
        "sha256": checksum, "verified": False,
    }, status="pending")
    jobs.enqueue("import_image", resource_id=image_id, user_id=user["id"])
    audit.log_action(user["id"], "compute.image_upload_requested", image_id,
                     {"filename": filename, "size_bytes": int(size_bytes)})
    return resources.get(user["id"], image_id)


def update_firewall(user, resource_id, rules):
    row = resources.get(user["id"], resource_id)
    if row is None or row["service_type"] != SERVICE_TYPE:
        raise ComputeError("instance not found", status=404)
    if row["status"] == "deleted":
        raise ComputeError("instance is deleted", status=409)
    rules = validate_firewall(rules)
    config = resources.to_dict(row)["config"]
    config["firewall"] = rules
    resources.set_config(resource_id, config)
    jobs.enqueue("firewall", resource_id=resource_id, user_id=user["id"])
    audit.log_action(user["id"], "compute.firewall_requested", resource_id, {"rules": rules})
    return resources.get(user["id"], resource_id)


def change_flavor(user, resource_id, flavor_name):
    """Change an existing instance size, growing its disk when necessary."""
    row = resources.get(user["id"], resource_id)
    if row is None or row["service_type"] != SERVICE_TYPE:
        raise ComputeError("instance not found", status=404)
    if row["status"] not in ("running", "stopped"):
        raise ComputeError("instance must be running or stopped to change its type", status=409)

    flavor = flavors.get(flavor_name)
    if flavor is None:
        raise ComputeError(f"unknown flavor: {flavor_name!r}")

    current = resources.to_dict(row)["config"]
    old_disk = int(current.get("disk_gb", 0))
    if flavor["disk_gb"] < old_disk:
        raise ComputeError(
            f"disk cannot be reduced from {old_disk} GB to {flavor['disk_gb']} GB"
        )

    used = limits.usage(user["id"], SERVICE_TYPE)
    allowed = limits.effective(user["id"])
    projected = {
        "vcpu": used["vcpu"] - int(current.get("vcpu", 0)) + flavor["vcpu"],
        "memory_mb": used["memory_mb"] - int(current.get("memory_mb", 0)) + flavor["memory_mb"],
        "disk_gb": used["disk_gb"] - old_disk + flavor["disk_gb"],
    }
    for field, label, suffix in (("vcpu", "vCPU", ""), ("memory_mb", "memory", " MB"),
                                 ("disk_gb", "disk", " GB")):
        cap = allowed["max_" + field]
        if projected[field] > cap:
            raise ComputeError(f"{label} quota exceeded: {projected[field]}{suffix} > {cap}{suffix}", status=409)

    previous_status = row["status"]
    if not resources.transition(resource_id, previous_status, "resizing"):
        raise ComputeError("instance changed state; try again", status=409)

    config = dict(current)
    config.update({"flavor": flavor["name"], "vcpu": flavor["vcpu"],
                  "memory_mb": flavor["memory_mb"], "disk_gb": flavor["disk_gb"]})
    resources.set_config(resource_id, config)
    jobs.enqueue("resize", resource_id=resource_id, user_id=user["id"],
                 payload={"was_running": previous_status == "running"})
    audit.log_action(user["id"], "compute.resize_requested", resource_id,
                     {"flavor": flavor["name"]})
    return resources.get(user["id"], resource_id)


def start_serveo(user, resource_id, port, subdomain=""):
    """Expose one running VM port through a host-side Serveo tunnel."""
    row = resources.get(user["id"], resource_id)
    if row is None or row["service_type"] != SERVICE_TYPE:
        raise ComputeError("instance not found", status=404)
    if row["status"] != "running":
        raise ComputeError("start the instance before creating public access", status=409)
    try:
        port = int(port)
    except (TypeError, ValueError) as error:
        raise ComputeError("port must be numeric") from error
    if not 1 <= port <= 65535:
        raise ComputeError("port must be 1-65535")
    subdomain = (subdomain or "").strip().lower()
    if subdomain and not SUBDOMAIN_RE.match(subdomain):
        raise ComputeError("subdomain must contain lowercase letters, digits or dashes")

    config = resources.to_dict(row)["config"]
    if not config.get("ip"):
        raise ComputeError("instance has no private address", status=409)
    current = config.get("serveo") or {}
    if current.get("status") in ("starting", "stopping"):
        raise ComputeError("a public-access operation is already in progress", status=409)
    config["serveo"] = {
        "enabled": True, "status": "starting", "port": port,
        "subdomain": subdomain, "url": None, "pid": current.get("pid"), "error": None,
    }
    resources.set_config(resource_id, config)
    jobs.enqueue("serveo_start", resource_id=resource_id, user_id=user["id"])
    audit.log_action(user["id"], "compute.serveo_requested", resource_id,
                     {"port": port, "subdomain": subdomain})
    return resources.get(user["id"], resource_id)


def stop_serveo(user, resource_id):
    row = resources.get(user["id"], resource_id)
    if row is None or row["service_type"] != SERVICE_TYPE:
        raise ComputeError("instance not found", status=404)
    config = resources.to_dict(row)["config"]
    tunnel = config.get("serveo") or {}
    if not tunnel.get("enabled") and not tunnel.get("pid"):
        raise ComputeError("public access is not active", status=409)
    tunnel.update({"enabled": False, "status": "stopping"})
    config["serveo"] = tunnel
    resources.set_config(resource_id, config)
    jobs.enqueue("serveo_stop", resource_id=resource_id, user_id=user["id"])
    audit.log_action(user["id"], "compute.serveo_stop_requested", resource_id)
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
