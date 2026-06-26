# epever-tracer-mqtt

Read an EPEVER / Tracer BN MPPT charge controller over RS-485 and publish state to MQTT, with optional Home Assistant auto-discovery and a terminal dashboard.

Designed for Raspberry Pi + Exar XR21B1411 USB RS-485 adapter + Mosquitto. Also works on other Linux hosts with a Modbus RTU adapter.

## Features

- **MQTT + Home Assistant** — auto-discovery sensors, lights control, reachability-aware availability
- **Terminal dashboard** — live sparklines, gauges, charger config, and time estimates
- **Time remaining** — LiFePO4-aware estimates using voltage trends, amp-hours, and prior-day charge/discharge logs
- **Safe coexistence** — file lock on the serial port so the MQTT daemon and dashboard can run together
- **Exar adapter setup** — one-command RS-485 mode configuration for the XR21B1411 stick

## Hardware

- **Controller:** EPEVER Tracer BN (Modbus RTU, slave address 1, 115200 8N1)
- **Adapter:** Exar XR21B1411 (`/dev/ttyACM0` via `cdc_acm`)
- **Broker:** Mosquitto (local or remote)

The Exar stick needs a one-time RS-485 mode configuration — see below.

## Quick start

```bash
git clone https://github.com/carefreeinv/epever-tracer-mqtt.git
cd epever-tracer-mqtt
pip3 install -r requirements.txt
cp .env.example .env
# edit .env with your MQTT credentials

# Configure Exar adapter (after each USB replug if udev rule not installed)
sudo python3 configure-exar-rs485.py

# Test read
python3 solar_data.py

# Run publisher (foreground)
python3 publish_mqtt.py

# Optional: full-screen dashboard (can run alongside the MQTT service)
python3 dashboard.py
```

## Install as a service (systemd + udev)

```bash
./scripts/install.sh
```

This installs the systemd unit and udev rule using your repo path and login user (not hard-coded to `pi`). Ensure that user is in the `dialout` group:

```bash
sudo usermod -aG dialout "$USER"
```

## Home Assistant

Set `MQTT_HA_DISCOVERY_PREFIX=homeassistant` in `.env`. Entities appear under device **Solar Tracer**.

| Topic | Purpose |
|-------|---------|
| `solartracer/charger_reachable` | `true` / `false` — RS-485 connectivity |
| `solartracer/battery_voltage` | Battery voltage (V) |
| `solartracer/battery_level_pct` | State of charge (%) |
| `solartracer/time_remaining` | Human-readable time estimate |
| `solartracer/time_remaining_seconds` | Numeric seconds (or empty when unknown) |
| `solartracer/lights_mode` | `off` / `auto` / `on` select |
| `solartracer/lights_on` | Lights relay switch (manual mode) |

When the charger is unreachable for three consecutive reads, retained state topics are cleared and entities go unavailable via the shared availability topic.

## Local data files

Runtime history is stored under `./var` by default (gitignored):

| File | Purpose |
|------|---------|
| `var/voltage-history.json` | 3-minute voltage trend window |
| `var/battery-rate-log.json` | Charge/discharge rate samples and prior-day rollups |

Override the directory with `SOLARTRACER_DATA_DIR` in `.env` or the environment. Legacy repo-root dotfiles are migrated into `var/` on first run.

## Project layout

| Module | Responsibility |
|--------|----------------|
| `solar_data.py` | Modbus I/O, charger config, LiFePO4 estimates, lights control |
| `publish_mqtt.py` | MQTT daemon, HA discovery, command topics |
| `dashboard.py` | Terminal UI |
| `storage.py` | Atomic JSON persistence |
| `app_paths.py` | Runtime data path resolution |
| `telemetry_store.py` | Voltage history + rate log coordination |
| `configure-exar-rs485.py` | Exar USB adapter setup |

## Dual RJ45 ports on the controller

Many Tracer units expose **two RJ45 jacks wired in parallel** on the same RS-485 bus. You can connect the MT50 to one jack and the Pi adapter to the other without a splitter cable.

## Upstream / reference

See [UPSTREAM.md](UPSTREAM.md). Modbus protocol reference PDFs live in `vendor/epsolar-tracer/archive/`.

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## License

MIT — see [LICENSE](LICENSE).