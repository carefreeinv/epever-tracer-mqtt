#!/usr/bin/env bash
# Install systemd service and udev rule for epever-tracer-mqtt.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_USER="${SUDO_USER:-${USER:-pi}}"
UDEV_RULE_DST="/etc/udev/rules.d/99-exar-rs485.rules"
SERVICE_DST="/etc/systemd/system/solartracer-mqtt.service"

echo "Repo: ${REPO_DIR}"
echo "Service user: ${INSTALL_USER}"

patch_template() {
    sed \
        -e "s|@REPO_DIR@|${REPO_DIR}|g" \
        -e "s|@INSTALL_USER@|${INSTALL_USER}|g"
}

patch_template < "${REPO_DIR}/udev/99-exar-rs485.rules" | sudo tee "${UDEV_RULE_DST}" >/dev/null
patch_template < "${REPO_DIR}/systemd/solartracer-mqtt.service" | sudo tee "${SERVICE_DST}" >/dev/null

sudo udevadm control --reload-rules
sudo systemctl daemon-reload
sudo systemctl enable solartracer-mqtt.service
sudo systemctl restart solartracer-mqtt.service

echo "Installed. Status:"
systemctl --no-pager status solartracer-mqtt.service | head -10
echo ""
echo "Ensure ${INSTALL_USER} is in the dialout group:"
echo "  sudo usermod -aG dialout ${INSTALL_USER}"