"""Core HTTP endpoints: dashboard page, resource registry API, health check."""

from flask import current_app, jsonify, render_template, request

from .. import audit
from ..auth import guards
from . import bp, resources


@bp.get("/")
@guards.login_required
def dashboard():
    """The dashboard shell. All data is fetched by static/js/dashboard.js."""
    return render_template(
        "dashboard.html",
        app_name=current_app.config["APP_NAME"],
        user=guards.current_user(),
    )


@bp.get("/healthz")
def healthz():
    """Unauthenticated liveness probe for nginx / monitoring."""
    return jsonify({"status": "ok", "app": current_app.config["APP_NAME"]})


@bp.get("/api/resources")
@guards.login_required
def list_resources():
    """List the current user's resources.

    Optional query parameter: ?service_type=compute
    """
    user = guards.current_user()
    service_type = request.args.get("service_type") or None
    rows = resources.list_for_user(user["id"], service_type)
    return jsonify({"resources": [resources.to_dict(row) for row in rows]})


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
    """Recent audit entries for the current user (admins see everything)."""
    user = guards.current_user()
    rows = audit.recent(limit=50, user_id=None if user["role"] == "admin" else user["id"])
    return jsonify({"entries": [dict(row) for row in rows]})
