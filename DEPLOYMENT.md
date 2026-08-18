# HomeCloud on an empty LXC or Raspberry Pi host

This is a production-shaped setup for either:

- an **amd64 or arm64** Debian 12/Ubuntu 24.04 LXC on a Proxmox host; or
- a **64-bit Raspberry Pi host** running Raspberry Pi OS or Ubuntu directly.

HomeCloud runs the web app as an unprivileged `homecloud` user and the VMM
worker as root because the worker creates TAP devices and NAT rules.

## Important security boundary

HomeCloud runs untrusted microVM workloads. Firecracker needs `/dev/kvm` and
the worker needs `CAP_NET_ADMIN`. The LXC variant is therefore **privileged**
and has a relaxed AppArmor profile. Do not use it for mutually untrusted
tenants and do not co-locate unrelated sensitive workloads in it. Use a
dedicated Proxmox node or, preferably, a small VM instead if that boundary is
not acceptable.

An unprivileged LXC is deliberately not the supported target: device ownership
mapping and LXC confinement commonly prevent usable access to `/dev/kvm` and
`/dev/net/tun`. The preflight checks in HomeCloud will make a missing device or
host tool explicit.

## 1. Prepare the host

### 1.1 Proxmox LXC

Create a privileged Debian 12 or Ubuntu 24.04 container with a static/DHCP
network interface, enough disk for VM images, and nested containers enabled.
The following example uses CT ID `123`; replace it with yours. Stop the
container before editing its configuration:

```bash
pct stop 123
editor /etc/pve/lxc/123.conf
```

Keep the ordinary `pct` settings and add these lines:

```ini
features: nesting=1,keyctl=1
lxc.apparmor.profile: unconfined
lxc.cgroup2.devices.allow: c 10:232 rwm
lxc.mount.entry: /dev/kvm dev/kvm none bind,create=file 0 0
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file 0 0
```

`10:232` is `/dev/kvm`; `10:200` is `/dev/net/tun`. Start and enter the
container, then confirm that the two devices are usable:

```bash
pct start 123
pct enter 123
test -r /dev/kvm && test -w /dev/kvm && echo 'KVM is available'
test -r /dev/net/tun && test -w /dev/net/tun && echo 'TUN is available'
```

If either command fails, stop here and correct the LXC configuration. Do not
work around this by making broad device mounts available to the container.

### 1.2 Raspberry Pi as a bare-metal server

For a Raspberry Pi, use a **64-bit** OS and run HomeCloud directly on the Pi;
this avoids the additional LXC privilege relaxation. A Pi 4 or Pi 5 with ample
RAM and SSD-backed storage is the practical minimum. The important requirement
is hardware virtualization, not the board name: the host must report `aarch64`
and expose a read/write `/dev/kvm`.

```bash
uname -m                    # must print aarch64
sudo modprobe kvm
test -r /dev/kvm && test -w /dev/kvm && echo 'KVM is available'
test -r /dev/net/tun && test -w /dev/net/tun && echo 'TUN is available'
```

Do not continue if KVM is unavailable. Firecracker uses KVM; it does not
emulate x86 guests on ARM. ARM hosts must use the `aarch64` Firecracker binary,
kernel and rootfs, and every microVM must use the same architecture as its
host.

## 2. Install the host packages

Run the rest of this guide as root **inside the LXC**:

```bash
apt update
apt install -y \
  ca-certificates curl git nginx openssl \
  python3 python3-venv python3-pip \
  iproute2 iptables e2fsprogs squashfs-tools
```

`iproute2` and `iptables` provide TAP/NAT networking. `e2fsprogs` and
`squashfs-tools` convert the Firecracker root filesystem into HomeCloud's
writable base image.

## 3. Install HomeCloud

```bash
useradd --system --create-home --home-dir /var/lib/homecloud \
  --shell /usr/sbin/nologin homecloud

git clone https://github.com/pythonIsFast/HomeCloud.git /opt/homecloud
cd /opt/homecloud

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

install -d -o homecloud -g homecloud -m 2770 instance
runuser -u homecloud -- .venv/bin/flask --app app init-db
```

The set-group-ID mode (`2770`) matters: the root worker and the unprivileged
web service share the SQLite database directory. It makes new SQLite WAL/SHM
files group-writable instead of leaving the web service unable to write after a
worker restart.

## 4. Install Firecracker and the base image

The project uses Firecracker CI artifacts named `vmlinux-6.1.155` and
`ubuntu-24.04.squashfs`. The download uses the architecture reported by the
host, so it works for both `x86_64` and `aarch64`. Download the kernel and
rootfs as a matching pair and keep these local file names, so the project
defaults resolve without extra configuration:

```bash
cd /opt/homecloud
install -d -o homecloud -g homecloud -m 0750 instance/bin instance/images

ARCH=$(uname -m)
case "$ARCH" in x86_64|aarch64) ;; *) echo "unsupported architecture: $ARCH"; exit 1;; esac
RELEASE_URL=https://github.com/firecracker-microvm/firecracker/releases
LATEST=$(basename "$(curl -fsSLI -o /dev/null -w '%{url_effective}' "${RELEASE_URL}/latest")")
curl -fsSL "${RELEASE_URL}/download/${LATEST}/firecracker-${LATEST}-${ARCH}.tgz" \
  | tar -xz
install -o root -g homecloud -m 0750 \
  "release-${LATEST}-${ARCH}/firecracker-${LATEST}-${ARCH}" \
  instance/bin/firecracker
rm -rf "release-${LATEST}-${ARCH}"

ARTIFACTS=https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.15/${ARCH}
curl -fL "${ARTIFACTS}/vmlinux-6.1.155" \
  -o instance/images/vmlinux-6.1.155
curl -fL "${ARTIFACTS}/ubuntu-24.04.squashfs" \
  -o instance/images/ubuntu-24.04.squashfs
chown homecloud:homecloud instance/images/*
chmod 0640 instance/images/*

runuser -u homecloud -- .venv/bin/flask --app app compute-build-image
runuser -u homecloud -- .venv/bin/flask --app app show-config
```

Pin and checksum these artifacts before a long-lived production deployment.
The Firecracker project documents its current release download and CI-artifact
workflow in its [getting-started guide](https://github.com/firecracker-microvm/firecracker/blob/main/docs/getting-started.md).
The worker does not currently invoke `jailer`; downloading it is unnecessary.
On ARM, the kernel content must be an aarch64 `Image`-format kernel even though
this project stores it under the neutral `vmlinux-6.1.155` file name. Firecracker
documents the architecture-specific kernel formats in its
[rootfs/kernel guide](https://github.com/firecracker-microvm/firecracker/blob/main/docs/rootfs-and-kernel-setup.md).

## 5. Configure environment and systemd

Find the LXC's outbound interface, then put its name in the environment file.
It is usually `eth0`, but do not guess:

```bash
ip -4 route show default
install -d -m 0750 /etc/homecloud
SECRET=$(openssl rand -base64 48 | tr -d '\n')
cat > /etc/homecloud/homecloud.env <<EOF
HOMECLOUD_SECRET_KEY=${SECRET}
HOMECLOUD_ALLOW_REGISTRATION=1
HOMECLOUD_COOKIE_SECURE=0
HOMECLOUD_VM_EGRESS_IF=eth0
EOF
chmod 0600 /etc/homecloud/homecloud.env
unset SECRET
```

Set `HOMECLOUD_COOKIE_SECURE=1` as soon as nginx terminates TLS. Set
`HOMECLOUD_ALLOW_REGISTRATION=0` after creating the required accounts.

Create the web-service unit:

```bash
cat > /etc/systemd/system/homecloud-web.service <<'EOF'
[Unit]
Description=HomeCloud web application
After=network.target

[Service]
User=homecloud
Group=homecloud
WorkingDirectory=/opt/homecloud
EnvironmentFile=/etc/homecloud/homecloud.env
ExecStart=/opt/homecloud/.venv/bin/gunicorn --workers 2 --bind 127.0.0.1:6002 wsgi:app
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
```

Create the privileged worker. Its group and umask are intentional: they keep
SQLite's side files accessible to the web service.

```bash
cat > /etc/systemd/system/homecloud-vmm.service <<'EOF'
[Unit]
Description=HomeCloud Firecracker VMM worker
After=network-online.target homecloud-web.service
Wants=network-online.target

[Service]
User=root
Group=homecloud
UMask=0007
WorkingDirectory=/opt/homecloud
EnvironmentFile=/etc/homecloud/homecloud.env
ExecStart=/opt/homecloud/.venv/bin/python -m app.vmm
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
```

Put nginx in front of Gunicorn:

```bash
cat > /etc/nginx/sites-available/homecloud <<'EOF'
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:6002;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF
ln -s /etc/nginx/sites-available/homecloud /etc/nginx/sites-enabled/homecloud
rm -f /etc/nginx/sites-enabled/default
nginx -t

systemctl daemon-reload
systemctl enable --now homecloud-web homecloud-vmm nginx
```

## 6. Verify and operate

```bash
systemctl --no-pager --full status homecloud-web homecloud-vmm nginx
curl -fsS http://127.0.0.1:6002/healthz
journalctl -u homecloud-vmm -n 100 --no-pager
```

The VMM worker should log `NAT ready` and `worker up`. A missing KVM device,
kernel, base image, or host package is reported during its preflight. Open the
LXC address in a browser, register the first account (it becomes admin), then
set `HOMECLOUD_ALLOW_REGISTRATION=0` and restart both HomeCloud services:

```bash
sed -i 's/^HOMECLOUD_ALLOW_REGISTRATION=.*/HOMECLOUD_ALLOW_REGISTRATION=0/' \
  /etc/homecloud/homecloud.env
systemctl restart homecloud-web homecloud-vmm
```

For updates, use the repository workflow in `CLAUDE.md`: pull with rebase,
commit the intended change, and push only after confirming the remote.
