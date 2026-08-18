"""Auth HTTP endpoints: login page + JSON API for register/login/logout/keys."""

import re

from flask import current_app, jsonify, make_response, render_template, request

from .. import audit, platform_settings
from . import bp, guards, models

# Deliberately loose email check: "something@something.tld". Full RFC 5322
# validation needs a library, and we do not send confirmation mails anyway.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

MIN_PASSWORD_LENGTH = 8


def _payload():
    """Read credentials from a JSON body, falling back to form encoding."""
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data
    return request.form.to_dict()


def _user_json(user_row):
    """Public representation of a user -- never includes the password hash."""
    return {
        "id": user_row["id"],
        "email": user_row["email"],
        "role": user_row["role"],
        "created_at": user_row["created_at"],
    }


# --- pages ------------------------------------------------------------------


@bp.get("/login")
def login_page():
    """Combined login / registration page."""
    return render_template(
        "login.html",
        app_name=current_app.config["APP_NAME"],
        allow_registration=platform_settings.value("allow_registration"),
    )


# --- JSON API ---------------------------------------------------------------


@bp.post("/api/register")
def register():
    if not platform_settings.value("allow_registration"):
        return jsonify({"error": "registration is disabled"}), 403

    data = _payload()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not EMAIL_RE.match(email):
        return jsonify({"error": "invalid email address"}), 400
    if len(password) < MIN_PASSWORD_LENGTH:
        return (
            jsonify(
                {"error": f"password must be at least {MIN_PASSWORD_LENGTH} characters"}
            ),
            400,
        )
    if models.get_user_by_email(email) is not None:
        return jsonify({"error": "email is already registered"}), 409

    # The first account ever created becomes the admin of this installation.
    role = "admin" if models.count_users() == 0 else "user"
    user_id = models.create_user(email, password, role=role)
    audit.log_action(user_id, "user.register", details={"email": email, "role": role})

    user = models.get_user_by_id(user_id)
    return jsonify({"user": _user_json(user)}), 201


@bp.post("/api/login")
def login():
    data = _payload()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    user = models.get_user_by_email(email)
    # Same message for "unknown email" and "wrong password" -- do not tell an
    # attacker which accounts exist.
    if user is None or not models.verify_password(user, password):
        audit.log_action(None, "user.login_failed", details={"email": email})
        return jsonify({"error": "invalid credentials"}), 401

    token = guards.issue_token(user)
    audit.log_action(user["id"], "user.login")

    response = make_response(jsonify({"user": _user_json(user), "token": token}))
    return guards.set_token_cookie(response, token)


@bp.post("/api/logout")
def logout():
    """Clear the session cookie. Stateless tokens cannot be revoked server side,
    so a leaked Bearer token stays valid until its exp -- keep the TTL short."""
    user = guards.current_user()
    if user is not None:
        audit.log_action(user["id"], "user.logout")
    response = make_response(jsonify({"ok": True}))
    return guards.clear_token_cookie(response)


@bp.get("/api/me")
@guards.login_required
def me():
    return jsonify({"user": _user_json(guards.current_user())})


# --- API keys ---------------------------------------------------------------


@bp.get("/api/keys")
@guards.login_required
def list_keys():
    rows = models.list_api_keys(guards.current_user()["id"])
    return jsonify({"keys": [dict(row) for row in rows]})


@bp.post("/api/keys")
@guards.login_required
def create_key():
    user = guards.current_user()
    label = (_payload().get("label") or "").strip()[:100]
    key_id, plaintext = models.create_api_key(user["id"], label)
    audit.log_action(user["id"], "apikey.create", details={"key_id": key_id, "label": label})
    # "key" is returned exactly once and never stored in plaintext.
    return jsonify({"id": key_id, "label": label, "key": plaintext}), 201


@bp.delete("/api/keys/<int:key_id>")
@guards.login_required
def delete_key(key_id):
    user = guards.current_user()
    if not models.delete_api_key(user["id"], key_id):
        return jsonify({"error": "key not found"}), 404
    audit.log_action(user["id"], "apikey.delete", details={"key_id": key_id})
    return jsonify({"ok": True})
