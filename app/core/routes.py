"""Core HTTP endpoints: dashboard page, resource registry API, admin, health."""

from flask import current_app, jsonify, render_template, request

from .. import audit, limits
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
