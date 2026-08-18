"""Compute service: Firecracker microVMs.

Instances are rows in the shared ``resources`` table with
``service_type='compute'`` -- no table of its own, as required by the resource
pattern in CLAUDE.md. Everything host-facing lives in ``app/vmm/`` and runs in
the privileged worker; this blueprint only validates, checks quota and enqueues
jobs.
"""

from flask import Blueprint

bp = Blueprint("compute", __name__, url_prefix="/compute")

# Imported at the bottom so the routes can decorate the blueprint above.
from . import routes  # noqa: E402,F401
