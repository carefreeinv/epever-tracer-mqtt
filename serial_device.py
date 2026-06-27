"""Resolve the Exar XR21B1411 RS-485 serial port across USB replugs."""

from __future__ import annotations

import glob
import os
from typing import List, Optional

EXAR_VENDOR_ID = 0x04E2
EXAR_PRODUCT_ID = 0x1411
STABLE_DEVICE = "/dev/solartracer-rs485"
_AUTO_VALUES = frozenset({"", "auto", "detect", "scan"})


def _read_hex(path: str) -> Optional[int]:
    try:
        with open(path, encoding="ascii") as handle:
            return int(handle.read().strip(), 16)
    except (OSError, ValueError):
        return None


def _exar_ports_from_sysfs() -> List[str]:
    """Find ttyACM nodes backed by the Exar adapter via sysfs."""
    matches: List[str] = []
    for tty_path in sorted(glob.glob("/sys/class/tty/ttyACM*")):
        tty_name = os.path.basename(tty_path)
        device_path = os.path.realpath(os.path.join(tty_path, "device"))
        path = device_path
        for _ in range(8):
            vendor = _read_hex(os.path.join(path, "idVendor"))
            product = _read_hex(os.path.join(path, "idProduct"))
            if vendor is not None and product is not None:
                if vendor == EXAR_VENDOR_ID and product == EXAR_PRODUCT_ID:
                    dev = f"/dev/{tty_name}"
                    if dev not in matches:
                        matches.append(dev)
                break
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
    return matches


def _exar_ports_from_pyserial() -> List[str]:
    try:
        import serial.tools.list_ports
    except ImportError:
        return []

    matches: List[str] = []
    for port in serial.tools.list_ports.comports():
        if port.vid == EXAR_VENDOR_ID and port.pid == EXAR_PRODUCT_ID and port.device:
            if port.device not in matches:
                matches.append(port.device)
    return matches


def find_exar_serial_ports() -> List[str]:
    """Return candidate serial devices for the Exar stick, stable symlink first."""
    candidates: List[str] = []
    if os.path.exists(STABLE_DEVICE):
        candidates.append(STABLE_DEVICE)
    for port in _exar_ports_from_pyserial():
        if port not in candidates:
            candidates.append(port)
    for port in _exar_ports_from_sysfs():
        if port not in candidates:
            candidates.append(port)
    return candidates


def device_realpath(device: str) -> Optional[str]:
    """Return the canonical tty node behind a path or symlink."""
    try:
        return os.path.realpath(device)
    except OSError:
        return None


def resolve_serial_device(explicit: Optional[str] = None) -> str:
    """Pick the RS-485 device path.

    Order:
      1. SERIAL_DEVICE when set (unless 'auto') and the path exists
      2. udev symlink /dev/solartracer-rs485
      3. Scan for Exar XR21B1411 (04e2:1411) on ttyACM*
      4. Fall back to the explicit hint or stable symlink for error messages
    """
    hint = (explicit or "").strip()
    if hint and hint.lower() not in _AUTO_VALUES and os.path.exists(hint):
        return hint

    candidates = find_exar_serial_ports()
    if candidates:
        return candidates[0]

    if hint and hint.lower() not in _AUTO_VALUES:
        return hint
    return STABLE_DEVICE


def rescan_serial_device(
    current: str,
    explicit: Optional[str] = None,
    current_realpath: Optional[str] = None,
) -> tuple:
    """Return (device, changed, message).

    Detects USB replugs even when the configured path is unchanged but the
    symlink target moved (e.g. /dev/solartracer-rs485: ttyACM0 -> ttyACM1).
    """
    resolved = resolve_serial_device(explicit)
    resolved_real = device_realpath(resolved) if os.path.exists(resolved) else None
    prev_real = current_realpath
    if prev_real is None and os.path.exists(current):
        prev_real = device_realpath(current)

    path_changed = resolved != current
    real_changed = (
        resolved_real is not None
        and prev_real is not None
        and resolved_real != prev_real
    )
    came_back = not os.path.exists(current) and os.path.exists(resolved)

    if not (path_changed or real_changed or came_back):
        return resolved, False, ""

    if path_changed:
        message = f"Serial device changed: {current} -> {resolved}"
    elif real_changed:
        message = f"Serial device replugged: {current} ({prev_real} -> {resolved_real})"
    else:
        message = f"Serial device reappeared: {resolved}"
    return resolved, True, message