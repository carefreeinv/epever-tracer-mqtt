# Configuration

Copy `.env.example` to `.env` and edit for your environment. All settings can also be passed as environment variables.

## Serial / RS-485

| Variable | Default | Description |
|----------|---------|-------------|
| `SERIAL_DEVICE` | `auto` | RS-485 device path, or `auto` to detect the Exar adapter |
| `MODBUS_INTER_READ_DELAY_SEC` | `0.05` | Pause between Modbus transactions (seconds) |
| `SOLARTRACER_DEBUG` | *(off)* | Verbose RS-485 and recovery logging (`1` / `true`) |

## MQTT broker

| Variable | Default | Description |
|----------|---------|-------------|
| `MQTT_HOST` | `localhost` | Broker hostname |
| `MQTT_PORT` | `1883` | Broker port |
| `MQTT_USERNAME` | *(empty)* | Optional username |
| `MQTT_PASSWORD` | *(empty)* | Optional password |
| `MQTT_CLIENT_ID` | `solartracer` | MQTT client ID |
| `MQTT_BASE_TOPIC` | `solartracer` | Prefix for all state topics |

## Home Assistant discovery

| Variable | Default | Description |
|----------|---------|-------------|
| `MQTT_HA_DISCOVERY_PREFIX` | `homeassistant` | Discovery prefix; set empty to disable |
| `DEVICE_NAME` | `Solar Tracer` | Friendly name in HA |
| `DEVICE_ID` | `solartracer_pi` | Unique device ID |
| `MANUFACTURER` | `EPEVER` | Manufacturer string |
| `MODEL` | `Tracer BN Series` | Model string |

## Polling intervals

| Variable | Default | Description |
|----------|---------|-------------|
| `PUBLISH_INTERVAL` | `60` | Full read + MQTT publish interval (seconds) |
| `VOLTAGE_SAMPLE_INTERVAL` | `PUBLISH_INTERVAL` | Lightweight voltage samples between full reads |
| `CONFIG_REFRESH_SEC` | `300` | Holding-register config refresh interval |
| `DISCOVERY_REFRESH_SEC` | `3600` | HA discovery message refresh interval |

## Serial recovery

When the charger stops responding, the publisher polls faster and attempts recovery:

| Variable | Default | Description |
|----------|---------|-------------|
| `SERIAL_RECOVERY_POLL_INTERVAL` | `10` | Poll interval while recovering (seconds) |
| `SERIAL_RECOVERY_MAX_ATTEMPTS` | `3` | Modbus read retries per poll cycle |
| `SERIAL_RECOVERY_COOLDOWN_SEC` | `15` | Minimum gap between full USB recovery attempts |

Full USB/Exar recovery (reset + reconfigure) runs only after **3 consecutive unreachable reads**. Earlier failures only rescan the tty path.

## Data storage

| Variable | Default | Description |
|----------|---------|-------------|
| `SOLARTRACER_DATA_DIR` | `./var` | Directory for voltage history and rate logs |

## Testing

| Variable | Description |
|----------|-------------|
| `DRY_RUN=1` | Print states without connecting to MQTT |