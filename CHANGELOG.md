# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `storage.py`, `app_paths.py`, and `telemetry_store.py` for clearer persistence boundaries
- `SOLARTRACER_DATA_DIR` environment variable to override local history file location
- Install script substitutes repo path and service user instead of hard-coding `pi`

### Changed
- MQTT publisher and dashboard share `TelemetryStore` for voltage/rate history
- Atomic JSON writes consolidated in `storage.atomic_write_json`
- Runtime history files moved to `./var/` (auto-migrates legacy repo-root dotfiles)

## [0.2.0] - 2026-06-26

### Added
- `dashboard.py` — full-screen terminal dashboard with sparklines and gauges
- Time-to-full / time-to-empty estimates with LiFePO4 OCV curve and plateau handling
- Empirical charge/discharge rates from 3/10/30/60 minute lookbacks
- Prior-day charge/discharge rate grounding via `.battery-rate-log.json`
- Home Assistant entities: `time_remaining`, `time_remaining_seconds`, `battery_level_pct`
- Fast 5-second voltage sampling for trend windows while MQTT publishes on interval
- RS-485 `serial_lock()` so MQTT daemon and dashboard can coexist safely
- Debounced charger unreachable handling (3 failures before clearing MQTT states)
- Warmup countdown and descriptive fallback labels when estimates are not ready
- External charging detection and estimate path (voltage rising, low Tracer amps)
- Lights mode select (`off` / `auto` / `on`) plus legacy `lights_on` switch

### Changed
- `solar_data.py` expanded with shared telemetry, status, and estimate logic
- MQTT publisher runs as a long-lived daemon with HA command topic subscriptions
- Reachability binary sensor drives HA entity availability

## [0.1.0] - 2026-06-25

### Added
- Initial release: EPEVER Tracer BN Modbus reader over RS-485
- MQTT publisher with Home Assistant auto-discovery
- Charger reachability signaling and retained state clearing on disconnect
- Lights relay control via MQTT (`lights_on`)
- Exar XR21B1411 USB RS-485 configuration script
- systemd service, udev rule, and `scripts/install.sh`
- Reference submodule `vendor/epsolar-tracer` (documentation only)