"""Talking to Firecracker: config file, process spawn, Unix-socket API.

Firecracker takes its whole boot configuration as a JSON file, so creating a VM
is "write JSON, exec firecracker". The Unix-socket REST API is only needed
afterwards, for actions such as a graceful shutdown.

There is no HTTP-over-Unix-socket client in the standard library, but
http.client.HTTPConnection only needs its connect() replaced -- about ten lines
below. That is why this works without the requests package.
"""

import http.client
import json
import os
import platform
import signal
import socket
import subprocess
import time

from . import console as serial_console

# Kernel command line. Notable parts:
#   ip=...        static address for the guest, so no DHCP server is needed
#   reboot=k      a reboot inside the guest exits firecracker instead of hanging
#   panic=1       a kernel panic exits too, so a broken VM does not linger
#   pci=off       microVMs have no PCI bus; skipping the probe saves boot time
def _boot_args(net_plan):
    """Return a kernel command line for the host's CPU architecture.

    Firecracker runs same-architecture guests only.  ``pci=off`` is useful on
    x86_64 where this project uses Firecracker's MMIO devices, but it is not an
    ARM boot parameter and must not be passed to aarch64 guests.
    """
    machine = platform.machine().lower()
    args = ["console=ttyS0", "panic=1", "nomodule", "ro", "root=/dev/vda"]
    if machine == "x86_64":
        args.extend(("reboot=k", "pci=off"))
    args.append(
        "ip={guest_ip}::{host_ip}:{netmask}::eth0:off".format(**net_plan)
    )
    return " ".join(args)


class FirecrackerError(Exception):
    pass


class _UnixHTTPConnection(http.client.HTTPConnection):
    """http.client over an AF_UNIX socket instead of TCP."""

    def __init__(self, socket_path, timeout=5.0):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


def api_request(socket_path, method, path, body=None, timeout=5.0):
    """One request against a VM's API socket. Returns (status, parsed body)."""
    connection = _UnixHTTPConnection(socket_path, timeout=timeout)
    try:
        payload = json.dumps(body) if body is not None else None
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            parsed = {"raw": raw}
        return response.status, parsed
    finally:
        connection.close()


def build_config(vm_dir, kernel_path, rootfs_path, vcpu, memory_mb, net_plan):
    """The JSON Firecracker boots from. Written next to the VM's disk."""
    return {
        "boot-source": {
            "kernel_image_path": kernel_path,
            "boot_args": _boot_args(net_plan),
        },
        "drives": [
            {
                "drive_id": "rootfs",
                "path_on_host": rootfs_path,
                "is_root_device": True,
                "is_read_only": False,
            }
        ],
        "machine-config": {
            "vcpu_count": int(vcpu),
            "mem_size_mib": int(memory_mb),
            # Ballooning and hugepages stay off: predictable memory accounting
            # matters more here than density.
            "smt": False,
        },
        "network-interfaces": [
            {
                "iface_id": "eth0",
                "host_dev_name": net_plan["tap"],
                "guest_mac": net_plan["mac"],
            }
        ],
        "logger": {
            "log_path": os.path.join(vm_dir, "firecracker.log"),
            "level": "Warning",
            "show_level": False,
            "show_log_origin": False,
        },
    }


def write_config(vm_dir, config):
    path = os.path.join(vm_dir, "config.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
    return path


def spawn(binary, vm_dir, config_path):
    """Start a firecracker process for one VM and return its pid.

    The API socket must not exist beforehand, and firecracker's own log file has
    to be present before it starts -- it will not create it.
    """
    socket_path = os.path.join(vm_dir, "api.sock")
    if os.path.exists(socket_path):
        os.remove(socket_path)

    log_path = os.path.join(vm_dir, "firecracker.log")
    open(log_path, "a", encoding="utf-8").close()

    fifo_path = serial_console.input_fifo_path(vm_dir)
    try:
        os.mkfifo(fifo_path, 0o660)
    except FileExistsError:
        pass

    # A read/write descriptor keeps the FIFO alive for the child. Unlike a
    # subprocess pipe it remains usable after the worker itself restarts.
    console_input = os.open(fifo_path, os.O_RDWR)
    console = open(os.path.join(vm_dir, "console.log"), "ab", buffering=0)
    try:
        process = subprocess.Popen(
            [binary, "--api-sock", socket_path, "--config-file", config_path],
            stdin=console_input,
            stdout=console,
            stderr=console,
            cwd=vm_dir,
            # Own session: a signal to the worker's process group does not take
            # the running VMs down with it.
            start_new_session=True,
        )
    finally:
        console.close()
        os.close(console_input)

    # Give it a moment to fail loudly (bad kernel path, tap missing, no KVM).
    time.sleep(0.4)
    if process.poll() is not None:
        raise FirecrackerError(
            f"firecracker exited immediately (code {process.returncode}); "
            f"see {os.path.join(vm_dir, 'console.log')}"
        )

    with open(os.path.join(vm_dir, "pid"), "w", encoding="ascii") as handle:
        handle.write(str(process.pid))
    return process.pid


def is_alive(pid):
    """True if the pid exists and still is a firecracker process.

    The cmdline check guards against a recycled pid: after a reboot some other
    process may well own the number we stored.
    """
    if not pid:
        return False
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as handle:
            return b"firecracker" in handle.read()
    except (OSError, ValueError):
        return False


def shutdown(vm_dir, pid, timeout=20.0):
    """Ask the guest to power down, then insist.

    Ctrl+Alt+Del is what Firecracker offers for a graceful stop; the Linux guest
    turns it into a normal shutdown. If it has not exited by the timeout we
    escalate to SIGTERM and finally SIGKILL, because a stuck VM must not block
    the queue.
    """
    socket_path = os.path.join(vm_dir, "api.sock")

    if os.path.exists(socket_path):
        try:
            api_request(socket_path, "PUT", "/actions",
                        {"action_type": "SendCtrlAltDel"})
        except (OSError, http.client.HTTPException):
            pass  # fall through to signals

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_alive(pid):
            return "graceful"
        time.sleep(0.3)

    for sig, label in ((signal.SIGTERM, "sigterm"), (signal.SIGKILL, "sigkill")):
        try:
            os.kill(int(pid), sig)
        except (OSError, ValueError):
            return "gone"
        for _ in range(20):
            if not is_alive(pid):
                return label
            time.sleep(0.2)

    return "stuck"


def read_console(vm_dir, after=0, max_bytes=4096, initial_bytes=16384):
    """Read one bounded incremental slice of the guest serial console.

    The first read gets a useful tail. Later reads continue at ``after`` so the
    browser never downloads and re-renders the full terminal once per poll.
    """
    path = os.path.join(vm_dir, "console.log")
    try:
        size = os.path.getsize(path)
        after = max(0, int(after))
        reset = after > size
        if after == 0 or reset:
            start = max(0, size - initial_bytes)
        else:
            start = after
        end = min(size, start + max_bytes)
        with open(path, "rb") as handle:
            handle.seek(start)
            text = handle.read(end - start).decode("utf-8", "replace")
        return {"console": text, "offset": end, "more": end < size, "reset": reset}
    except OSError:
        return {"console": "", "offset": 0, "more": False, "reset": False}


def tail_console(vm_dir, max_bytes=16384):
    """Compatibility helper for callers that only need one console tail."""
    return read_console(vm_dir, initial_bytes=max_bytes)["console"]
