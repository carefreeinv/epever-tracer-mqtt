#!/usr/bin/env python3
"""
publish_mqtt.py

Reads data from the EPEVER/Tracer BN solar charger and publishes it to MQTT.

Configuration is loaded from a .env file (see .env.example).

Usage:
    pip3 install paho-mqtt python-dotenv minimalmodbus pyserial
    cp .env.example .env
    # edit .env with your broker
    python3 publish_mqtt.py

Test without a broker:
    DRY_RUN=1 python3 publish_mqtt.py

Recommended: run via cron every 1-5 minutes.

It can optionally publish Home Assistant MQTT discovery messages.
"""

import json
import os
import sys
import time
from typing import Any, Dict, Optional

try:
    from dotenv import load_dotenv
    import paho.mqtt.client as mqtt
    from paho.mqtt.client import CallbackAPIVersion
except ImportError as e:
    print("Missing dependencies. Run:")
    print("  pip3 install paho-mqtt python-dotenv minimalmodbus pyserial")
    sys.exit(1)

# Local module
try:
    from solar_data import (
        CONFIG_MQTT_KEYS,
        LIGHTS_STATES,
        compute_battery_level_pct,
        config_has_values,
        display_time_remaining,
        get_lights_state,
        get_solar_data,
        is_charger_reachable,
        read_battery_voltage,
        set_lights_enabled,
        set_lights_state,
    )
    from serial_device import device_realpath, rescan_serial_device, resolve_serial_device
    from telemetry_store import TelemetryStore
except ImportError:
    print("solar_data.py must be in the same directory.")
    sys.exit(1)


def load_config() -> Dict[str, Any]:
    """Load configuration from .env (or environment variables)."""
    load_dotenv()

    serial_hint = os.getenv("SERIAL_DEVICE")
    serial_device = resolve_serial_device(serial_hint)

    cfg = {
        "serial_device": serial_device,
        "serial_device_hint": serial_hint,
        "mqtt_host": os.getenv("MQTT_HOST", "localhost"),
        "mqtt_port": int(os.getenv("MQTT_PORT", "1883")),
        "mqtt_username": os.getenv("MQTT_USERNAME") or None,
        "mqtt_password": os.getenv("MQTT_PASSWORD") or None,
        "mqtt_client_id": os.getenv("MQTT_CLIENT_ID", "solartracer"),
        "mqtt_base_topic": os.getenv("MQTT_BASE_TOPIC", "solartracer").rstrip("/"),
        "mqtt_ha_discovery_prefix": os.getenv("MQTT_HA_DISCOVERY_PREFIX", "homeassistant").rstrip("/"),
        "device_name": os.getenv("DEVICE_NAME", "Solar Tracer"),
        "device_id": os.getenv("DEVICE_ID", "solartracer_pi"),
        "manufacturer": os.getenv("MANUFACTURER", "EPEVER"),
        "model": os.getenv("MODEL", "Tracer BN Series"),
        "publish_interval": int(os.getenv("PUBLISH_INTERVAL", "60")),
        "voltage_sample_interval": float(
            os.getenv("VOLTAGE_SAMPLE_INTERVAL", os.getenv("PUBLISH_INTERVAL", "60"))
        ),
        "config_refresh_sec": float(os.getenv("CONFIG_REFRESH_SEC", "300")),
        "unreachable_poll_interval": int(os.getenv("UNREACHABLE_POLL_INTERVAL", "120")),
        "discovery_refresh_sec": float(os.getenv("DISCOVERY_REFRESH_SEC", "3600")),
    }
    return cfg


def on_connect(client, userdata, flags, reason_code, properties=None):
    # Compatible with both old and new paho-mqtt callback signatures
    rc = reason_code.value if hasattr(reason_code, "value") else reason_code
    if rc == 0:
        print("Connected to MQTT broker")
    else:
        print(f"Failed to connect to MQTT broker, return code {rc}")


def connect_mqtt(cfg: Dict[str, Any]) -> mqtt.Client:
    # Use VERSION2 to avoid deprecation warning on newer paho-mqtt
    client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id=cfg["mqtt_client_id"])
    client.on_connect = on_connect

    if cfg["mqtt_username"]:
        client.username_pw_set(cfg["mqtt_username"], cfg["mqtt_password"])

    print(f"Connecting to MQTT at {cfg['mqtt_host']}:{cfg['mqtt_port']} ...")
    client.connect(cfg["mqtt_host"], cfg["mqtt_port"], keepalive=60)
    client.loop_start()
    # Give it a moment to connect
    time.sleep(0.5)
    return client


def publish_value(client: mqtt.Client, topic: str, value: Any, retain: bool = False):
    if value is None:
        payload = ""
    else:
        payload = str(value)
    client.publish(topic, payload=payload, qos=0, retain=retain)
    print(f"  {topic} = {payload}")


# Published when the charger responds on RS-485; also drives HA entity availability.
REACHABILITY_KEY = "charger_reachable"


def _availability_fields(cfg: Dict[str, Any]) -> Dict[str, str]:
    """HA availability tied to charger_reachable (shared with the binary sensor)."""
    base_topic = cfg["mqtt_base_topic"]
    return {
        "availability_topic": f"{base_topic}/{REACHABILITY_KEY}",
        "payload_available": "true",
        "payload_not_available": "false",
    }


def publish_discovery(client: mqtt.Client, cfg: Dict[str, Any], key: str, name: str,
                      unit: Optional[str], device_class: Optional[str],
                      state_class: Optional[str]):
    """Publish Home Assistant MQTT discovery config for a sensor."""
    if not cfg["mqtt_ha_discovery_prefix"]:
        return

    base_topic = cfg["mqtt_base_topic"]
    discovery_prefix = cfg["mqtt_ha_discovery_prefix"]
    device_id = cfg["device_id"]

    unique_id = f"{device_id}_{key}"
    config_topic = f"{discovery_prefix}/sensor/{device_id}/{key}/config"
    state_topic = f"{base_topic}/{key}"

    config = {
        "name": name,
        "state_topic": state_topic,
        "unique_id": unique_id,
        "device": {
            "identifiers": [device_id],
            "name": cfg["device_name"],
            "manufacturer": cfg["manufacturer"],
            "model": cfg["model"],
        },
        **_availability_fields(cfg),
    }

    if unit:
        config["unit_of_measurement"] = unit
    if device_class:
        config["device_class"] = device_class
    if state_class:
        config["state_class"] = state_class

    client.publish(config_topic, payload=json.dumps(config), qos=1, retain=True)
    print(f"  Discovery: {config_topic}")


def publish_binary_discovery(client: mqtt.Client, cfg: Dict[str, Any], key: str, name: str,
                             device_class: Optional[str] = None,
                             payload_on: str = "on", payload_off: str = "off",
                             use_availability: bool = True):
    """Publish Home Assistant MQTT discovery config for a binary_sensor."""
    if not cfg["mqtt_ha_discovery_prefix"]:
        return

    base_topic = cfg["mqtt_base_topic"]
    discovery_prefix = cfg["mqtt_ha_discovery_prefix"]
    device_id = cfg["device_id"]

    unique_id = f"{device_id}_{key}"
    config_topic = f"{discovery_prefix}/binary_sensor/{device_id}/{key}/config"
    state_topic = f"{base_topic}/{key}"

    config = {
        "name": name,
        "state_topic": state_topic,
        "unique_id": unique_id,
        "device": {
            "identifiers": [device_id],
            "name": cfg["device_name"],
            "manufacturer": cfg["manufacturer"],
            "model": cfg["model"],
        },
        "payload_on": payload_on,
        "payload_off": payload_off,
    }
    if use_availability:
        config.update(_availability_fields(cfg))

    if device_class:
        config["device_class"] = device_class

    client.publish(config_topic, payload=json.dumps(config), qos=1, retain=True)
    print(f"  Discovery: {config_topic}")


def publish_select_discovery(
    client: mqtt.Client,
    cfg: Dict[str, Any],
    key: str,
    name: str,
    options: list,
):
    """Publish Home Assistant MQTT discovery config for a select."""
    if not cfg["mqtt_ha_discovery_prefix"]:
        return

    base_topic = cfg["mqtt_base_topic"]
    discovery_prefix = cfg["mqtt_ha_discovery_prefix"]
    device_id = cfg["device_id"]

    unique_id = f"{device_id}_{key}"
    config_topic = f"{discovery_prefix}/select/{device_id}/{key}/config"
    state_topic = f"{base_topic}/{key}"
    command_topic = f"{base_topic}/{key}/set"

    config = {
        "name": name,
        "state_topic": state_topic,
        "command_topic": command_topic,
        "unique_id": unique_id,
        "options": options,
        "device": {
            "identifiers": [device_id],
            "name": cfg["device_name"],
            "manufacturer": cfg["manufacturer"],
            "model": cfg["model"],
        },
        **_availability_fields(cfg),
    }

    client.publish(config_topic, payload=json.dumps(config), qos=1, retain=True)
    print(f"  Select Discovery: {config_topic}")


def publish_switch_discovery(client: mqtt.Client, cfg: Dict[str, Any], key: str, name: str,
                             payload_on="on", payload_off="off"):
    """Publish Home Assistant MQTT discovery config for a switch (toggleable)."""
    if not cfg["mqtt_ha_discovery_prefix"]:
        return

    base_topic = cfg["mqtt_base_topic"]
    discovery_prefix = cfg["mqtt_ha_discovery_prefix"]
    device_id = cfg["device_id"]

    unique_id = f"{device_id}_{key}"
    config_topic = f"{discovery_prefix}/switch/{device_id}/{key}/config"
    state_topic = f"{base_topic}/{key}"
    command_topic = f"{base_topic}/{key}/set"

    config = {
        "name": name,
        "state_topic": state_topic,
        "command_topic": command_topic,
        "unique_id": unique_id,
        "device": {
            "identifiers": [device_id],
            "name": cfg["device_name"],
            "manufacturer": cfg["manufacturer"],
            "model": cfg["model"],
        },
        "payload_on": payload_on,
        "payload_off": payload_off,
        **_availability_fields(cfg),
    }

    client.publish(config_topic, payload=json.dumps(config), qos=1, retain=True)
    print(f"  Switch Discovery: {config_topic}")


# Mapping from our internal key to friendly info for discovery
# (key, display_name, unit, device_class, state_class)
DISCOVERY_MAP = {
    "pv_voltage": ("PV Voltage", "V", "voltage", "measurement"),
    "pv_current": ("PV Current", "A", "current", "measurement"),
    "battery_voltage": ("Battery Voltage", "V", "voltage", "measurement"),
    "battery_current": ("Battery Current", "A", "current", "measurement"),
    "load_current": ("Load Current", "A", "current", "measurement"),
    "charger_temp": ("Charger Temperature", "°C", "temperature", "measurement"),
    "power_temp": ("Power Component Temperature", "°C", "temperature", "measurement"),
    "battery_temp": ("Battery Temperature", "°C", "temperature", "measurement"),
    "max_pv_voltage_today": ("Max PV Voltage Today", "V", "voltage", "measurement"),
    "max_battery_voltage_today": ("Max Battery Voltage Today", "V", "voltage", "measurement"),
    "min_battery_voltage_today": ("Min Battery Voltage Today", "V", "voltage", "measurement"),
    "consumed_energy_today": ("Consumed Energy Today", "kWh", "energy", "total_increasing"),
    "generated_energy_today": ("Generated Energy Today", "kWh", "energy", "total_increasing"),
    "charging_status": ("Charging Status", None, None, None),
    "battery_level_pct": ("Battery Level", "%", "battery", "measurement"),
    "time_remaining": ("Time Remaining", None, None, None),
    "time_remaining_seconds": ("Time Remaining Seconds", "s", "duration", "measurement"),
}

# Binary sensors (read-only states)
BINARY_DISCOVERY_MAP = {
    "is_charging": ("Charging", None),
    "is_night": ("PV Night", None),
}

BINARY_PAYLOADS = {
    "is_night": ("true", "false"),
}

# Selects (multi-state control)
SELECT_DISCOVERY_MAP = {
    "lights_mode": ("Lights Mode", list(LIGHTS_STATES)),
}

# Switches (toggleable control; forces manual on/off, exits auto mode)
SWITCH_DISCOVERY_MAP = {
    "lights_on": ("Lights Relay", "on", "off"),
}

# Charger configuration (holding registers, refreshed periodically).
CONFIG_DISCOVERY_MAP = {
    "battery_capacity_ah": ("Battery Capacity", "Ah", None, "measurement"),
    "float_voltage": ("Float Voltage Setpoint", "V", "voltage", "measurement"),
    "boost_voltage": ("Boost Voltage Setpoint", "V", "voltage", "measurement"),
    "equalization_voltage": ("Equalization Voltage Setpoint", "V", "voltage", "measurement"),
    "charging_limit_voltage": ("Charging Limit Voltage", "V", "voltage", "measurement"),
    "boost_reconnect_voltage": ("Boost Reconnect Voltage", "V", "voltage", "measurement"),
    "low_voltage_reconnect": ("Low Voltage Reconnect", "V", "voltage", "measurement"),
    "under_voltage_recover": ("Under Voltage Recover", "V", "voltage", "measurement"),
    "under_voltage_warning": ("Under Voltage Warning", "V", "voltage", "measurement"),
    "low_voltage_disconnect": ("Low Voltage Disconnect", "V", "voltage", "measurement"),
    "discharging_limit_voltage": ("Discharging Limit Voltage", "V", "voltage", "measurement"),
    "night_time_threshold_voltage": ("Night Threshold Voltage", "V", "voltage", "measurement"),
    "day_time_threshold_voltage": ("Day Threshold Voltage", "V", "voltage", "measurement"),
    "discharging_percentage": ("Discharge Depth Limit", "%", None, "measurement"),
    "charging_percentage": ("Charge Depth Limit", "%", None, "measurement"),
    "battery_type_name": ("Battery Type", None, None, None),
    "battery_rated_voltage_name": ("Battery Rated Voltage", None, None, None),
    "management_mode_name": ("Management Mode", None, None, None),
}

# Retired HA entity (coil 0 is not in the Tracer BN protocol).
RETIRED_ENTITIES = ("charging_enabled_coil",)

# Extra state topics cleared on disconnect (not in discovery maps above).
EXTRA_STATE_KEYS = (
    "rated_pv_voltage",
    "rated_pv_current",
    "rated_battery_voltage",
    "rated_charge_current",
    "charging_equipment_status",
    "load_control_mode",
    "lights_manual_mode",
    "lights_state",
    "battery_soc",
    "battery_level_pct",
    "system_rated_voltage",
)

UNREACHABLE_CLEAR_THRESHOLD = 3
SERIAL_RESCAN_INTERVAL_SEC = 30.0


def _maybe_rescan_serial(cfg: Dict[str, Any], runtime: Dict[str, Any]) -> bool:
    """Re-detect the Exar port after USB replug (ttyACM number may change)."""
    now = time.time()
    if now - runtime.get("last_serial_scan", 0.0) < SERIAL_RESCAN_INTERVAL_SEC:
        return False
    runtime["last_serial_scan"] = now
    device, changed, message = rescan_serial_device(
        cfg["serial_device"],
        cfg.get("serial_device_hint"),
        runtime.get("serial_device_realpath"),
    )
    if changed:
        print(message)
        cfg["serial_device"] = device
        runtime["serial_device_realpath"] = device_realpath(device)
        runtime["consecutive_failures"] = 0
        return True
    return False


def _poll_intervals(cfg: Dict[str, Any], runtime: Dict[str, Any]) -> tuple:
    """Return (publish_interval, voltage_sample_interval), slower when unreachable."""
    failures = runtime.get("consecutive_failures", 0)
    if failures >= UNREACHABLE_CLEAR_THRESHOLD:
        slow = cfg["unreachable_poll_interval"]
        return slow, slow
    return cfg["publish_interval"], cfg["voltage_sample_interval"]


def _needs_extra_voltage_sample(
    cfg: Dict[str, Any],
    last_publish: float,
    last_sample: float,
    now: float,
) -> bool:
    """Skip redundant reads when a full publish already sampled voltage recently."""
    voltage_iv = cfg["voltage_sample_interval"]
    if now - last_sample < voltage_iv:
        return False
    if voltage_iv >= cfg["publish_interval"]:
        return False
    return now - last_publish >= voltage_iv

ALL_STATE_KEYS = (
    tuple(DISCOVERY_MAP.keys())
    + tuple(BINARY_DISCOVERY_MAP.keys())
    + tuple(SELECT_DISCOVERY_MAP.keys())
    + tuple(SWITCH_DISCOVERY_MAP.keys())
    + tuple(CONFIG_DISCOVERY_MAP.keys())
    + EXTRA_STATE_KEYS
    + ("last_update",)
)


def publish_charger_reachability(client: mqtt.Client, cfg: Dict[str, Any], reachable: bool):
    topic = f"{cfg['mqtt_base_topic']}/{REACHABILITY_KEY}"
    payload = "true" if reachable else "false"
    client.publish(topic, payload=payload, qos=1, retain=True)
    print(f"  {topic} = {payload}")


def clear_all_states(client: mqtt.Client, cfg: Dict[str, Any]):
    """Clear retained MQTT state topics so HA does not show stale values."""
    base_topic = cfg["mqtt_base_topic"]
    for key in ALL_STATE_KEYS:
        publish_value(client, f"{base_topic}/{key}", None, retain=True)


def retire_ha_entities(client: mqtt.Client, cfg: Dict[str, Any]):
    """Remove retired MQTT discovery entities from Home Assistant."""
    if not cfg["mqtt_ha_discovery_prefix"]:
        return

    discovery_prefix = cfg["mqtt_ha_discovery_prefix"]
    device_id = cfg["device_id"]
    base_topic = cfg["mqtt_base_topic"]

    for key in RETIRED_ENTITIES:
        config_topic = f"{discovery_prefix}/switch/{device_id}/{key}/config"
        client.publish(config_topic, payload="", qos=1, retain=True)
        publish_value(client, f"{base_topic}/{key}", None, retain=True)
        print(f"  Retired: {config_topic}")


def publish_all_discoveries(client: mqtt.Client, cfg: Dict[str, Any]):
    """Publish HA discovery configs (including reachability binary sensor)."""
    if not cfg["mqtt_ha_discovery_prefix"]:
        return

    retire_ha_entities(client, cfg)

    for key, (name, unit, device_class, state_class) in DISCOVERY_MAP.items():
        publish_discovery(client, cfg, key, name, unit, device_class, state_class)
    for key, (name, device_class) in BINARY_DISCOVERY_MAP.items():
        payload_on, payload_off = BINARY_PAYLOADS.get(key, ("on", "off"))
        publish_binary_discovery(
            client, cfg, key, name, device_class,
            payload_on=payload_on, payload_off=payload_off,
        )
    for key, (name, options) in SELECT_DISCOVERY_MAP.items():
        publish_select_discovery(client, cfg, key, name, options)
    for key, (name, p_on, p_off) in SWITCH_DISCOVERY_MAP.items():
        publish_switch_discovery(client, cfg, key, name, p_on, p_off)
    for key, (name, unit, device_class, state_class) in CONFIG_DISCOVERY_MAP.items():
        publish_discovery(client, cfg, key, name, unit, device_class, state_class)
    publish_binary_discovery(
        client,
        cfg,
        REACHABILITY_KEY,
        "Charger Reachable",
        "connectivity",
        payload_on="true",
        payload_off="false",
        use_availability=False,
    )


def publish_states(
    client: mqtt.Client,
    cfg: Dict[str, Any],
    telemetry: TelemetryStore,
    cached_config: Dict[str, Any],
    last_config_read: float,
    runtime: Dict[str, Any],
) -> float:
    """Read current data and publish all states + discovery (including switches for control)."""
    now = time.time()
    if runtime.get("consecutive_failures", 0) > 0:
        _maybe_rescan_serial(cfg, runtime)

    need_config = (
        not cached_config
        or (now - last_config_read) >= cfg["config_refresh_sec"]
    )
    data = get_solar_data(device=cfg["serial_device"], include_config=need_config)
    if need_config and data.get("config") and config_has_values(data["config"]):
        cached_config.clear()
        cached_config.update(data["config"])
        last_config_read = now
    if cached_config:
        data["config"] = dict(cached_config)
    data["battery_level_pct"] = compute_battery_level_pct(data, data.get("config"))
    telemetry.record_charger_sample(data)
    pack_v = data.get("battery_voltage")
    if pack_v is not None:
        telemetry.add_voltage_sample(pack_v)

    time_label, time_seconds = display_time_remaining(
        data, telemetry.voltage_history, telemetry.rate_log,
    )
    data["time_remaining"] = time_label
    data["time_remaining_seconds"] = time_seconds

    if not is_charger_reachable(data):
        runtime["consecutive_failures"] = runtime.get("consecutive_failures", 0) + 1
        failures = runtime["consecutive_failures"]
        _maybe_rescan_serial(cfg, runtime)
        print(
            f"WARNING: Charger unreachable ({failures}/{UNREACHABLE_CLEAR_THRESHOLD}) "
            f"on {cfg['serial_device']} — keeping last MQTT states"
        )
        if failures >= UNREACHABLE_CLEAR_THRESHOLD:
            publish_charger_reachability(client, cfg, False)
            clear_all_states(client, cfg)
            runtime["was_reachable"] = False
        return last_config_read

    runtime["consecutive_failures"] = 0
    runtime["was_reachable"] = True
    # Always refresh availability while reachable (broker may retain a stale false).
    publish_charger_reachability(client, cfg, True)

    def _to_onoff(v):
        if v is None:
            return None
        if isinstance(v, bool):
            return "on" if v else "off"
        if v in (0, "0", False):
            return "off"
        if v in (1, "1", True):
            return "on"
        return str(v).lower()

    data["lights_mode"] = get_lights_state(data)

    for key in ("is_charging", "lights_on"):
        if key in data:
            data[key] = _to_onoff(data[key])

    if data.get("is_night") is not None:
        data["is_night"] = "true" if data["is_night"] else "false"

    base_topic = cfg["mqtt_base_topic"]

    # Publish states (retained)
    for key, value in data.items():
        if key == "config":
            continue
        publish_value(client, f"{base_topic}/{key}", value, retain=True)

    config = data.get("config")
    if config:
        for key in CONFIG_MQTT_KEYS:
            if key in config:
                publish_value(client, f"{base_topic}/{key}", config[key], retain=True)

    last_update = time.strftime("%Y-%m-%d %H:%M:%S")
    publish_value(client, f"{base_topic}/last_update", last_update, retain=True)

    try:
        telemetry.persist()
    except OSError as exc:
        print(f"Failed to persist telemetry: {exc}")

    return last_config_read


def main():
    cfg = load_config()
    dry_run = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")

    print("=== SolarTracer MQTT Publisher (daemon mode for HA control) ===")
    hint = cfg.get("serial_device_hint")
    if hint and hint.strip().lower() not in ("", "auto"):
        print(f"Serial device: {cfg['serial_device']} (hint: {hint})")
    else:
        print(f"Serial device: {cfg['serial_device']} (auto-detected)")
    print(f"MQTT base topic: {cfg['mqtt_base_topic']}")
    print(f"Publish interval: {cfg['publish_interval']}s")
    print(f"Voltage sample interval: {cfg['voltage_sample_interval']}s")
    print(f"Unreachable poll interval: {cfg['unreachable_poll_interval']}s")
    print(f"HA Discovery prefix: {cfg['mqtt_ha_discovery_prefix'] or '(disabled)'}")
    if dry_run:
        print("DRY_RUN mode - no MQTT connection")
        # Dry run can still print what would be published
        data = get_solar_data(device=cfg["serial_device"])
        print("[DRY RUN] States and switch commands would be available")
        return

    client = connect_mqtt(cfg)
    base_topic = cfg["mqtt_base_topic"]
    telemetry = TelemetryStore.load()
    cached_config: Dict[str, Any] = {}
    last_config_read = 0.0
    last_sample = 0.0
    last_publish = 0.0
    last_discovery = 0.0
    runtime: Dict[str, Any] = {
        "consecutive_failures": 0,
        "was_reachable": None,
        "serial_device_realpath": device_realpath(cfg["serial_device"]),
    }

    # Subscribe to command topics so HA can control lights
    client.subscribe(f"{base_topic}/lights_on/set")
    client.subscribe(f"{base_topic}/lights_mode/set")

    def on_message(client, userdata, msg):
        nonlocal last_config_read
        payload = msg.payload.decode().strip().lower()
        topic = msg.topic
        print(f"Received command: {topic} = {payload}")
        try:
            if topic.endswith("lights_mode/set"):
                if payload in LIGHTS_STATES:
                    set_lights_state(payload, device=cfg["serial_device"], quiet=True)
                else:
                    print(f"  Ignored unknown lights mode: {payload!r}")
            elif topic.endswith("lights_on/set"):
                set_lights_enabled(payload == "on", device=cfg["serial_device"], quiet=True)
            time.sleep(0.7)  # let the controller apply the change
            last_config_read = publish_states(
                client, cfg, telemetry, cached_config, last_config_read, runtime,
            )
        except Exception as e:
            print(f"Command handler error: {e}")

    client.on_message = on_message

    print("Subscribed to command topics. Starting periodic state publishing...")

    # Register HA entities (availability + reachability sensor) before first read.
    publish_all_discoveries(client, cfg)
    last_discovery = time.time()

    def sample_voltage_history() -> None:
        nonlocal last_sample
        voltage = read_battery_voltage(device=cfg["serial_device"])
        if voltage is not None:
            telemetry.add_voltage_sample(voltage)
            try:
                telemetry.persist()
            except OSError as exc:
                print(f"Failed to persist telemetry: {exc}")
        last_sample = time.time()

    # Initial publish (states + switch discovery)
    sample_voltage_history()
    last_config_read = publish_states(
        client, cfg, telemetry, cached_config, last_config_read, runtime,
    )
    last_publish = time.time()

    # Sample voltage between full publishes when needed; full publish on interval.
    try:
        while True:
            now = time.time()
            publish_iv, _ = _poll_intervals(cfg, runtime)

            if _needs_extra_voltage_sample(cfg, last_publish, last_sample, now):
                sample_voltage_history()

            if now - last_publish >= publish_iv:
                last_config_read = publish_states(
                    client, cfg, telemetry, cached_config, last_config_read, runtime,
                )
                last_publish = now
                last_sample = now

            if (
                cfg["mqtt_ha_discovery_prefix"]
                and now - last_discovery >= cfg["discovery_refresh_sec"]
            ):
                publish_all_discoveries(client, cfg)
                last_discovery = now

            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
