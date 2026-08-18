"""Core blueprint: the dashboard and the service-agnostic resource registry."""

from flask import Blueprint

bp = Blueprint("core", __name__)

# Imported at the bottom so the routes can decorate the blueprint above.
from . import routes  # noqa: E402,F401
