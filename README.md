# epever-tracer-mqtt

Read an EPEVER / Tracer BN MPPT charge controller over RS-485 and publish state to MQTT, with optional Home Assistant auto-discovery.

Designed for Raspberry Pi + Exar XR21B1411 USB RS-485 adapter + Mosquitto.

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
```

## Install on Raspberry Pi (systemd + udev)

```bash
./scripts/install.sh
```

This installs the systemd service and udev rule for the Exar adapter. Ensure user `pi` is in the `dialout` group.

## Home Assistant

Set `MQTT_HA_DISCOVERY_PREFIX=homeassistant` in `.env`. Entities appear under device **Solar Tracer**.

Notable topics:

| Topic | Purpose |
|-------|---------|
| `solartracer/charger_reachable` | `true` / `false` — RS-485 connectivity |
| `solartracer/battery_voltage` | Battery voltage (V) |
| `solartracer/lights_on` | Lights relay switch (manual load mode) |

When the charger is unreachable, all state topics are cleared and entities go unavailable via the shared availability topic.

## Dual RJ45 ports on the controller

Many Tracer units expose **two RJ45 jacks wired in parallel** on the same RS-485 bus. You can connect the MT50 to one jack and the Pi adapter to the other without a splitter cable.

## Upstream / reference

See [UPSTREAM.md](UPSTREAM.md). Modbus protocol reference PDFs live in `vendor/epsolar-tracer/archive/`.

## License

MIT — see [LICENSE](LICENSE).