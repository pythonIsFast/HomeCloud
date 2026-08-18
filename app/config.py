"""Application configuration.

Kept deliberately small: values come from environment variables with sane
defaults for local development. No python-dotenv (dependency policy) -- export
the variables in the systemd unit / gunicorn wrapper on the LXC container.
"""

import os
import secrets


def load_secret_key(instance_path):
    """Return the HMAC/session secret.

    Priority:
      1. HOMECLOUD_SECRET_KEY environment variable (preferred in production).
      2. instance/secret_key file, generated on first start.

    Generating and persisting the key ourselves means JWTs survive a restart
    without the operator having to configure anything for a local test run.
    """
    from_env = os.environ.get("HOMECLOUD_SECRET_KEY")
    if from_env:
        return from_env

    key_file = os.path.join(instance_path, "secret_key")
    if os.path.exists(key_file):
        with open(key_file, "r", encoding="utf-8") as f:
            stored = f.read().strip()
        if stored:
            return stored

    generated = secrets.token_urlsafe(48)
    # 0o600: only the service user may read the key.
    with open(key_file, "w", encoding="utf-8") as f:
        f.write(generated)
    os.chmod(key_file, 0o600)
    return generated


def build_config(instance_path):
    """Return the config dict handed to Flask's app.config.from_mapping()."""
    return {
        "APP_NAME": "HomeCloud",
        "SECRET_KEY": load_secret_key(instance_path),
        "DATABASE": os.path.join(instance_path, "homecloud.db"),
        # Lifetime of an access token in seconds (default 12 hours).
        "JWT_TTL_SECONDS": int(os.environ.get("HOMECLOUD_JWT_TTL", 12 * 3600)),
        # Name of the HttpOnly cookie that carries the JWT for browser sessions.
        "JWT_COOKIE_NAME": "homecloud_token",
        # Set to 1 once nginx terminates TLS -> cookie is only sent over HTTPS.
        "JWT_COOKIE_SECURE": os.environ.get("HOMECLOUD_COOKIE_SECURE", "0") == "1",
        # Allow self-service registration. Turn off after creating your accounts.
        "ALLOW_REGISTRATION": os.environ.get("HOMECLOUD_ALLOW_REGISTRATION", "1") == "1",

        # --- compute service / Firecracker -------------------------------
        # Binaries and images live under instance/ because they are host state,
        # not source code, and instance/ is gitignored.
        "FIRECRACKER_BIN": os.environ.get(
            "HOMECLOUD_FIRECRACKER_BIN", os.path.join(instance_path, "bin", "firecracker")
        ),
        "VM_DIR": os.path.join(instance_path, "vms"),
        "IMAGE_DIR": os.path.join(instance_path, "images"),
        "VM_KERNEL": os.environ.get(
            "HOMECLOUD_VM_KERNEL",
            os.path.join(instance_path, "images", "vmlinux-6.1.155"),
        ),
        # Writable ext4 built once from the CI squashfs; see vmm/images.py.
        "VM_BASE_ROOTFS": os.environ.get(
            "HOMECLOUD_VM_BASE_ROOTFS",
            os.path.join(instance_path, "images", "base-ubuntu-24.04.ext4"),
        ),
        "VM_BASE_SQUASHFS": os.environ.get(
            "HOMECLOUD_VM_BASE_SQUASHFS",
            os.path.join(instance_path, "images", "ubuntu-24.04.squashfs"),
        ),
        # First two octets of the VM range. Each VM gets a /30 derived from its
        # resource id, so 10.71.0.0/16 holds 16383 VMs without any lease table.
        "VM_SUBNET_PREFIX": os.environ.get("HOMECLOUD_VM_SUBNET_PREFIX", "10.71"),
        # Interface the NAT rule masquerades to. Empty = detect the default route.
        "VM_EGRESS_IF": os.environ.get("HOMECLOUD_VM_EGRESS_IF", ""),
    }
