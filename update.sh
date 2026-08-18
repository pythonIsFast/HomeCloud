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

