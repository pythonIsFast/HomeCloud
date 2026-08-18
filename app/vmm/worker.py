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
import subprocess
import sys
import time

from .. import audit, jobs, platform_settings, update
from ..core import resources
from . import console, firecracker, images, net, serveo


class Worker:
    def __init__(self, app, poll_seconds=2.0):
        self.app = app
        self.poll_seconds = poll_seconds
        self.host = jobs.local_host_name()
        self.running = True
        self.console_bridges = {}
        self.usage_samples = {}

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

    def stop_serveo_bridge(self, row, disable=False):
        config = self.config_of(row)
        tunnel = dict(config.get("serveo") or {})
        serveo.stop(tunnel.get("pid"))
        tunnel.update({
            "enabled": False if disable else bool(tunnel.get("enabled")),
            "status": "stopped" if disable else "offline",
            "pid": None,
            "url": None,
            "error": None,
        })
        resources.merge_config(row["id"], {"serveo": tunnel})

    def start_serveo_bridge(self, row, user_id=None):
        config = self.config_of(row)
        tunnel = dict(config.get("serveo") or {})
        if not tunnel.get("enabled"):
            return
        serveo.stop(tunnel.get("pid"))
        try:
            result = serveo.start(
                self.vm_dir(row["id"]),
                config.get("ip"),
                tunnel.get("port"),
                tunnel.get("subdomain", ""),
            )
        except Exception as error:
            tunnel.update({"status": "error", "pid": None, "url": None,
                           "error": str(error)[:600]})
            resources.merge_config(row["id"], {"serveo": tunnel})
            audit.log_action(user_id or row["user_id"], "compute.serveo_failed", row["id"],
                             {"error": str(error)[:200]})
            return
        tunnel.update({"status": "running", "pid": result["pid"],
                       "url": result["url"], "error": None})
        resources.merge_config(row["id"], {"serveo": tunnel})
        audit.log_action(user_id or row["user_id"], "compute.serveo_started", row["id"],
                         {"port": tunnel.get("port"), "url": result["url"]})

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
        base_image = self.app.config["VM_BASE_ROOTFS"]
        if config.get("image_id"):
            image = resources.get_any(config["image_id"])
            image_config = self.config_of(image) if image else {}
            if image is None or image["user_id"] != row["user_id"] or image["status"] != "ready":
                raise RuntimeError("selected image is unavailable")
            base_image = image_config.get("path")
        images.create_vm_disk(base_image, rootfs, config.get("disk_gb", 1))

        net.create_tap(plan)
        net.apply_firewall(row["id"], plan, config.get("firewall", []))
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
            "usage": None,
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
        net.apply_firewall(row["id"], plan, config.get("firewall", []))
        pid = self._boot(row, config, plan, directory, rootfs)
        self.ensure_console_bridge(row["id"])

        resources.merge_config(row["id"], {"pid": pid, "usage": None, "last_error": None})
        resources.set_status(row["id"], "running")
        refreshed = resources.get_any(row["id"])
        if refreshed is not None:
            self.start_serveo_bridge(refreshed, job["user_id"])
        audit.log_action(job["user_id"], "compute.start", row["id"])

    def do_stop(self, job):
        row = resources.get_any(job["resource_id"])
        if row is None:
            raise RuntimeError("resource no longer exists")

        config = self.config_of(row)
        self.stop_serveo_bridge(row)
        outcome = firecracker.shutdown(self.vm_dir(row["id"]), config.get("pid"))
        self.stop_console_bridge(row["id"])
        net.delete_tap(config.get("tap") or f"hc-vm{row['id']}")
        net.delete_firewall(row["id"], net.plan(row["id"], self.app.config["VM_SUBNET_PREFIX"]))

        resources.merge_config(row["id"], {"pid": None, "usage": None})
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
        self.stop_serveo_bridge(row, disable=True)
        firecracker.shutdown(self.vm_dir(row["id"]), config.get("pid"))
        self.stop_console_bridge(row["id"])
        net.delete_tap(config.get("tap") or f"hc-vm{row['id']}")
        net.delete_firewall(row["id"], net.plan(row["id"], self.app.config["VM_SUBNET_PREFIX"]))
        images.remove_vm_dir(self.vm_dir(row["id"]))

        resources.merge_config(row["id"], {"pid": None, "rootfs": None, "usage": None})
        resources.set_status(row["id"], "deleted")
        audit.log_action(job["user_id"], "compute.delete", row["id"])

    def do_firewall(self, job):
        row = resources.get_any(job["resource_id"])
        if row is None:
            return
        config = self.config_of(row)
        if row["status"] == "running":
            plan = net.plan(row["id"], self.app.config["VM_SUBNET_PREFIX"])
            net.apply_firewall(row["id"], plan, config.get("firewall", []))
        audit.log_action(job["user_id"], "compute.firewall", row["id"],
                         {"rules": config.get("firewall", [])})

    def do_snapshot(self, job):
        image = resources.get_any(job["resource_id"])
        payload = jobs.payload_of(job)
        source = resources.get_any(payload.get("source_id"))
        if image is None or source is None or image["user_id"] != source["user_id"]:
            raise RuntimeError("snapshot source is unavailable")
        source_config = self.config_of(source)
        source_path = source_config.get("rootfs")
        target = os.path.join(self.app.config["IMAGE_DIR"], "user", str(image["user_id"]),
                              f"{image['id']}.ext4")
        checksum = images.copy_image(source_path, target)
        resources.merge_config(image["id"], {"path": target, "sha256": checksum,
                                               "size_bytes": os.path.getsize(target),
                                               "verified": True})
        resources.set_status(image["id"], "ready")
        audit.log_action(job["user_id"], "compute.snapshot", image["id"], {"source": source["id"]})

    def do_import_image(self, job):
        image = resources.get_any(job["resource_id"])
        if image is None or image["service_type"] != "compute_image":
            raise RuntimeError("uploaded image resource is unavailable")
        resources.set_status(image["id"], "creating")
        config = self.config_of(image)
        staged = config.get("staged_path")
        limit = platform_settings.upload_limit_bytes()
        target = os.path.join(self.app.config["IMAGE_DIR"], "user", str(image["user_id"]),
                              f"{image['id']}.ext4")
        try:
            images.validate_uploaded_ext4(staged, limit)
            checksum = images.copy_image(staged, target)
            if checksum != config.get("sha256"):
                raise RuntimeError("uploaded image checksum changed during validation")
        except Exception:
            for path in (staged, target):
                try:
                    os.remove(path)
                except (OSError, TypeError):
                    pass
            raise
        os.remove(staged)
        resources.merge_config(image["id"], {"path": target, "staged_path": None,
                                               "verified": True, "size_bytes": os.path.getsize(target)})
        resources.set_status(image["id"], "ready")
        audit.log_action(job["user_id"], "compute.image_import", image["id"],
                         {"sha256": checksum})

    def do_resize(self, job):
        """Apply a new CPU/RAM size and grow the disk while the VM is offline."""
        row = resources.get_any(job["resource_id"])
        if row is None:
            raise RuntimeError("resource no longer exists")
        payload = jobs.payload_of(job)
        config = self.config_of(row)
        if payload.get("was_running"):
            self.do_stop(job)
        rootfs = config.get("rootfs") or os.path.join(self.vm_dir(row["id"]), "rootfs.ext4")
        images.resize_vm_disk(rootfs, config.get("disk_gb", 1))
        if payload.get("was_running"):
            self.do_start(job)
        else:
            resources.set_status(row["id"], "stopped")
        audit.log_action(job["user_id"], "compute.resize", row["id"],
                         {"flavor": config.get("flavor")})

    def do_serveo_start(self, job):
        row = resources.get_any(job["resource_id"])
        if row is None:
            return
        if row["status"] != "running":
            resources.merge_config(row["id"], {"serveo": {
                **(self.config_of(row).get("serveo") or {}),
                "status": "error", "pid": None, "url": None,
                "error": "instance stopped before the tunnel could start",
            }})
            return
        self.start_serveo_bridge(row, job["user_id"])

    def do_serveo_stop(self, job):
        row = resources.get_any(job["resource_id"])
        if row is None:
            return
        self.stop_serveo_bridge(row, disable=True)
        audit.log_action(job["user_id"], "compute.serveo_stopped", row["id"])

    def do_update(self, job):
        """Start the fixed root-owned updater outside this worker service."""
        self.log(f"job {job['id']}: starting platform update service")
        update.write_runtime_status(
            "starting", "Waiting for the platform update service", job["id"]
        )
        try:
            # A oneshot unit remains in "activating" until update.sh exits.
            # --no-block confirms only that systemd accepted the request, so a
            # slow image rebuild cannot turn into a false worker timeout.
            subprocess.run(
                ["systemctl", "--no-block", "start", "homecloud-update.service"],
                check=True,
                timeout=10,
            )
        except Exception as error:
            update.write_runtime_status(
                "failed", f"Could not start the update service: {error}", job["id"]
            )
            raise
        audit.log_action(job["user_id"], "platform.update_started", None,
                         {"job_id": job["id"]})

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

    def sample_usage(self, row, config):
        """Return actual host consumption for a running Firecracker process."""
        pid = config.get("pid")
        try:
            with open(f"/proc/{int(pid)}/stat", "r", encoding="ascii") as handle:
                stat = handle.read()
            fields = stat[stat.rfind(")") + 2:].split()
            cpu_ticks = int(fields[11]) + int(fields[12])  # utime + stime

            rss_kb = 0
            with open(f"/proc/{int(pid)}/status", "r", encoding="ascii") as handle:
                for line in handle:
                    if line.startswith("VmRSS:"):
                        rss_kb = int(line.split()[1])
                        break
        except (OSError, ValueError, IndexError):
            return None

        now = time.monotonic()
        previous = self.usage_samples.get(row["id"])
        self.usage_samples[row["id"]] = (cpu_ticks, now)
        cpu_percent = 0.0
        if previous is not None and now > previous[1]:
            cpu_percent = max(0.0, (cpu_ticks - previous[0]) / os.sysconf("SC_CLK_TCK")
                              / (now - previous[1]) * 100)

        rootfs = config.get("rootfs") or os.path.join(self.vm_dir(row["id"]), "rootfs.ext4")
        return {
            "cpu_percent": round(cpu_percent, 1),
            "memory_bytes": rss_kb * 1024,
            "disk_bytes": images.disk_usage_bytes(rootfs),
            "sampled_at": int(time.time()),
        }

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
                tunnel = dict(config.get("serveo") or {})
                if tunnel.get("enabled"):
                    if serveo.is_alive(tunnel.get("pid")):
                        url = serveo.read_url(self.vm_dir(row["id"]))
                        if url:
                            if url != tunnel.get("url") or tunnel.get("status") != "running":
                                tunnel.update({"status": "running", "url": url, "error": None})
                                resources.merge_config(row["id"], {"serveo": tunnel})
                        else:
                            # Clean up stale tunnels created by older versions,
                            # which treated an alive SSH process as success even
                            # though Serveo had not provided a public URL.
                            serveo.stop(tunnel.get("pid"))
                            tunnel.update({
                                "status": "error", "pid": None, "url": None,
                                "error": "Serveo did not announce a public URL. "
                                         "Start the tunnel again to retry.",
                            })
                            resources.merge_config(row["id"], {"serveo": tunnel})
                    elif tunnel.get("status") not in ("error", "offline"):
                        tunnel.update({"status": "error", "pid": None, "url": None,
                                       "error": serveo.read_error(self.vm_dir(row["id"]))})
                        resources.merge_config(row["id"], {"serveo": tunnel})
                usage = self.sample_usage(row, config)
                if usage is not None:
                    resources.merge_config(row["id"], {"usage": usage})
                continue

            self.log(f"resource {row['id']}: process gone, marking stopped")
            self.stop_serveo_bridge(row)
            self.stop_console_bridge(row["id"])
            self.usage_samples.pop(row["id"], None)
            net.delete_tap(config.get("tap") or f"hc-vm{row['id']}")
            resources.merge_config(row["id"], {"pid": None, "usage": None})
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
