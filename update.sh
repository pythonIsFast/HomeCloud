#!/usr/bin/env bash
# Update a deployed HomeCloud installation in place.
#
# Run as root from any directory:
#   sudo /opt/homecloud/update.sh
#
# The script never deletes existing microVM disks. It rebuilds the base image
# for newly created VMs, then restarts the web process and worker.

set -euo pipefail

if [ "${EUID}" -ne 0 ]; then
  echo "Run this updater as root: sudo $0" >&2
  exit 1
fi

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV="${PROJECT_DIR}/.venv"

if [ ! -x "${VENV}/bin/pip" ] || [ ! -x "${VENV}/bin/flask" ]; then
  echo "Virtual environment missing at ${VENV}; follow DEPLOYMENT.md first." >&2
  exit 1
fi
if ! id homecloud >/dev/null 2>&1; then
  echo "System user 'homecloud' is missing; follow DEPLOYMENT.md first." >&2
  exit 1
fi

cd "${PROJECT_DIR}"

echo "==> Updating source"
git pull --rebase

echo "==> Updating Python dependencies"
PIP_DISABLE_PIP_VERSION_CHECK=1 "${VENV}/bin/pip" install -r requirements.txt

echo "==> Rebuilding the base image for new instances"
runuser -u homecloud -- "${VENV}/bin/flask" --app app compute-build-image

echo "==> Installing a safe worker-restart override"
install -d -m 0755 /etc/systemd/system/homecloud-vmm.service.d
cat > /etc/systemd/system/homecloud-vmm.service.d/worker.conf <<'EOF'
[Service]
# Firecracker VMs use their own session and must survive a worker update.
KillMode=process
EOF

echo "==> Installing the platform update service"
cat > /etc/systemd/system/homecloud-update.service <<EOF
[Unit]
Description=HomeCloud platform updater
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=root
WorkingDirectory=${PROJECT_DIR}
EnvironmentFile=/etc/homecloud/homecloud.env
ExecStart=${PROJECT_DIR}/update.sh
EOF

echo "==> Enabling concurrent terminal streams"
install -d -m 0755 /etc/systemd/system/homecloud-web.service.d
cat > /etc/systemd/system/homecloud-web.service.d/streaming.conf <<EOF
[Service]
ExecStart=
ExecStart=${PROJECT_DIR}/.venv/bin/gunicorn --worker-class gthread --workers 2 --threads 8 --bind 127.0.0.1:6002 wsgi:app
EOF
cat > /etc/nginx/conf.d/homecloud-streaming.conf <<'EOF'
# SSE terminal connections must reach the browser immediately and stay open.
client_max_body_size 2048m;
proxy_request_buffering off;
proxy_buffering off;
proxy_cache off;
proxy_read_timeout 1h;
EOF

echo "==> Restarting HomeCloud services"
systemctl daemon-reload
systemctl restart homecloud-web.service
systemctl restart homecloud-vmm.service
systemctl try-reload-or-restart nginx.service

systemctl is-active --quiet homecloud-web.service
systemctl is-active --quiet homecloud-vmm.service

echo
echo "HomeCloud was updated successfully."
echo "The rebuilt base image applies to new instances only."
echo "Recreate older VMs to remove legacy SSH access and gain the web terminal."
