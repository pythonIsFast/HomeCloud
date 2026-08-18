"""Core HTTP endpoints: dashboard page, resource registry API, admin, health."""

from flask import current_app, jsonify, render_template, request

from .. import audit, jobs, limits, platform_settings, update
from ..auth import guards, models
from . import bp, resources


@bp.get("/")
@guards.login_required
def dashboard():
    """The console shell. All data is fetched by static/js/dashboard.js."""
    return render_template(
        "dashboard.html",
        app_name=current_app.config["APP_NAME"],
        user=guards.current_user(),
    )


@bp.get("/healthz")
def healthz():
    """Unauthenticated liveness probe for nginx / monitoring."""
    return jsonify({"status": "ok", "app": current_app.config["APP_NAME"]})


# --- resource registry ------------------------------------------------------


@bp.get("/api/resources")
@guards.login_required
def list_resources():
    """One keyset page of the caller's resources.

    Query parameters: ?service_type=compute&before_id=<id>&limit=<n>
    Never returns an unbounded list -- see resources.list_page().
    """
    user = guards.current_user()
    rows, has_more = resources.list_page(
        user["id"],
        service_type=request.args.get("service_type") or None,
        before_id=request.args.get("before_id", type=int),
        limit=request.args.get("limit", type=int) or resources.DEFAULT_PAGE_SIZE,
    )
    items = [resources.to_dict(row) for row in rows]
    return jsonify({
        "resources": items,
        "next_before_id": items[-1]["id"] if (items and has_more) else None,
        "has_more": has_more,
    })


@bp.get("/api/resources/<int:resource_id>")
@guards.login_required
def get_resource(resource_id):
    user = guards.current_user()
    row = resources.get(user["id"], resource_id)
    if row is None:
        return jsonify({"error": "resource not found"}), 404
    return jsonify({"resource": resources.to_dict(row)})


@bp.get("/api/audit")
@guards.login_required
def list_audit():
    """Recent audit entries for the caller (admins see every user)."""
    user = guards.current_user()
    entries, has_more = audit.recent(
        limit=request.args.get("limit", type=int) or 50,
        user_id=None if user["role"] == "admin" else user["id"],
        before_id=request.args.get("before_id", type=int),
    )
    items = [dict(row) for row in entries]
    return jsonify({
        "entries": items,
        "next_before_id": items[-1]["id"] if (items and has_more) else None,
        "has_more": has_more,
    })


# --- admin: quota -----------------------------------------------------------
#
# Limits are platform policy, not a service concern, so they live here rather
# than in the compute blueprint -- a future storage service will reuse the same
# table and the same view.


@bp.get("/api/admin/limits")
@guards.admin_required
def admin_get_limits():
    """Installation defaults plus one page of accounts with usage and overrides."""
    rows, has_more = models.list_users_page(
        before_id=request.args.get("before_id", type=int),
        limit=request.args.get("limit", type=int) or 50,
        search=request.args.get("q"),
    )

    users = []
    for row in rows:
        effective = limits.effective(row["id"])
        users.append({
            "id": row["id"],
            "email": row["email"],
            "role": row["role"],
            "created_at": row["created_at"],
            "limits": effective,
            "usage": limits.usage(row["id"]),
        })

    return jsonify({
        "defaults": limits.defaults(),
        "users": users,
        "next_before_id": users[-1]["id"] if (users and has_more) else None,
        "has_more": has_more,
    })


@bp.put("/api/admin/limits")
@guards.admin_required
def admin_set_defaults():
    """Change the installation-wide default limits."""
    data = request.get_json(silent=True) or {}
    try:
        limits.set_defaults(data)
    except (TypeError, ValueError) as error:
        return jsonify({"error": f"invalid value: {error}"}), 400

    audit.log_action(guards.current_user()["id"], "limits.set_defaults", None, data)
    return jsonify({"defaults": limits.defaults()})


@bp.put("/api/admin/limits/<int:user_id>")
@guards.admin_required
def admin_set_override(user_id):
    """Give one user their own limits."""
    if models.get_user_by_id(user_id) is None:
        return jsonify({"error": "user not found"}), 404

    data = request.get_json(silent=True) or {}
    try:
        limits.set_override(user_id, data)
    except (TypeError, ValueError) as error:
        return jsonify({"error": f"invalid value: {error}"}), 400

    audit.log_action(guards.current_user()["id"], "limits.set_override", None,
                     {"target_user": user_id, **data})
    return jsonify({"limits": limits.effective(user_id)})


@bp.delete("/api/admin/limits/<int:user_id>")
@guards.admin_required
def admin_clear_override(user_id):
    """Drop a user's override so the installation default applies again."""
    if not limits.clear_override(user_id):
        return jsonify({"error": "no override for this user"}), 404

    audit.log_action(guards.current_user()["id"], "limits.clear_override", None,
                     {"target_user": user_id})
    return jsonify({"limits": limits.effective(user_id)})


# --- admin: platform policy, flavours and all compute ----------------------


@bp.get("/api/admin/settings")
@guards.admin_required
def admin_get_settings():
    """Return editable application policy and safe read-only host facts."""
    return jsonify({
        "settings": platform_settings.values(),
        "host": {
            "vm_subnet_prefix": current_app.config["VM_SUBNET_PREFIX"],
            "vm_egress_if": current_app.config["VM_EGRESS_IF"] or "auto-detect",
            "vm_kernel": current_app.config["VM_KERNEL"],
            "base_image": current_app.config["VM_BASE_ROOTFS"],
            "upload_ceiling_mb": current_app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024),
        },
    })


@bp.put("/api/admin/settings")
@guards.admin_required
def admin_set_settings():
    data = request.get_json(silent=True) or {}
    try:
        values = platform_settings.update(data)
    except (TypeError, ValueError) as error:
        return jsonify({"error": f"invalid setting: {error}"}), 400
    audit.log_action(guards.current_user()["id"], "platform.settings_updated", None,
                     {"keys": sorted(data)})
    return jsonify({"settings": values})


@bp.get("/api/admin/flavors")
@guards.admin_required
def admin_list_flavors():
    from ..services.compute import flavors

    return jsonify({"flavors": flavors.catalogue(include_disabled=True),
                    "default": flavors.default_name()})


@bp.post("/api/admin/flavors")
@guards.admin_required
def admin_create_flavor():
    from ..services.compute import flavors

    data = request.get_json(silent=True) or {}
    try:
        row = flavors.save(data.get("name"), data, creating=True)
    except (TypeError, ValueError) as error:
        return jsonify({"error": f"invalid flavor: {error}"}), 400
    audit.log_action(guards.current_user()["id"], "compute.flavor_created", None,
                     {"name": row["name"]})
    return jsonify({"flavor": dict(row)}), 201


@bp.put("/api/admin/flavors/<name>")
@guards.admin_required
def admin_update_flavor(name):
    from ..services.compute import flavors

    data = request.get_json(silent=True) or {}
    try:
        row = flavors.save(name, data)
    except (TypeError, ValueError) as error:
        return jsonify({"error": f"invalid flavor: {error}"}), 400
    audit.log_action(guards.current_user()["id"], "compute.flavor_updated", None,
                     {"name": row["name"]})
    return jsonify({"flavor": dict(row)})


@bp.get("/api/admin/instances")
@guards.admin_required
def admin_list_instances():
    """List every compute instance so an admin can open or reconfigure it."""
    from ..services.compute import service as compute_service

    rows, has_more = resources.list_service_page(
        "compute", before_id=request.args.get("before_id", type=int),
        limit=request.args.get("limit", type=int) or 50,
    )
    instances = []
    for row in rows:
        item = compute_service.public_view(row, include_secrets=True)
        owner = models.get_user_by_id(row["user_id"])
        item["owner"] = {"id": row["user_id"], "email": owner["email"] if owner else "deleted user"}
        instances.append(item)
    return jsonify({
        "instances": instances,
        "next_before_id": instances[-1]["id"] if instances and has_more else None,
        "has_more": has_more,
    })


# --- admin: platform updates ------------------------------------------------


@bp.get("/api/admin/update")
@guards.admin_required
def admin_update_status():
    """Check the configured origin and report the latest update job."""
    job = jobs.latest_update()
    return jsonify({
        "check": update.check(),
        "job": dict(job) if job else None,
        "runtime": update.runtime_status(),
    })


@bp.post("/api/admin/update")
@guards.admin_required
def admin_request_update():
    """Queue an update for the privileged worker."""
    if update.runtime_status().get("state") in ("starting", "running"):
        return jsonify({"error": "a platform update is already running"}), 409
    user = guards.current_user()
    job, created = update.request(user["id"])
    if created:
        audit.log_action(user["id"], "platform.update_requested", None,
                         {"job_id": job["id"]})
    return jsonify({"job": dict(job), "created": created}), 202
