# Upstream projects

This repository is a fresh project for Raspberry Pi + Home Assistant integration.
It was assembled from prior clones on the Pi; it does not continue their git history.

## Runtime code (independent)

The MQTT publisher and Modbus reader in this repo use **minimalmodbus** directly.
They do not import or require the upstream Python libraries at runtime.

## Reference submodule: `vendor/epsolar-tracer`

| Field | Value |
|-------|--------|
| URL | https://github.com/kasbert/epsolar-tracer |
| Author | Jarkko Sonninen |
| Use here | Modbus register definitions, protocol PDFs, Exar driver reference |

Pinned as a git submodule for documentation and register lookup only.

## Prior Pi clones (not dependencies)

| Path (historical) | URL | Notes |
|-------------------|-----|-------|
| `/home/pi/solartracer-485` | https://github.com/buba447/solartracer-485 | 2018 Dashing/MySQL project; MQTT layer was added locally and never committed upstream |
| `/home/pi/epsolar-tracer` | https://github.com/kasbert/epsolar-tracer | Unmodified clone; superseded by `vendor/epsolar-tracer` submodule here |

## What lives only in this repo

- `publish_mqtt.py` — MQTT + Home Assistant discovery, reachability, lights control
- `solar_data.py` — Tracer BN Modbus reader (minimalmodbus)
- `configure-exar-rs485.py` — Exar XR21B1411 USB RS-485 setup
- `systemd/`, `udev/`, `scripts/install.sh`