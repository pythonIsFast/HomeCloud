"""Request authentication: turn an incoming request into a current user.

Two credential types are accepted, checked in this order:

  1. Authorization: Bearer <jwt>   -- API clients that logged in
  2. X-API-Key: hc_...             -- long-lived machine credentials
  3. homecloud_token cookie        -- browser session (HttpOnly cookie)

The cookie holds the same JWT as the Bearer header, so "sessions" are
stateless: nothing is stored server side, validity comes from the HMAC
signature plus the exp claim. Logout simply clears the cookie.
"""

import functools

from flask import current_app, g, jsonify, redirect, request, url_for

from . import jwt, models


def issue_token(user_row):
    """Create a signed access token for a user row."""
    return jwt.encode(
        {
            "sub": user_row["id"],
            "email": user_row["email"],
            "role": user_row["role"],
        },
        current_app.config["SECRET_KEY"],
        ttl_seconds=current_app.config["JWT_TTL_SECONDS"],
    )


def _user_from_token(token):
    """Validate a JWT and load the referenced user, or return None."""
    try:
        claims = jwt.decode(token, current_app.config["SECRET_KEY"])
    except jwt.JWTError:
        return None
    return models.get_user_by_id(claims.get("sub"))


def current_user():
    """Return the authenticated user row for this request, or None.

    Cached on ``g`` so repeated calls inside one request hit the DB once.
    """
    if "current_user" in g:
        return g.current_user

    user = None

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        user = _user_from_token(auth_header[len("Bearer ") :].strip())

    if user is None:
        api_key = request.headers.get("X-API-Key")
        if api_key:
            user = models.get_user_by_api_key(api_key.strip())

    if user is None:
        cookie = request.cookies.get(current_app.config["JWT_COOKIE_NAME"])
        if cookie:
            user = _user_from_token(cookie)

    g.current_user = user
    return user


def _unauthorized():
    """JSON 401 for API calls, redirect to the login page for browser calls."""
    if request.path.startswith("/api/") or "/api/" in request.path:
        return jsonify({"error": "authentication required"}), 401
    return redirect(url_for("auth.login_page", next=request.path))


def login_required(view):
    """Decorator: reject the request unless a valid credential was presented."""

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            return _unauthorized()
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    """Decorator: like login_required, but also requires role == 'admin'."""

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            return _unauthorized()
        if user["role"] != "admin":
            return jsonify({"error": "admin role required"}), 403
        return view(*args, **kwargs)

    return wrapped


def set_token_cookie(response, token):
    """Attach the JWT to the response as an HttpOnly cookie."""
    response.set_cookie(
        current_app.config["JWT_COOKIE_NAME"],
        token,
        max_age=current_app.config["JWT_TTL_SECONDS"],
        httponly=True,  # not readable from JavaScript -> mitigates XSS token theft
        secure=current_app.config["JWT_COOKIE_SECURE"],
        samesite="Lax",  # cookie is not sent on cross-site POSTs -> basic CSRF guard
        path="/",
    )
    return response


def clear_token_cookie(response):
    response.delete_cookie(current_app.config["JWT_COOKIE_NAME"], path="/")
    return response
