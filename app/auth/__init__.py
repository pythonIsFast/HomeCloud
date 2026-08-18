"""Auth blueprint: local accounts, JWT sessions and API keys.

Everything here is written from scratch on top of the standard library and the
pieces that ship with Flask (werkzeug for password hashing) -- no Flask-Login,
no Flask-WTF, no PyJWT. See CLAUDE.md for the dependency policy.
"""

from flask import Blueprint

bp = Blueprint("auth", __name__, url_prefix="/auth")

# Imported at the bottom so the routes can decorate the blueprint above.
from . import routes  # noqa: E402,F401
