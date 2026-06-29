# SolarTracer MQTT

Read an **EPEVER / Tracer BN** MPPT charge controller over RS-485 and publish state to MQTT, with optional Home Assistant auto-discovery and a terminal dashboard.

Designed for **Raspberry Pi** + **Exar XR21B1411** USB RS-485 adapter + **Mosquitto**. Also works on other Linux hosts with a Modbus RTU adapter.

## Features

- **MQTT + Home Assistant** — auto-discovery sensors, lights control, reachability-aware availability
- **Terminal dashboard** — live sparklines, gauges, charger config, and time estimates
- **Time remaining** — LiFePO4-aware estimates using voltage trends, amp-hours, and prior-day charge/discharge logs
- **Serial recovery** — automatic USB/RS-485 recovery after adapter glitches or replugs
- **Exar adapter setup** — one-command RS-485 mode configuration for the XR21B1411 stick

## Hardware

| Component | Details |
|-----------|---------|
| Controller | EPEVER Tracer BN (Modbus RTU, slave address 1, 115200 8N1) |
| Adapter | Exar XR21B1411 (`04e2:1411`, `/dev/ttyACM0` via `cdc_acm`) |
| Broker | Mosquitto (local or remote) |

!!! note "Dual RJ45 ports"
    Many Tracer units expose **two RJ45 jacks wired in parallel** on the same RS-485 bus. You can connect the MT50 to one jack and the Pi adapter to the other without a splitter cable.

## Project layout

| Module | Responsibility |
|--------|----------------|
| `solar_data.py` | Modbus I/O, charger config, LiFePO4 estimates, lights control |
| `publish_mqtt.py` | MQTT daemon, HA discovery, command topics |
| `serial_device.py` | Exar port detection, USB recovery |
| `dashboard.py` | Terminal UI |
| `telemetry_store.py` | Voltage history + rate log coordination |
| `configure-exar-rs485.py` | Exar USB adapter setup |

## License

MIT — see [LICENSE](https://github.com/carefreeinv/epever-tracer-mqtt/blob/main/LICENSE).