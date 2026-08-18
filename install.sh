#!/usr/bin/env bash
# Install HomeCloud on a fresh Debian 12 / Ubuntu 24.04 amd64 or arm64 host.
#
# Run from a checkout:
#   git clone https://github.com/pythonIsFast/HomeCloud.git /opt/homecloud
#   cd /opt/homecloud && sudo ./install.sh

set -euo pipefail

if [ "${EUID}" -ne 0 ]; then
  echo "Run this installer as root: sudo $0" >&2
  exit 1
fi

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ARCH="$(uname -m)"
case "${ARCH}" in
  x86_64|aarch64) ;;
  *) echo "Unsupported architecture: ${ARCH} (need x86_64 or aarch64)." >&2; exit 1 ;;
esac
if ! command -v apt-get >/dev/null 2>&1; then
  echo "This installer currently supports Debian/Ubuntu hosts only." >&2
  exit 1
fi
if [ ! -f "${PROJECT_DIR}/requirements.txt" ] || [ ! -f "${PROJECT_DIR}/wsgi.py" ]; then
  echo "Run install.sh from a HomeCloud checkout." >&2
  exit 1
fi

echo "==> Installing operating-system packages"
apt-get update
apt-get install -y \
  ca-certificates curl git nginx openssl \
  python3 python3-venv python3-pip \
  iproute2 iptables e2fsprogs squashfs-tools

echo "==> Creating the HomeCloud service account"
if ! id homecloud >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /var/lib/homecloud \
    --shell /usr/sbin/nologin homecloud
fi

cd "${PROJECT_DIR}"
echo "==> Creating the Python environment"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
PIP_DISABLE_PIP_VERSION_CHECK=1 .venv/bin/pip install --upgrade pip
PIP_DISABLE_PIP_VERSION_CHECK=1 .venv/bin/pip install -r requirements.txt

echo "==> Initializing HomeCloud state"
install -d -o homecloud -g homecloud -m 2770 instance
runuser -u homecloud -- .venv/bin/flask --app app init-db
install -d -o homecloud -g homecloud -m 0750 instance/bin instance/images

echo "==> Downloading Firecracker for ${ARCH}"
RELEASE_URL=https://github.com/firecracker-microvm/firecracker/releases
LATEST="$(basename "$(curl -fsSLI -o /dev/null -w '%{url_effective}' "${RELEASE_URL}/latest")")"
curl -fsSL "${RELEASE_URL}/download/${LATEST}/firecracker-${LATEST}-${ARCH}.tgz" \
  | tar -xz
install -o root -g homecloud -m 0750 \
  "release-${LATEST}-${ARCH}/firecracker-${LATEST}-${ARCH}" \
  instance/bin/firecracker
rm -rf "release-${LATEST}-${ARCH}"

echo "==> Downloading matching kernel and rootfs"
ARTIFACTS="https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.15/${ARCH}"
curl -fL "${ARTIFACTS}/vmlinux-6.1.155" -o instance/images/vmlinux-6.1.155
curl -fL "${ARTIFACTS}/ubuntu-24.04.squashfs" -o instance/images/ubuntu-24.04.squashfs
chown homecloud:homecloud instance/images/*
chmod 0640 instance/images/*
runuser -u homecloud -- .venv/bin/flask --app app compute-build-image

echo "==> Writing service configuration"
install -d -m 0750 /etc/homecloud
ENV_FILE=/etc/homecloud/homecloud.env
if [ ! -f "${ENV_FILE}" ]; then
  VM_EGRESS_IF="${HOMECLOUD_VM_EGRESS_IF:-$(ip -4 route show default | awk '/default/ {print $5; exit}')}"
  if [ -z "${VM_EGRESS_IF}" ]; then
    echo "No default IPv4 route found; set HOMECLOUD_VM_EGRESS_IF and try again." >&2
    exit 1
  fi
  SECRET="$(openssl rand -base64 48 | tr -d '\n')"
  cat > "${ENV_FILE}" <<EOF
HOMECLOUD_SECRET_KEY=${SECRET}
HOMECLOUD_ALLOW_REGISTRATION=1
HOMECLOUD_COOKIE_SECURE=${HOMECLOUD_COOKIE_SECURE:-0}
HOMECLOUD_VM_EGRESS_IF=${VM_EGRESS_IF}
EOF
  chmod 0600 "${ENV_FILE}"
  unset SECRET
fi

cat > /etc/systemd/system/homecloud-web.service <<EOF
[Unit]
Description=HomeCloud web application
After=network.target

[Service]
User=homecloud
Group=homecloud
WorkingDirectory=${PROJECT_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${PROJECT_DIR}/.venv/bin/gunicorn --worker-class gthread --workers 2 --threads 8 --bind 127.0.0.1:6002 wsgi:app
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/homecloud-vmm.service <<EOF
[Unit]
Description=HomeCloud Firecracker VMM worker
After=network-online.target homecloud-web.service
Wants=network-online.target

[Service]
User=root
Group=homecloud
UMask=0007
KillMode=process
WorkingDirectory=${PROJECT_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${PROJECT_DIR}/.venv/bin/python -m app.vmm
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/nginx/sites-available/homecloud <<'EOF'
server {
    listen 80;
    server_name _;
    client_max_body_size 2048m;

    location / {
        proxy_pass http://127.0.0.1:6002;
        proxy_request_buffering off;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 1h;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF
ln -sfn /etc/nginx/sites-available/homecloud /etc/nginx/sites-enabled/homecloud
rm -f /etc/nginx/sites-enabled/default
nginx -t

echo "==> Starting HomeCloud"
systemctl daemon-reload
systemctl enable --now homecloud-web.service homecloud-vmm.service nginx.service
systemctl is-active --quiet homecloud-web.service
systemctl is-active --quiet homecloud-vmm.service

echo
echo "HomeCloud is installed."
echo "Open this host's HTTP address, register the first account, then set"
echo "HOMECLOUD_ALLOW_REGISTRATION=0 in ${ENV_FILE} and restart both services."
echo "Configure HTTPS and set HOMECLOUD_COOKIE_SECURE=1 before exposing it publicly."
