"""Host-side Serveo HTTP tunnels for private microVM ports.

The tunnel runs on the HomeCloud host and targets the VM's private address.
Guests therefore need no SSH client, account, key or HomeCloud agent.
"""

import os
import re
import shutil
import signal
import stat
import subprocess
import time


class ServeoError(Exception):
    pass


URL_RE = re.compile(r"https://[a-z0-9.-]+", re.IGNORECASE)
SYSTEM_SSH_PROXY_CONFIG = "/etc/ssh/ssh_config.d/20-systemd-ssh-proxy.conf"
URL_WAIT_SECONDS = 15


def require_tools():
    if shutil.which("ssh") is None:
        raise ServeoError("OpenSSH client is missing on the HomeCloud host")
    try:
        metadata = os.stat(SYSTEM_SSH_PROXY_CONFIG)
    except FileNotFoundError:
        return
    except OSError:
        return
    mode = stat.S_IMODE(metadata.st_mode)
    if metadata.st_uid != 0 or mode & 0o022:
        raise ServeoError(
            "Host SSH configuration has unsafe ownership or permissions. Run: "
            "sudo chown root:root /etc/ssh/ssh_config.d/20-systemd-ssh-proxy.conf "
            "&& sudo chmod 644 /etc/ssh/ssh_config.d/20-systemd-ssh-proxy.conf"
        )


def log_path(vm_dir):
    return os.path.join(vm_dir, "serveo.log")


def known_hosts_path(vm_dir):
    return os.path.join(vm_dir, "serveo-known-hosts")


def read_url(vm_dir):
    try:
        with open(log_path(vm_dir), "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()[-32768:]
    except OSError:
        return None
    matches = URL_RE.findall(text)
    return matches[-1] if matches else None


def read_error(vm_dir):
    try:
        with open(log_path(vm_dir), "r", encoding="utf-8", errors="replace") as handle:
            lines = [line.strip() for line in handle.readlines()[-12:] if line.strip()]
    except OSError:
        return "Serveo tunnel exited before returning a URL"
    return " ".join(lines)[-600:] or "Serveo tunnel exited before returning a URL"


def is_alive(pid):
    if not pid:
        return False
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as handle:
            command = handle.read()
        return b"ssh" in command and b"serveo.net" in command
    except (OSError, ValueError):
        return False


def _reap(pid):
    try:
        os.waitpid(int(pid), os.WNOHANG)
    except (ChildProcessError, OSError, TypeError, ValueError):
        pass


def start(vm_dir, guest_ip, port, subdomain=""):
    """Start a Serveo remote forward and wait until it announces its URL."""
    require_tools()
    os.makedirs(vm_dir, exist_ok=True)
    remote = f"{subdomain}:80:{guest_ip}:{int(port)}" if subdomain else f"80:{guest_ip}:{int(port)}"
    command = [
        # Do not use ``-N`` here.  Serveo sends the assigned public URL through
        # its remote command output; ``-N`` suppresses that channel while
        # leaving the forward alive, which made the UI wait forever for a URL.
        "ssh", "-T",
        "-o", "BatchMode=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={known_hosts_path(vm_dir)}",
        "-R", remote,
        "serveo.net",
    ]
    with open(log_path(vm_dir), "wb") as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=vm_dir,
            start_new_session=True,
        )

    deadline = time.monotonic() + URL_WAIT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ServeoError(read_error(vm_dir))
        url = read_url(vm_dir)
        if url:
            return {"pid": process.pid, "url": url}
        time.sleep(0.2)

    # A running SSH process is not sufficient: without the announced URL the
    # tunnel cannot be used from the UI.  Tear it down instead of persisting an
    # unresolvable "waiting for URL" state.
    try:
        process.terminate()
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    except OSError:
        pass
    details = read_error(vm_dir)
    raise ServeoError(
        f"Serveo did not announce a public URL within {URL_WAIT_SECONDS} seconds. "
        f"{details}"
    )


def stop(pid):
    if not is_alive(pid):
        _reap(pid)
        return
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (OSError, ValueError):
        return
    for _ in range(30):
        if not is_alive(pid):
            _reap(pid)
            return
        time.sleep(0.1)
    try:
        os.kill(int(pid), signal.SIGKILL)
    except (OSError, ValueError):
        pass
    _reap(pid)
