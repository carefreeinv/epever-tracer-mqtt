# Installation

## systemd service and udev rule

The install script substitutes your repo path and login user into the service and udev templates:

```bash
./scripts/install.sh
```

This will:

1. Install `/etc/udev/rules.d/99-exar-rs485.rules`
2. Install `/etc/systemd/system/solartracer-mqtt.service`
3. Enable and start `solartracer-mqtt.service`

!!! warning "Do not copy the systemd template directly"
    `systemd/solartracer-mqtt.service` contains `@REPO_DIR@` and `@INSTALL_USER@` placeholders. Always run `scripts/install.sh` so systemd receives valid paths.

## Serial port permissions

Ensure the service user is in the `dialout` group:

```bash
sudo usermod -aG dialout "$USER"
# log out and back in, or reboot
```

## Stable device symlink

The udev rule creates `/dev/solartracer-rs485` pointing at the Exar `ttyACM` node. Set `SERIAL_DEVICE=auto` in `.env` (the default) so the publisher finds the adapter after USB replugs even when the `ttyACM` number changes.

On USB plug the udev rule also:

- Runs `configure-exar-rs485.py` to restore RS-485 mode
- Requests a restart of `solartracer-mqtt.service` so stale serial handles are dropped

## Service management

```bash
sudo systemctl status solartracer-mqtt.service
sudo journalctl -u solartracer-mqtt.service -f
sudo systemctl restart solartracer-mqtt.service
```

The service loads environment variables from `.env` in the repo root via `EnvironmentFile`.

## Local data directory

Runtime history files are stored under `./var` by default (gitignored):

| File | Purpose |
|------|---------|
| `var/voltage-history.json` | 3-minute voltage trend window |
| `var/battery-rate-log.json` | Charge/discharge rate samples and prior-day rollups |

Override with `SOLARTRACER_DATA_DIR` in `.env`. Legacy repo-root dotfiles are migrated into `var/` on first run.