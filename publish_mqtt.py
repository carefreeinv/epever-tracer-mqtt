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
        get_solar_data,
        set_lights_enabled,
    )
except ImportError:
    print("solar_data.py must be in the same directory.")
    sys.exit(1)


def load_config() -> Dict[str, Any]:
    """Load configuration from .env (or environment variables)."""
    load_dotenv()

    cfg = {
        "serial_device": os.getenv("SERIAL_DEVICE", "/dev/ttyACM0"),
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
        "publish_interval": int(os.getenv("PUBLISH_INTERVAL", "30")),
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
}

# Binary sensors (read-only states)
BINARY_DISCOVERY_MAP = {
    "is_charging": ("Charging", None),
}

# Switches (toggleable control)
SWITCH_DISCOVERY_MAP = {
    "lights_on": ("Lights Relay", "on", "off"),
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
)

ALL_STATE_KEYS = (
    tuple(DISCOVERY_MAP.keys())
    + tuple(BINARY_DISCOVERY_MAP.keys())
    + tuple(SWITCH_DISCOVERY_MAP.keys())
    + EXTRA_STATE_KEYS
    + ("last_update",)
)


def is_charger_reachable(data: Dict[str, Any]) -> bool:
    """True when at least one register was read from the controller."""
    return not all(v is None for v in data.values())


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
        publish_binary_discovery(client, cfg, key, name, device_class)
    for key, (name, p_on, p_off) in SWITCH_DISCOVERY_MAP.items():
        publish_switch_discovery(client, cfg, key, name, p_on, p_off)
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


def publish_states(client: mqtt.Client, cfg: Dict[str, Any]):
    """Read current data and publish all states + discovery (including switches for control)."""
    data = get_solar_data(device=cfg["serial_device"])

    if not is_charger_reachable(data):
        print("WARNING: Charger unreachable - clearing MQTT states")
        publish_charger_reachability(client, cfg, False)
        clear_all_states(client, cfg)
        return

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

    for key in ("is_charging", "lights_on"):
        if key in data:
            data[key] = _to_onoff(data[key])

    base_topic = cfg["mqtt_base_topic"]

    # Publish states (retained)
    for key, value in data.items():
        publish_value(client, f"{base_topic}/{key}", value, retain=True)

    last_update = time.strftime("%Y-%m-%d %H:%M:%S")
    publish_value(client, f"{base_topic}/last_update", last_update, retain=True)

    publish_all_discoveries(client, cfg)


def main():
    cfg = load_config()
    dry_run = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")

    print("=== SolarTracer MQTT Publisher (daemon mode for HA control) ===")
    print(f"Serial device: {cfg['serial_device']}")
    print(f"MQTT base topic: {cfg['mqtt_base_topic']}")
    print(f"Publish interval: {cfg['publish_interval']}s")
    print(f"HA Discovery prefix: {cfg['mqtt_ha_discovery_prefix'] or '(disabled)'}")
    if dry_run:
        print("DRY_RUN mode - no MQTT connection")
        # Dry run can still print what would be published
        data = get_solar_data(device=cfg["serial_device"])
        print("[DRY RUN] States and switch commands would be available")
        return

    client = connect_mqtt(cfg)
    base_topic = cfg["mqtt_base_topic"]

    # Subscribe to command topics so HA switches can control us
    client.subscribe(f"{base_topic}/lights_on/set")

    def on_message(client, userdata, msg):
        payload = msg.payload.decode().strip().lower()
        topic = msg.topic
        print(f"Received command: {topic} = {payload}")
        try:
            if topic.endswith("lights_on/set"):
                set_lights_enabled(payload == "on")
            time.sleep(0.7)  # let the controller apply the change
            publish_states(client, cfg)  # push updated state immediately
        except Exception as e:
            print(f"Command handler error: {e}")

    client.on_message = on_message

    # Background thread for receiving MQTT messages (commands)
    client.loop_start()

    print("Subscribed to command topics. Starting periodic state publishing...")

    # Register HA entities (availability + reachability sensor) before first read.
    publish_all_discoveries(client, cfg)

    # Initial publish (states + switch discovery)
    publish_states(client, cfg)

    # Periodic publish loop
    try:
        while True:
            time.sleep(cfg["publish_interval"])
            publish_states(client, cfg)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
