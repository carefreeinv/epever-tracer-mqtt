#!/usr/bin/env python3
"""
solar_data.py

Shared logic for reading data from EPEVER/Tracer BN solar controllers
over RS-485 using minimalmodbus.

Usage:
    from solar_data import get_solar_data, setup_instrument
    data = get_solar_data(device="/dev/ttyACM0")
"""

import os
import time
import minimalmodbus
from typing import Any, Dict, Optional, Union

# Common registers for Tracer BN series (function code 4, 2 decimal places)
# Format: (address, decimals, key, description, unit, device_class, state_class)
REGISTERS = [
    (0x3100, 2, "pv_voltage", "PV Voltage", "V", "voltage", "measurement"),
    (0x3101, 2, "pv_current", "PV Current", "A", "current", "measurement"),
    (0x3104, 2, "battery_voltage", "Battery Voltage", "V", "voltage", "measurement"),
    (0x3105, 2, "battery_current", "Battery Current", "A", "current", "measurement"),
    (0x310D, 2, "load_current", "Load Current", "A", "current", "measurement"),
    (0x3110, 2, "charger_temp", "Charger Temperature", "°C", "temperature", "measurement"),
    (0x3111, 2, "power_temp", "Power Component Temperature", "°C", "temperature", "measurement"),
    (0x311B, 2, "battery_temp", "Battery Temperature", "°C", "temperature", "measurement"),
    (0x3300, 2, "max_pv_voltage_today", "Max PV Voltage Today", "V", "voltage", "measurement"),
    (0x3302, 2, "max_battery_voltage_today", "Max Battery Voltage Today", "V", "voltage", "measurement"),
    (0x3303, 2, "min_battery_voltage_today", "Min Battery Voltage Today", "V", "voltage", "measurement"),
    (0x3304, 2, "consumed_energy_today", "Consumed Energy Today", "kWh", "energy", "total_increasing"),
    (0x330C, 2, "generated_energy_today", "Generated Energy Today", "kWh", "energy", "total_increasing"),
]

# Additional useful registers (controller returns *100, so use 2 decimals like realtime)
EXTRA_REGISTERS = [
    (0x3000, 2, "rated_pv_voltage", "Rated PV Voltage", "V"),
    (0x3001, 2, "rated_pv_current", "Rated PV Current", "A"),
    (0x3004, 2, "rated_battery_voltage", "Rated Battery Voltage", "V"),
    (0x3005, 2, "rated_charge_current", "Rated Charge Current", "A"),
]

# Status registers (read raw as integer, no decimal scaling)
STATUS_REGISTERS = [
    (0x3201, 0, "charging_equipment_status"),
]


def setup_instrument(device: str = "/dev/ttyACM0", slave: int = 1, timeout: float = 1.2) -> minimalmodbus.Instrument:
    """Create and configure a minimalmodbus Instrument for the solar controller."""
    instrument = minimalmodbus.Instrument(device, slave)
    instrument.serial.baudrate = 115200
    instrument.serial.bytesize = 8
    instrument.serial.parity = minimalmodbus.serial.PARITY_NONE
    instrument.serial.stopbits = 1
    instrument.serial.timeout = timeout
    instrument.mode = minimalmodbus.MODE_RTU
    instrument.clear_buffers_before_each_transaction = True
    return instrument


def read_register_safe(instrument: minimalmodbus.Instrument, address: int, decimals: int = 2) -> Optional[Union[int, float]]:
    """Read a register, return None on any error."""
    try:
        val = instrument.read_register(address, decimals, 4)
        # For status registers (decimals=0) return as int
        if decimals == 0 and val is not None:
            return int(val)
        return val
    except Exception:
        return None


def _decode_charging_status(raw_value: Optional[Union[int, float]]) -> Dict[str, Any]:
    """Decode EPEVER charging equipment status register (0x3201).

    From the protocol:
      D3-D2: Charging status
        00 = No charging
        01 = Float
        02 = Boost
        03 = Equalization
      D0: 1=Running, 0=Standby
      D1: 1=Fault

    We use D3-D2 for the mode.
    """
    if raw_value is None:
        return {
            "charging_status": None,
            "is_charging": None,
        }

    try:
        raw = int(float(raw_value))
    except (TypeError, ValueError):
        return {
            "charging_status": None,
            "is_charging": None,
        }

    # Charging status is in bits 2-3 (D3-D2)
    charging_mode = (raw >> 2) & 0x03

    status_map = {
        0: "No Charging",
        1: "Float",
        2: "Boost",
        3: "Equalization",
    }

    status_str = status_map.get(charging_mode, f"Unknown ({charging_mode})")
    is_charging = charging_mode != 0

    return {
        "charging_status": status_str,
        "is_charging": is_charging,
    }


def get_solar_data(device: Optional[str] = None) -> Dict[str, Any]:
    """
    Read all defined registers from the solar charger.

    Returns a dict with numeric values, plus derived fields:
        {
            "pv_voltage": 0.0,
            "battery_voltage": 26.54,
            "charging_status": "Float",
            "is_charging": True,
            ...
        }
    """
    if device is None:
        device = os.getenv("SERIAL_DEVICE", "/dev/ttyACM0")

    data: Dict[str, Optional[float]] = {}

    try:
        instrument = setup_instrument(device)
    except Exception as e:
        print(f"Failed to open serial device {device}: {e}")
        # Return empty data with keys present
        for _, _, key, *_ in REGISTERS + EXTRA_REGISTERS:
            data[key] = None
        for _, _, key in STATUS_REGISTERS:
            data[key] = None
        charging_info = _decode_charging_status(None)
        data.update(charging_info)
        return data

    # Read main realtime + daily registers
    for address, decimals, key, *_ in REGISTERS:
        data[key] = read_register_safe(instrument, address, decimals)

    # Read extra rated registers
    for address, decimals, key, *_ in EXTRA_REGISTERS:
        data[key] = read_register_safe(instrument, address, decimals)

    # Read status registers (as integers)
    for address, decimals, key in STATUS_REGISTERS:
        data[key] = read_register_safe(instrument, address, decimals)

    # Add derived charging state
    charging_info = _decode_charging_status(data.get("charging_equipment_status"))
    data.update(charging_info)

    # Read load/lights relay state from holding registers (when in manual mode)
    # 0x903D = load control mode (0 = manual)
    # 0x906A = default load on/off in manual mode (0=off, 1=on)
    try:
        load_mode = instrument.read_register(0x903D, 0, functioncode=3)
        data["load_control_mode"] = load_mode
        data["lights_manual_mode"] = (load_mode == 0)
        load_default = instrument.read_register(0x906A, 0, functioncode=3)
        data["lights_on"] = bool(load_default)
    except Exception:
        data["lights_on"] = None
        data["lights_manual_mode"] = None

    return data


def get_simple_data(device: Optional[str] = None) -> Dict[str, Optional[float]]:
    """Backwards-compatible simpler subset (used by older scripts)."""
    full = get_solar_data(device)
    return {
        "pv_voltage": full.get("pv_voltage"),
        "pv_current": full.get("pv_current"),
        "battery_voltage": full.get("battery_voltage"),
        "battery_current": full.get("battery_current"),
        "load_current": full.get("load_current"),
        "battery_temp": full.get("battery_temp"),
        "generated_energy_today": full.get("generated_energy_today"),
    }


def get_instrument(device: Optional[str] = None) -> minimalmodbus.Instrument:
    """Return a configured instrument for read/write operations."""
    if device is None:
        device = os.getenv("SERIAL_DEVICE", "/dev/ttyACM0")
    return setup_instrument(device)


def set_lights_enabled(enabled: bool):
    """Control the lights relay (load output) when in manual mode.

    Forces manual mode, uses both manual control coil (2) and force coil (6),
    plus updates the default register 0x906A.
    """
    inst = get_instrument()
    # Force manual control mode
    inst.write_register(0x903D, 0)
    # Coil 2: manual control the load
    inst.write_bit(2, enabled)
    # Coil 6: force the load on/off (temporary test / direct)
    inst.write_bit(6, enabled)
    # Default for manual mode
    inst.write_register(0x906A, 1 if enabled else 0)
    print(f"Lights relay {'enabled' if enabled else 'disabled'}")


def set_charging_limit_voltage(volts: float):
    """Temporarily set 'Charging limit voltage' (holding register 0x9004).

    This is the closest thing to a current limit available in the protocol.
    Lowering this value close to current battery voltage will cause the MPPT
    to back off and deliver very low charging current (can be tuned to ~1A).

    WARNING:
    - This changes a fundamental charging parameter.
    - Always save the original value and restore it promptly when the mini-split stops.
    - Test the voltage delta needed for your desired current (e.g. current_bat_v + 0.2V to +0.5V).
    - Do not leave it low permanently.

    volts: float, e.g. 26.8
    """
    if not (10.0 < volts < 80.0):
        raise ValueError("Voltage out of reasonable range")
    inst = get_instrument()
    val = int(round(volts * 100))
    inst.write_register(0x9004, val)  # function 0x06 single write
    print(f"Charging limit voltage set to {volts:.2f}V (0x9004={val})")


def get_charging_limit_voltage() -> float:
    """Read the current Charging limit voltage from holding register 0x9004.
    Returns value in volts (e.g. 28.2).
    """
    inst = get_instrument()
    val = inst.read_register(0x9004, 2, functioncode=3)
    return val  # already scaled by minimalmodbus decimals=2


def limit_charging_current_temporarily(desired_amps: float = 1.0, duration_sec: int = 60):
    """Example helper: lower charging limit voltage for a period.
    You should implement your own logic based on mini-split state (via HA or current sensing).
    This is approximate and requires tuning.
    """
    original = get_charging_limit_voltage()
    bat_v = get_solar_data().get("battery_voltage", 26.0)
    # Rough starting point - tune the offset for ~1A on your system
    low_limit = bat_v + 0.3
    try:
        set_charging_limit_voltage(low_limit)
        print(f"Limited charging (approx <{desired_amps}A) for {duration_sec}s. Original limit will be restored.")
        time.sleep(duration_sec)
    finally:
        set_charging_limit_voltage(original)
        print("Restored original charging limit voltage.")


if __name__ == "__main__":
    # Quick test
    print("Reading solar data...")
    data = get_solar_data()
    for k, v in sorted(data.items()):
        print(f"  {k}: {v}")

    print("\nCharging limit voltage (read only):")
    print("  Current charging limit V:", get_charging_limit_voltage())
