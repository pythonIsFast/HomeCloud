"""Read-only update checks and the admin update request boundary.

The web process may inspect the git remote and enqueue an update, but never
executes the updater itself. The privileged VMM worker starts the fixed
``homecloud-update.service`` unit for the actual update.
"""

import os
import subprocess
import time

from flask import current_app

from . import jobs


_STATUS_KEYS = {"state", "message", "started_at", "finished_at", "job_id"}


def _status_file():
    return os.environ.get(
        "HOMECLOUD_UPDATE_STATUS_FILE",
        "/var/lib/homecloud/update.status",
    )


def runtime_status():
    """Return the updater's durable status without trusting arbitrary fields."""
    status = {"state": "idle"}
    try:
        path = _status_file()
        modified_at = os.path.getmtime(path)
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle.read(16384).splitlines():
                key, separator, value = line.partition("=")
                if separator and key in _STATUS_KEYS:
                    status[key] = value
    except FileNotFoundError:
        return status
    except OSError as error:
        return {"state": "unknown", "message": str(error)}

    if status.get("state") not in {"starting", "running", "succeeded", "failed"}:
        return {"state": "unknown", "message": "The update status file is invalid."}

    # A power loss or killed updater can leave the last atomic record at
    # "running" forever. Once the record is no longer fresh, systemd is the
    # authoritative source for whether the oneshot unit still exists.
    if status["state"] in {"starting", "running"} and time.time() - modified_at > 60:
        try:
            service = subprocess.run(
                ["systemctl", "is-active", "--quiet", "homecloud-update.service"],
                capture_output=True,
                timeout=3,
                check=False,
            )
            if service.returncode in (3, 4):
                return {
                    **status,
                    "state": "failed",
                    "message": "The update service stopped before reporting completion.",
                }
        except (OSError, subprocess.SubprocessError):
            pass
    return status


def write_runtime_status(state, message, job_id=None):
    """Atomically publish the worker-to-updater handoff state.

    The privileged worker calls this immediately before asking systemd to start
    the updater. The shell updater replaces it with its own progress record.
    """
    path = _status_file()
    directory = os.path.dirname(path)
    temporary = f"{path}.{os.getpid()}.tmp"
    os.makedirs(directory, mode=0o755, exist_ok=True)
    lines = [
        f"state={state}",
        f"message={str(message).replace(chr(10), ' ')[:500]}",
        f"started_at={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        "finished_at=",
        f"job_id={job_id or ''}",
    ]
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def _repository_dir():
    return os.path.abspath(os.path.join(current_app.root_path, os.pardir))


def _git(*args):
    repository = _repository_dir()
    result = subprocess.run(
        ["git", "-c", f"safe.directory={repository}", *args],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    return result.stdout.strip()


def check():
    """Compare the checked-out commit with the configured origin branch."""
    branch = os.environ.get("HOMECLOUD_UPDATE_BRANCH", "main")
    if not os.path.exists(os.path.join(_repository_dir(), ".git")):
        return {
            "ok": False,
            "error": (
                "This installation is not a Git checkout. Clone HomeCloud from "
                "GitHub before using platform updates."
            ),
        }
    try:
        current = _git("rev-parse", "HEAD")
        _git("remote", "get-url", "origin")
        remote = _git("ls-remote", "origin", f"refs/heads/{branch}")
        latest = remote.split()[0] if remote else ""
        if not latest:
            raise RuntimeError("remote branch returned no commit")
        return {
            "ok": True,
            "branch": branch,
            "current": current,
            "latest": latest,
            "update_available": current != latest,
            "checked_at": int(time.time()),
        }
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as error:
        detail = getattr(error, "stderr", "") or str(error)
        return {"ok": False, "error": detail.strip()}


def request(user_id):
    """Queue one platform update unless one is already queued or running."""
    if runtime_status().get("state") in ("starting", "running"):
        return {"id": None, "status": "running"}, False
    previous = jobs.latest_update()
    if previous and previous["status"] in ("queued", "running"):
        return previous, False
    job_id = jobs.enqueue("update", user_id=user_id)
    return jobs.latest_update() or {"id": job_id, "status": "queued"}, True
