"""Compute HTTP endpoints.

Thin on purpose: parse, delegate to service.py, translate ComputeError into a
status code. No host access, no SQL.
"""

import hashlib
import json
import os
import tempfile
import time

from flask import Response, current_app, jsonify, request

from ...auth import guards
from ...core import resources
from ...vmm import console, firecracker
from . import bp, flavors, service


def _payload():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else request.form.to_dict()


def _handle(callable_):
    """Run a service call and turn a domain rejection into a JSON error."""
    try:
        return callable_()
    except service.ComputeError as error:
        return jsonify({"error": str(error)}), error.status


@bp.get("/api/flavors")
@guards.login_required
def list_flavors():
    """The size catalogue plus the caller's quota, for the create form."""
    user = guards.current_user()
    return jsonify({
        "flavors": flavors.catalogue(),
        "default": flavors.DEFAULT_FLAVOR,
        **service.quota_summary(user),
    })


@bp.get("/api/instances")
@guards.login_required
def list_instances():
    """One keyset page of the caller's instances. ?before_id=<id>&limit=<n>"""
    user = guards.current_user()
    before_id = request.args.get("before_id", type=int)
    limit = request.args.get("limit", type=int)
    return jsonify(service.list_page(user, before_id=before_id, limit=limit))


@bp.post("/api/instances")
@guards.login_required
def create_instance():
    user = guards.current_user()
    data = _payload()

    def run():
        row = service.create_instance(
            user,
            data.get("name"),
            data.get("flavor"),
            data.get("image_id"),
        )
        return jsonify({"instance": service.public_view(row)}), 201

    return _handle(run)


@bp.get("/api/images")
@guards.login_required
def list_images():
    return jsonify({"images": service.list_images(guards.current_user()),
                    "upload_limit_bytes": current_app.config["MAX_CONTENT_LENGTH"]})


@bp.post("/api/images/snapshots")
@guards.login_required
def create_snapshot():
    data = _payload()
    return _handle(lambda: jsonify({"image": service.image_view(service.snapshot(
        guards.current_user(), data.get("instance_id"), data.get("name")
    ))}), 202)


@bp.post("/api/images/uploads")
@guards.login_required
def upload_image():
    """Stage one bounded raw ext4 image; the worker validates it unprivileged."""
    name = (request.args.get("name") or "").strip()
    filename = os.path.basename(request.args.get("filename") or "")[-255:]
    if request.mimetype != "application/octet-stream" or not filename:
        return jsonify({"error": "send the image as application/octet-stream"}), 400
    if not filename.lower().endswith((".ext4", ".img")):
        return jsonify({"error": "image filename must end in .ext4 or .img"}), 400
    if request.content_length is not None and request.content_length < 16 * 1024 * 1024:
        return jsonify({"error": "image must be at least 16 MiB"}), 400
    try:
        name = service.validate_image_import(guards.current_user(), name)
    except service.ComputeError as error:
        return jsonify({"error": str(error)}), error.status

    descriptor, staged = tempfile.mkstemp(prefix="image-", suffix=".upload",
                                          dir=current_app.config["UPLOAD_DIR"])
    size = 0
    digest = hashlib.sha256()
    try:
        with os.fdopen(descriptor, "wb") as handle:
            while True:
                block = request.stream.read(1024 * 1024)
                if not block:
                    break
                size += len(block)
                if size > current_app.config["MAX_CONTENT_LENGTH"]:
                    raise service.ComputeError("image exceeds the upload limit", status=413)
                handle.write(block)
                digest.update(block)
        row = service.import_image(guards.current_user(), name, staged, filename,
                                   size, digest.hexdigest())
    except service.ComputeError as error:
        try:
            os.remove(staged)
        except OSError:
            pass
        return jsonify({"error": str(error)}), error.status
    except Exception:
        try:
            os.remove(staged)
        except OSError:
            pass
        raise
    return jsonify({"image": service.image_view(row)}), 202


@bp.get("/api/instances/<int:resource_id>")
@guards.login_required
def get_instance(resource_id):
    user = guards.current_user()
    is_admin = user["role"] == "admin"
    return _handle(
        lambda: jsonify({
            "instance": service.get_instance(user, resource_id, include_secrets=is_admin)
        })
    )


@bp.post("/api/instances/<int:resource_id>/actions/<action>")
@guards.login_required
def instance_action(resource_id, action):
    user = guards.current_user()

    def run():
        row = service.perform_action(user, resource_id, action)
        return jsonify({"instance": service.public_view(row)}), 202

    return _handle(run)


@bp.delete("/api/instances/<int:resource_id>")
@guards.login_required
def delete_instance(resource_id):
    user = guards.current_user()

    def run():
        row = service.perform_action(user, resource_id, "delete")
        return jsonify({"instance": service.public_view(row)}), 202

    return _handle(run)


@bp.get("/api/instances/<int:resource_id>/console")
@guards.login_required
def instance_console(resource_id):
    """Tail of the guest serial console -- the only way to see a failed boot.

    Read-only and rate-limited by its size cap; the file is written by
    firecracker itself.
    """
    user = guards.current_user()
    row = resources.get(user["id"], resource_id)
    if row is None or row["service_type"] != service.SERVICE_TYPE:
        return jsonify({"error": "instance not found"}), 404

    vm_dir = os.path.join(current_app.config["VM_DIR"], str(resource_id))
    after = request.args.get("after", default=0, type=int)
    return jsonify(firecracker.read_console(vm_dir, after=max(0, after or 0)))


@bp.get("/api/instances/<int:resource_id>/console/stream")
@guards.login_required
def instance_console_stream(resource_id):
    """Continuously stream serial output as SSE without frontend polling."""
    user = guards.current_user()
    row = resources.get(user["id"], resource_id)
    if row is None or row["service_type"] != service.SERVICE_TYPE:
        return jsonify({"error": "instance not found"}), 404
    vm_dir = os.path.join(current_app.config["VM_DIR"], str(resource_id))
    start = max(0, request.args.get("after", default=0, type=int) or 0)

    def events():
        offset = start
        heartbeat = time.monotonic()
        while True:
            chunk = firecracker.read_console(vm_dir, after=offset)
            if chunk["data"] or chunk["reset"]:
                offset = chunk["offset"]
                yield "data:" + json.dumps(chunk, separators=(",", ":")) + "\n\n"
                heartbeat = time.monotonic()
                if chunk["more"]:
                    continue
            elif time.monotonic() - heartbeat >= 15:
                yield ": keepalive\n\n"
                heartbeat = time.monotonic()
            time.sleep(0.05)

    return Response(events(), headers={
        "Cache-Control": "no-cache, no-store",
        "X-Accel-Buffering": "no",
    }, mimetype="text/event-stream")


@bp.post("/api/instances/<int:resource_id>/console/input")
@guards.login_required
def instance_console_input(resource_id):
    """Forward a small terminal keystroke batch to the privileged worker."""
    user = guards.current_user()
    row = resources.get(user["id"], resource_id)
    if row is None or row["service_type"] != service.SERVICE_TYPE:
        return jsonify({"error": "instance not found"}), 404
    if row["status"] != "running":
        return jsonify({"error": "terminal is available only while running"}), 409

    value = _payload().get("input", "")
    if not isinstance(value, str):
        return jsonify({"error": "terminal input must be text"}), 400
    data = value.encode("utf-8")
    if not data or len(data) > console.MAX_INPUT_BYTES:
        return jsonify({"error": "terminal input must contain 1-4096 bytes"}), 400

    vm_dir = os.path.join(current_app.config["VM_DIR"], str(resource_id))
    try:
        console.send_input(vm_dir, data)
    except console.ConsoleError as error:
        return jsonify({"error": str(error)}), 409
    return jsonify({"ok": True})


@bp.put("/api/instances/<int:resource_id>/firewall")
@guards.login_required
def instance_firewall(resource_id):
    user = guards.current_user()
    rules = _payload().get("rules", [])
    return _handle(lambda: jsonify({"instance": service.public_view(
        service.update_firewall(user, resource_id, rules)
    )}))
