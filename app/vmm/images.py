"""Disk images for microVMs.

Two jobs:

  1. build_base_image() converts the Firecracker CI squashfs into a writable
     ext4 base image, once, at setup time. Run via
     ``flask --app app compute-build-image``.

  2. create_vm_disk() gives one VM its own copy of that base, resized to the
     requested size, with the owner's SSH key written in.

Notably none of this needs root:

  * ``mke2fs -d <dir>`` builds a filesystem image from a directory tree without
    mounting anything.
  * ``cp --sparse=always`` copies the base image cheaply -- a 20 GB image only
    occupies the blocks actually used.
  * ``debugfs -w`` writes a file into an ext4 image without mounting it, so we
    never touch loop devices and can never leak a mount.

All three are part of e2fsprogs / squashfs-tools / coreutils, i.e. base system
tools rather than Python packages.
"""

import os
import shutil
import subprocess
import tempfile

# The guest resolver. The host's own resolver is usually 127.0.0.53
# (systemd-resolved), which is unreachable from inside the VM, so a public
# resolver is written into the base image instead.
GUEST_NAMESERVERS = ("1.1.1.1", "9.9.9.9")

REQUIRED_TOOLS = ("unsquashfs", "mke2fs", "resize2fs", "debugfs")


class ImageError(Exception):
    pass


def _run(args, check=True, stdin_text=None):
    result = subprocess.run(
        args,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    if check and result.returncode != 0:
        raise ImageError(
            f"{' '.join(args[:3])}... failed ({result.returncode}): "
            f"{(result.stdout + result.stderr).strip()[:600]}"
        )
    return result


def require_tools():
    missing = [tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None]
    if missing:
        raise ImageError(
            "missing host tools: " + ", ".join(missing)
            + " (install e2fsprogs and squashfs-tools)"
        )


def build_base_image(squashfs_path, output_path, size_mb=1024, log=print):
    """Turn the CI squashfs into a writable ext4 base image.

    The result is deliberately small (1 GB by default). Each VM copies it and
    grows its own copy to the requested size, so the base stays cheap to keep
    and cheap to copy.
    """
    require_tools()
    if not os.path.exists(squashfs_path):
        raise ImageError(f"squashfs not found: {squashfs_path}")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="homecloud-rootfs-") as workdir:
        tree = os.path.join(workdir, "root")
        log(f"unpacking {os.path.basename(squashfs_path)} ...")
        _run(["unsquashfs", "-quiet", "-no-xattrs", "-dest", tree, squashfs_path])

        # Prepare the places we later write into per VM. Doing it here means the
        # per-VM step only has to write a file, never create directories.
        ssh_dir = os.path.join(tree, "root", ".ssh")
        os.makedirs(ssh_dir, exist_ok=True)
        os.chmod(ssh_dir, 0o700)
        with open(os.path.join(ssh_dir, "authorized_keys"), "w", encoding="ascii") as f:
            f.write("")  # placeholder, replaced per VM
        os.chmod(os.path.join(ssh_dir, "authorized_keys"), 0o600)

        etc = os.path.join(tree, "etc")
        os.makedirs(etc, exist_ok=True)
        with open(os.path.join(etc, "resolv.conf"), "w", encoding="ascii") as f:
            for server in GUEST_NAMESERVERS:
                f.write(f"nameserver {server}\n")

        # A marker so it is obvious inside the guest what it is running on.
        with open(os.path.join(etc, "homecloud-release"), "w", encoding="ascii") as f:
            f.write("HomeCloud microVM base image\n")

        log(f"building ext4 image ({size_mb} MB) ...")
        if os.path.exists(output_path):
            os.remove(output_path)
        _run(["mke2fs", "-q", "-t", "ext4", "-L", "homecloud", "-d", tree,
              output_path, f"{size_mb}m"])

    log(f"base image ready: {output_path}")
    return output_path


def create_vm_disk(base_image, target_path, disk_gb, ssh_public_key):
    """Give one VM its disk: sparse copy of the base, grown, key injected."""
    require_tools()
    if not os.path.exists(base_image):
        raise ImageError(
            f"base image missing: {base_image} "
            "(run: flask --app app compute-build-image)"
        )

    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    if os.path.exists(target_path):
        os.remove(target_path)

    # Sparse copy: cheap regardless of the nominal size.
    _run(["cp", "--sparse=always", base_image, target_path])

    # Grow the file, then the filesystem inside it.
    _run(["truncate", "-s", f"{int(disk_gb)}G", target_path])
    _run(["e2fsck", "-fp", target_path], check=False)  # resize2fs insists on a check
    _run(["resize2fs", target_path])

    if ssh_public_key:
        inject_ssh_key(target_path, ssh_public_key)

    return target_path


def inject_ssh_key(image_path, ssh_public_key):
    """Write authorized_keys into an unmounted ext4 image via debugfs."""
    key = ssh_public_key.strip() + "\n"

    with tempfile.NamedTemporaryFile("w", suffix=".pub", delete=False) as handle:
        handle.write(key)
        source = handle.name

    try:
        # rm first: debugfs "write" refuses to overwrite an existing file.
        script = (
            "rm /root/.ssh/authorized_keys\n"
            f"write {source} /root/.ssh/authorized_keys\n"
            "sif /root/.ssh/authorized_keys mode 0100600\n"
            "sif /root/.ssh/authorized_keys uid 0\n"
            "sif /root/.ssh/authorized_keys gid 0\n"
        )
        result = _run(["debugfs", "-w", "-f", "/dev/stdin", image_path],
                      check=False, stdin_text=script)
        combined = result.stdout + result.stderr
        # debugfs reports most problems on stdout with a zero exit code, so the
        # output has to be inspected rather than the return code.
        if "File not found" in combined and "write" in combined:
            raise ImageError(f"could not write SSH key into image: {combined[:400]}")
    finally:
        os.unlink(source)


def remove_vm_dir(vm_dir):
    """Delete a VM's directory including its disk. Never raises."""
    shutil.rmtree(vm_dir, ignore_errors=True)


def disk_usage_bytes(path):
    """Actual blocks used by a (sparse) file, not its nominal size."""
    try:
        return os.stat(path).st_blocks * 512
    except OSError:
        return 0
