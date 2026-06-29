# Home Assistant

Set `MQTT_HA_DISCOVERY_PREFIX=homeassistant` in `.env`. Entities appear under device **Solar Tracer**.

## Key MQTT topics

| Topic | Purpose |
|-------|---------|
| `solartracer/charger_reachable` | `true` / `false` — RS-485 connectivity |
| `solartracer/battery_voltage` | Battery voltage (V) |
| `solartracer/battery_level_pct` | State of charge (%) |
| `solartracer/time_remaining` | Human-readable time estimate |
| `solartracer/time_remaining_seconds` | Numeric seconds (empty when unknown) |
| `solartracer/lights_mode` | `off` / `auto` / `on` select |
| `solartracer/lights_on` | Lights relay switch (manual mode) |

Command topics:

| Topic | Purpose |
|-------|---------|
| `solartracer/lights_mode/set` | Set lights mode (`off`, `auto`, `on`) |
| `solartracer/lights_on/set` | Toggle relay in manual mode (`on` / `off`) |

## Availability

All discovered entities share an availability topic tied to `charger_reachable`. When the charger is unreachable for three consecutive reads, retained state topics are cleared and entities go unavailable.

## Auto-discovered entities

The publisher registers sensors for PV/battery/load readings, temperatures, daily energy totals, charger configuration setpoints, binary sensors (`is_charging`, `is_night`, `charger_reachable`), and controls for lights.

Discovery messages are retained and refreshed on an interval (`DISCOVERY_REFRESH_SEC`, default 1 hour) so new HA entities pick up config after broker restarts.