"""Small request-security layer with no external services or packages."""

import threading
import time
import secrets
from collections import defaultdict, deque

from flask import g, jsonify, request

from . import platform_settings


class RateLimiter:
    """In-process sliding-window limiter.

    HomeCloud is a single-host deployment. Keeping only timestamps avoids a
    dependency on Redis while still protecting login, upload and API endpoints.
    Each gunicorn worker has its own conservative window.
    """

    def __init__(self):
        self.windows = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key, limit, period):
        now = time.monotonic()
        with self.lock:
            window = self.windows[key]
            while window and window[0] <= now - period:
                window.popleft()
            if len(window) >= limit:
                return False, max(1, int(period - (now - window[0])))
            window.append(now)
            return True, 0


def client_address():
    """Honor nginx's address header only when nginx is the direct peer."""
    if request.remote_addr in ("127.0.0.1", "::1"):
        return request.headers.get("X-Real-IP", request.remote_addr)
    return request.remote_addr or "unknown"


def register(app):
    limiter = RateLimiter()

    @app.before_request
    def apply_rate_limit():
        g.csp_nonce = secrets.token_urlsafe(18)
        path = request.path
        if path.startswith("/static/") or path == "/healthz":
            return None
        if path in ("/auth/api/login", "/auth/api/register"):
            limit = platform_settings.value("auth_rate_limit")
            period = platform_settings.value("auth_rate_window_seconds")
            bucket = "auth"
        elif request.method in ("POST", "PUT", "PATCH", "DELETE"):
            limit, period, bucket = platform_settings.value("write_rate_limit"), 60, "write"
        elif "/api/" in path or path.startswith("/api/"):
            limit, period, bucket = platform_settings.value("api_rate_limit"), 60, "api"
        else:
            return None
        allowed, retry_after = limiter.allow((bucket, client_address()), limit, period)
        if allowed:
            return None
        response = jsonify({"error": "rate limit exceeded", "retry_after": retry_after})
        response.status_code = 429
        response.headers["Retry-After"] = str(retry_after)
        return response

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            f"script-src 'self' 'nonce-{g.csp_nonce}'; connect-src 'self'; "
            "base-uri 'self'; frame-ancestors 'none'",
        )
        if request.path.startswith(("/auth/", "/api/", "/compute/")):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    app.extensions["homecloud_rate_limiter"] = limiter

    @app.context_processor
    def security_context():
        return {"csp_nonce": getattr(g, "csp_nonce", "")}
