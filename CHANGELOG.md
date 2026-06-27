# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-06-27

### Added
- `serial_device.py` — auto-detect the Exar XR21B1411 adapter, prefer the udev symlink, and rescan after USB replug
- udev rule: stable `/dev/solartracer-rs485` symlink, `dialout` permissions, and restart `solartracer-mqtt.service` on replug
- Charger holding-register configuration published to MQTT and Home Assistant discovery
- Dashboard MQTT subscriber (`MqttStateCache`) with persistent connection; lights control via `lights_mode/set`
- Dashboard fallback to local history artifacts when MQTT is unavailable
- `close_instrument()` to release the tty after each Modbus session
- `lights_relay_engaged()` and `lights_auto_label()` for accurate auto-mode relay display
- Configurable env vars: `VOLTAGE_SAMPLE_INTERVAL`, `CONFIG_REFRESH_SEC`, `UNREACHABLE_POLL_INTERVAL`, `MODBUS_INTER_READ_DELAY_SEC`, `DISCOVERY_REFRESH_SEC`

### Changed
- Default `SERIAL_DEVICE=auto` (was `/dev/ttyACM0`); publisher resolves the Exar port on each start and after failures
- Default `PUBLISH_INTERVAL` 60s (was 30s); voltage sampling defaults to the publish interval unless overridden
- Dashboard reads live state from MQTT only — the publisher owns RS-485 (no serial lock contention)
- MQTT publisher slows polling when the charger is unreachable (`UNREACHABLE_POLL_INTERVAL`)
- HA discovery refreshed on an interval instead of every state publish
- Availability topic refreshed on every successful read while reachable (clears stale retained `false`)
- Modbus inter-read delay between register reads (default 50 ms) for shared RS-485 buses
- Auto-mode `lights_on` derived from load current and day/night thresholds instead of a single register read

### Fixed
- USB replug recovery when `ttyACM` number changes (symlink target tracking + periodic rescan)
- Stale serial file handles preventing reconnect after adapter disconnect
- Dashboard and MQTT daemon no longer compete for the RS-485 port

## [0.2.0] - 2026-06-26

### Added
- `dashboard.py` — full-screen terminal dashboard with sparklines and gauges
- `storage.py`, `app_paths.py`, and `telemetry_store.py` for clearer persistence boundaries
- `SOLARTRACER_DATA_DIR` environment variable to override local history file location
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
- Install script substitutes repo path and service user instead of hard-coding `pi`

### Changed
- `solar_data.py` expanded with shared telemetry, status, and estimate logic
- MQTT publisher and dashboard share `TelemetryStore` for voltage/rate history
- Atomic JSON writes consolidated in `storage.atomic_write_json`
- Runtime history files moved to `./var/` (auto-migrates legacy repo-root dotfiles)
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