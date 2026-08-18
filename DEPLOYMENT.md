# HomeCloud deployment

HomeCloud supports amd64 and arm64, including a 64-bit Raspberry Pi host.

## 1. Host prerequisite

Firecracker needs a Linux host with working /dev/kvm and /dev/net/tun. A
Raspberry Pi should run HomeCloud directly on a 64-bit OS. For Proxmox LXC, use
a privileged container and pass through those two devices; the worker preflight
reports missing prerequisites clearly.

Do not use an unprivileged LXC for Firecracker. It commonly cannot access KVM
or create TAP devices.

## 2. Install

On the empty Debian 12 or Ubuntu 24.04 host:

    git clone https://github.com/pythonIsFast/HomeCloud.git /opt/homecloud
    cd /opt/homecloud
    sudo ./install.sh

[install.sh](install.sh) installs OS packages, Firecracker, the matching
architecture-specific kernel/rootfs, systemd services, nginx, and creates the
base image. It generates /etc/homecloud/homecloud.env with a secret and a
detected outbound interface.

Open the host over HTTP, register the first account, then close registration:

    sudo sed -i 's/^HOMECLOUD_ALLOW_REGISTRATION=.*/HOMECLOUD_ALLOW_REGISTRATION=0/' \
      /etc/homecloud/homecloud.env
    sudo systemctl restart homecloud-web homecloud-vmm

Configure HTTPS before making the service public, then set
HOMECLOUD_COOKIE_SECURE=1.

## 3. Update

    sudo /opt/homecloud/update.sh

[update.sh](update.sh) pulls the tracked branch, updates dependencies, rebuilds
the base image for new VMs, and restarts HomeCloud safely. Existing VM disks are
not modified; recreate pre-terminal VMs to remove legacy SSH access and use the
browser terminal.
