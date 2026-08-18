"""The privileged VM worker.

Run it as root, because creating a tap device and adding a NAT rule needs
CAP_NET_ADMIN:

    sudo -E env "PATH=$PATH" python -m app.vmm

It does two things in a loop:

  1. claim the next job from the ``jobs`` table and execute it
  2. supervise the running VMs -- if a firecracker process is gone (the guest
     powered itself off, or it crashed), write that back to the registry

It reuses the Flask application object purely for its configuration and the
database helpers; no HTTP server is started. Each pass runs in its own
application context, so every cycle gets a fresh, short-lived SQLite
connection instead of holding one open for days.
"""

import json
import os
import platform
import signal
import sys
import time

from .. import audit, jobs
from ..core import resources
from . import console, firecracker, images, net


class Worker:
    def __init__(self, app, poll_seconds=2.0):
        self.app = app
        self.poll_seconds = poll_seconds
        self.host = jobs.local_host_name()
        self.running = True
        self.console_bridges = {}

    # --- setup ------------------------------------------------------------

    def preflight(self):
        """Check everything that would otherwise fail per-VM, once, loudly."""
        config = self.app.config
        architecture = platform.machine().lower()

        if architecture not in ("x86_64", "aarch64"):
            raise SystemExit(
                "unsupported host architecture "
                f"{architecture!r}: HomeCloud requires x86_64 or aarch64"
            )
        self.log(f"host architecture: {architecture}")

        if os.geteuid() != 0:
            self.log("WARNING: not running as root -- tap devices and NAT will fail")

        if not os.path.exists("/dev/kvm"):
            raise SystemExit("/dev/kvm is missing: this host cannot run microVMs")
        if not os.access("/dev/kvm", os.R_OK | os.W_OK):
            raise SystemExit("/dev/kvm is not accessible (add the user to group kvm)")

        binary = config["FIRECRACKER_BIN"]
        if not os.access(binary, os.X_OK):
            raise SystemExit(f"firecracker binary not executable: {binary}")
        if not os.path.exists(config["VM_KERNEL"]):
            raise SystemExit(f"guest kernel missing: {config['VM_KERNEL']}")
        if not os.path.exists(config["VM_BASE_ROOTFS"]):
            raise SystemExit(
                f"base image missing: {config['VM_BASE_ROOTFS']}\n"
                "build it once with: flask --app app compute-build-image"
            )

        net.require_tools()
        images.require_tools()
        net.enable_forwarding()

        egress = config["VM_EGRESS_IF"] or net.default_egress_interface()
        net.ensure_nat(config["VM_SUBNET_PREFIX"], egress)
        self.log(f"NAT ready: {config['VM_SUBNET_PREFIX']}.0.0/16 via {egress}")

        requeued = jobs.reset_stale_running()
        if requeued:
            self.log(f"requeued {requeued} job(s) left running by a previous worker")

    def log(self, message):
        print(f"[vmm] {message}", flush=True)

    # --- main loop --------------------------------------------------------

    def run(self):
        signal.signal(signal.SIGINT, self._stop)
        signal.signal(signal.SIGTERM, self._stop)

        with self.app.app_context():
            self.preflight()

        self.log(f"worker up as host {self.host!r}, polling every {self.poll_seconds}s")

        while self.running:
            worked = False
            try:
                with self.app.app_context():
                    job = jobs.claim_next(self.host)
                    if job is not None:
                        worked = True
                        self.handle(job)
                    else:
                        self.supervise()
            except Exception as error:  # never let the loop die
                self.log(f"loop error: {error!r}")
                time.sleep(1.0)

            if not worked:
                time.sleep(self.poll_seconds)

        self.log("worker stopped (VMs keep running)")

    def _stop(self, signum, frame):
        self.running = False

    # --- job dispatch -----------------------------------------------------

    def handle(self, job):
        action = job["action"]
        resource_id = job["resource_id"]
        self.log(f"job {job['id']}: {action} resource={resource_id}")

        try:
            handler = getattr(self, f"do_{action}")
            handler(job)
        except Exception as error:
            message = f"{type(error).__name__}: {error}"
            self.log(f"job {job['id']} failed: {message}")
            given_up = jobs.fail(job["id"], message)
            if given_up and resource_id:
                resources.set_status(resource_id, "error")
                resources.merge_config(resource_id, {"last_error": message[:500]})
                audit.log_action(job["user_id"], f"compute.{action}_failed",
                                 resource_id, {"error": message[:200]})
        else:
            jobs.finish(job["id"])
            self.log(f"job {job['id']} done")

    # --- individual actions -----------------------------------------------

    def vm_dir(self, resource_id):
        return os.path.join(self.app.config["VM_DIR"], str(resource_id))

    def config_of(self, row):
        try:
            return json.loads(row["config_json"] or "{}")
        except json.JSONDecodeError:
            return {}

    def do_create(self, job):
        row = resources.get_any(job["resource_id"])
        if row is None:
            raise RuntimeError("resource no longer exists")

        config = self.config_of(row)
        plan = net.plan(row["id"], self.app.config["VM_SUBNET_PREFIX"])
        directory = self.vm_dir(row["id"])
        os.makedirs(directory, exist_ok=True)

        resources.set_status(row["id"], "creating")

        self.log(f"  building disk ({config.get('disk_gb', 1)} GB)")
        rootfs = os.path.join(directory, "rootfs.ext4")
        images.create_vm_disk(
            self.app.config["VM_BASE_ROOTFS"], rootfs, config.get("disk_gb", 1)
        )

        net.create_tap(plan)
        pid = self._boot(row, config, plan, directory, rootfs)
        self.ensure_console_bridge(row["id"])

        resources.merge_config(row["id"], {
            "tap": plan["tap"],
            "ip": plan["guest_ip"],
            "gateway": plan["host_ip"],
            "netmask": plan["netmask"],
            "mac": plan["mac"],
            "pid": pid,
            "rootfs": rootfs,
            "last_error": None,
        })
        resources.set_status(row["id"], "running")
        audit.log_action(job["user_id"], "compute.create", row["id"],
                         {"ip": plan["guest_ip"], "vcpu": config.get("vcpu"),
                          "memory_mb": config.get("memory_mb")})

    def do_start(self, job):
        row = resources.get_any(job["resource_id"])
        if row is None:
            raise RuntimeError("resource no longer exists")

        config = self.config_of(row)
        directory = self.vm_dir(row["id"])
        rootfs = config.get("rootfs") or os.path.join(directory, "rootfs.ext4")
        if not os.path.exists(rootfs):
            raise RuntimeError("disk image is missing; recreate the instance")

        if firecracker.is_alive(config.get("pid")):
            self.log("  already running")
            resources.set_status(row["id"], "running")
            return

        plan = net.plan(row["id"], self.app.config["VM_SUBNET_PREFIX"])
        net.create_tap(plan)
        pid = self._boot(row, config, plan, directory, rootfs)
        self.ensure_console_bridge(row["id"])

        resources.merge_config(row["id"], {"pid": pid, "last_error": None})
        resources.set_status(row["id"], "running")
        audit.log_action(job["user_id"], "compute.start", row["id"])

    def do_stop(self, job):
        row = resources.get_any(job["resource_id"])
        if row is None:
            raise RuntimeError("resource no longer exists")

        config = self.config_of(row)
        outcome = firecracker.shutdown(self.vm_dir(row["id"]), config.get("pid"))
        self.stop_console_bridge(row["id"])
        net.delete_tap(config.get("tap") or f"hc-vm{row['id']}")

        resources.merge_config(row["id"], {"pid": None})
        resources.set_status(row["id"], "stopped")
        audit.log_action(job["user_id"], "compute.stop", row["id"], {"via": outcome})

    def do_restart(self, job):
        self.do_stop(job)
        self.do_start(job)

    def do_delete(self, job):
        row = resources.get_any(job["resource_id"])
        if row is None:
            return  # already gone, nothing to clean

        config = self.config_of(row)
        firecracker.shutdown(self.vm_dir(row["id"]), config.get("pid"))
        self.stop_console_bridge(row["id"])
        net.delete_tap(config.get("tap") or f"hc-vm{row['id']}")
        images.remove_vm_dir(self.vm_dir(row["id"]))

        resources.merge_config(row["id"], {"pid": None, "rootfs": None})
        resources.set_status(row["id"], "deleted")
        audit.log_action(job["user_id"], "compute.delete", row["id"])

    def _boot(self, row, config, plan, directory, rootfs):
        vm_config = firecracker.build_config(
            directory,
            self.app.config["VM_KERNEL"],
            rootfs,
            config.get("vcpu", 1),
            config.get("memory_mb", 256),
            plan,
        )
        config_path = firecracker.write_config(directory, vm_config)
        pid = firecracker.spawn(
            self.app.config["FIRECRACKER_BIN"], directory, config_path
        )
        self.log(f"  booted pid={pid} ip={plan['guest_ip']}")
        return pid

    def ensure_console_bridge(self, resource_id):
        """Make a running VM's serial input reachable by the web process."""
        bridge = self.console_bridges.get(resource_id)
        if bridge is None:
            bridge = console.ConsoleBridge(self.vm_dir(resource_id))
            self.console_bridges[resource_id] = bridge
        bridge.start()

    def stop_console_bridge(self, resource_id):
        bridge = self.console_bridges.pop(resource_id, None)
        if bridge is not None:
            bridge.stop()

    # --- supervision ------------------------------------------------------

    def supervise(self):
        """Reconcile registry state with the processes actually alive.

        A guest that runs ``poweroff`` makes firecracker exit; nothing tells the
        database about it. So whatever claims to be running gets checked, and
        anything whose process is gone is marked stopped.
        """
        rows = resources.query_running("compute")
        for row in rows:
            if jobs.pending_for_resource(row["id"]):
                continue  # a job is about to change this anyway

            config = self.config_of(row)
            if firecracker.is_alive(config.get("pid")):
                self.ensure_console_bridge(row["id"])
                continue

            self.log(f"resource {row['id']}: process gone, marking stopped")
            self.stop_console_bridge(row["id"])
            net.delete_tap(config.get("tap") or f"hc-vm{row['id']}")
            resources.merge_config(row["id"], {"pid": None})
            resources.set_status(row["id"], "stopped")
            audit.log_action(row["user_id"], "compute.exited", row["id"],
                             {"reason": "process gone"})


def main(argv=None):
    # Imported here so "python -m app.vmm" does not build an app just to fail on
    # a missing dependency in the import block above.
    from .. import create_app

    app = create_app()
    poll = float(os.environ.get("HOMECLOUD_VMM_POLL", "2.0"))
    Worker(app, poll_seconds=poll).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
