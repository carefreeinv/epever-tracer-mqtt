#!/usr/bin/env bash
# Install systemd service and udev rule for epever-tracer-mqtt.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
UDEV_RULE_DST="/etc/udev/rules.d/99-exar-rs485.rules"
SERVICE_DST="/etc/systemd/system/solartracer-mqtt.service"

echo "Repo: ${REPO_DIR}"

# Patch paths in udev rule if repo is not at the default location
sed "s|/home/pi/epever-tracer-mqtt|${REPO_DIR}|g" \
    "${REPO_DIR}/udev/99-exar-rs485.rules" | sudo tee "${UDEV_RULE_DST}" >/dev/null

# Patch paths in systemd unit
sed "s|/home/pi/epever-tracer-mqtt|${REPO_DIR}|g" \
    "${REPO_DIR}/systemd/solartracer-mqtt.service" | sudo tee "${SERVICE_DST}" >/dev/null

sudo udevadm control --reload-rules
sudo systemctl daemon-reload
sudo systemctl enable solartracer-mqtt.service
sudo systemctl restart solartracer-mqtt.service

echo "Installed. Status:"
systemctl --no-pager status solartracer-mqtt.service | head -10