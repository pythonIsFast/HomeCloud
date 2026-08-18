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
    try:
        current = _git("rev-parse", "HEAD")
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
    previous = jobs.latest_update()
    if previous and previous["status"] in ("queued", "running"):
        return previous, False
    job_id = jobs.enqueue("update", user_id=user_id)
    return jobs.latest_update() or {"id": job_id, "status": "queued"}, True
