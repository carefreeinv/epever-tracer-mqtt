"""Resolve the Exar XR21B1411 RS-485 serial port across USB replugs."""

from __future__ import annotations

import glob
import os
import subprocess
import sys
import time
from typing import Callable, List, Optional, Tuple

EXAR_VENDOR_ID = 0x04E2
EXAR_PRODUCT_ID = 0x1411
STABLE_DEVICE = "/dev/solartracer-rs485"
_AUTO_VALUES = frozenset({"", "auto", "detect", "scan"})
_LOCATION_CACHE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "var", "exar-usb-location",
)


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


def _hub_port_from_device_name(name: str) -> Tuple[Optional[str], Optional[int]]:
    """Map a sysfs USB device name (e.g. 1-1.1) to hub location and port."""
    if "." not in name:
        return None, None
    hub, port_str = name.rsplit(".", 1)
    if not port_str.isdigit():
        return None, None
    return hub, int(port_str)


def _remember_exar_location(sysfs_path: str) -> None:
    hub, port = _hub_port_from_device_name(os.path.basename(sysfs_path))
    if hub is None or port is None:
        return
    try:
        os.makedirs(os.path.dirname(_LOCATION_CACHE), exist_ok=True)
        with open(_LOCATION_CACHE, "w", encoding="ascii") as handle:
            handle.write(f"{hub} {port}\n")
    except OSError:
        pass


def _cached_hub_port() -> Tuple[Optional[str], Optional[int]]:
    try:
        with open(_LOCATION_CACHE, encoding="ascii") as handle:
            parts = handle.read().strip().split()
        if len(parts) == 2 and parts[1].isdigit():
            return parts[0], int(parts[1])
    except OSError:
        pass
    return None, None


def _exar_usb_bus_devices() -> List[str]:
    """Find Exar adapters directly on the USB bus (works before ttyACM appears)."""
    matches: List[str] = []
    for entry in sorted(glob.glob("/sys/bus/usb/devices/*")):
        name = os.path.basename(entry)
        if ":" in name or name.startswith("usb"):
            continue
        vendor = _read_hex(os.path.join(entry, "idVendor"))
        product = _read_hex(os.path.join(entry, "idProduct"))
        if vendor == EXAR_VENDOR_ID and product == EXAR_PRODUCT_ID:
            real = os.path.realpath(entry)
            if real not in matches:
                matches.append(real)
    return matches


def _exar_usb_sysfs_path_from_tty() -> Optional[str]:
    for tty_path in sorted(glob.glob("/sys/class/tty/ttyACM*")):
        device_path = os.path.realpath(os.path.join(tty_path, "device"))
        path = device_path
        for _ in range(10):
            vendor = _read_hex(os.path.join(path, "idVendor"))
            product = _read_hex(os.path.join(path, "idProduct"))
            if vendor is not None and product is not None:
                if vendor == EXAR_VENDOR_ID and product == EXAR_PRODUCT_ID:
                    return path
                break
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
    return None


def _exar_usb_sysfs_path() -> Optional[str]:
    """Return sysfs path for the Exar USB device (not the tty node)."""
    bus_devices = _exar_usb_bus_devices()
    if bus_devices:
        _remember_exar_location(bus_devices[0])
        return bus_devices[0]
    tty_path = _exar_usb_sysfs_path_from_tty()
    if tty_path is not None:
        _remember_exar_location(tty_path)
    return tty_path


def exar_present_on_usb() -> bool:
    """True when the Exar stick is enumerated on the USB bus."""
    if _exar_usb_bus_devices():
        return True
    try:
        import usb.core
    except ImportError:
        return False
    return usb.core.find(idVendor=EXAR_VENDOR_ID, idProduct=EXAR_PRODUCT_ID) is not None


def exar_enumeration_stuck() -> bool:
    """True when the Exar is on USB but no usable tty node exists (config -110)."""
    if not exar_present_on_usb():
        return False
    for port in find_exar_serial_ports():
        if not os.path.exists(port):
            continue
        real = device_realpath(port)
        if real and os.path.exists(real):
            return False
    return True


def cycle_exar_hub_port(
    hub: Optional[str] = None,
    port: Optional[int] = None,
) -> Tuple[bool, str]:
    """Power-cycle the hub port the Exar stick last used (enumeration recovery)."""
    if hub is None or port is None:
        cached_hub, cached_port = _cached_hub_port()
        hub = hub or cached_hub or "1-1"
        port = port if port is not None else (cached_port or 1)

    commands = (
        ["sudo", "-n", "uhubctl", "-l", hub, "-p", str(port), "-a", "cycle"],
        ["uhubctl", "-l", hub, "-p", str(port), "-a", "cycle"],
    )
    last_error = "uhubctl not available"
    for cmd in commands:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=12,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            last_error = str(exc)
            continue
        if result.returncode == 0:
            time.sleep(2.0)
            return True, f"Hub port cycled: {hub} port {port}"
        detail = (result.stderr or result.stdout or "").strip()
        last_error = detail or f"exit {result.returncode}"

    return False, f"Hub port cycle failed ({hub}:{port}: {last_error})"


def wait_for_serial_device(
    device: str,
    timeout: float = 8.0,
    poll_interval: float = 0.25,
) -> bool:
    """Block until *device* exists or *timeout* elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(device):
            return True
        time.sleep(poll_interval)
    return os.path.exists(device)


def _sudo_write_sysfs_authorized(path: str, timeout: float = 3.0) -> None:
    """Write authorized via sudo tee; direct sysfs writes can hang when USB is wedged."""
    for value in ("0", "1"):
        try:
            result = subprocess.run(
                ["sudo", "-n", "tee", path],
                input=f"{value}\n",
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise OSError(f"timed out writing {path}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise OSError(detail or f"sudo tee {path} failed")
        if value == "0":
            time.sleep(0.6)


def reset_exar_usb_device() -> Tuple[bool, str]:
    """Cycle USB authorization to force the adapter to re-enumerate."""
    sysfs = _exar_usb_sysfs_path()
    if sysfs is None:
        return False, "Exar USB device not found in sysfs"

    authorized = os.path.join(sysfs, "authorized")
    if os.path.exists(authorized):
        try:
            _sudo_write_sysfs_authorized(authorized)
            return True, f"USB re-authorized: {sysfs}"
        except OSError as exc:
            return False, f"USB re-authorize failed ({exc})"

    driver_path = os.path.join(sysfs, "driver")
    device_name = os.path.basename(sysfs)
    unbind_path = os.path.join(driver_path, "unbind")
    bind_path = os.path.join(os.path.dirname(driver_path), "bind")
    if not (os.path.exists(unbind_path) and os.path.exists(bind_path)):
        return False, "USB reset not available (no authorized or driver unbind)"

    try:
        with open(unbind_path, "w", encoding="ascii") as handle:
            handle.write(f"{device_name}\n")
        time.sleep(0.8)
        with open(bind_path, "w", encoding="ascii") as handle:
            handle.write(f"{device_name}\n")
        return True, f"USB driver rebound: {device_name}"
    except OSError as exc:
        return False, f"USB driver rebind failed ({exc})"


def reconfigure_exar_adapter(
    timeout: float = 30.0,
    *,
    gentle: bool = False,
) -> Tuple[bool, str]:
    """Run configure-exar-rs485.py to restore RS-485 half-duplex mode."""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "configure-exar-rs485.py")
    if not os.path.isfile(script):
        return False, f"configure script missing: {script}"

    extra = ["--gentle"] if gentle else []
    commands = (
        [sys.executable, script, *extra],
        ["sudo", "-n", sys.executable, script, *extra],
    )
    last_error = "unknown error"
    for cmd in commands:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            last_error = str(exc)
            continue
        if result.returncode == 0:
            return True, "Exar RS-485 mode reconfigured"
        detail = (result.stderr or result.stdout or "").strip()
        last_error = detail or f"exit {result.returncode}"

    return False, f"Exar reconfigure failed: {last_error}"


def recover_serial_comms(
    current: str,
    explicit: Optional[str] = None,
    current_realpath: Optional[str] = None,
    *,
    log: Optional[Callable[[str], None]] = None,
    allow_usb_reset: bool = True,
    allow_reconfigure: bool = True,
    wait_timeout: float = 8.0,
) -> Tuple[str, bool, str]:
    """Actively try to restore RS-485 comms after a failed read.

    Steps (each logged when *log* is provided):
      1. Rescan for a new/replugged tty path
      2. USB reset when the device is missing or the path did not change
      3. Wait for the serial node to reappear
      4. Re-run Exar RS-485 configuration
      5. Final rescan

    Returns (device, did_recovery_work, summary_message).
    """
    emit = log or (lambda _msg: None)
    actions: List[str] = []
    device = current

    resolved, changed, message = rescan_serial_device(
        current, explicit, current_realpath,
    )
    device = resolved
    if changed:
        actions.append(message)
        emit(message)

    device_missing = not os.path.exists(device)
    exar_missing = not exar_present_on_usb()
    stuck = exar_enumeration_stuck()
    if allow_usb_reset and (device_missing or exar_missing or stuck or not changed):
        ok = False
        # sysfs authorize blocks when enumeration is wedged (dmesg: can't set config -110)
        if stuck:
            ok_hub, hub_msg = cycle_exar_hub_port()
            actions.append(hub_msg)
            emit(f"Serial recovery: {hub_msg}")
            ok = ok_hub
        else:
            ok, usb_msg = reset_exar_usb_device()
            actions.append(usb_msg)
            emit(f"Serial recovery: {usb_msg}")
            if not ok and (device_missing or exar_missing):
                ok_hub, hub_msg = cycle_exar_hub_port()
                actions.append(hub_msg)
                emit(f"Serial recovery: {hub_msg}")
                ok = ok_hub
        if ok:
            wait_for_serial_device(STABLE_DEVICE, timeout=wait_timeout)
            for port in find_exar_serial_ports():
                wait_for_serial_device(port, timeout=wait_timeout)
            resolved, changed2, msg2 = rescan_serial_device(
                device, explicit, current_realpath,
            )
            device = resolved
            if changed2:
                actions.append(msg2)
                emit(msg2)

    # Reconfigure only when the tty vanished or we just reset USB. Running the
    # full script (rmmod cdc_acm) against a healthy open port breaks the driver.
    if allow_reconfigure and (device_missing or changed):
        ok, cfg_msg = reconfigure_exar_adapter(gentle=device_missing)
        actions.append(cfg_msg)
        emit(f"Serial recovery: {cfg_msg}")
        if ok:
            wait_for_serial_device(device, timeout=wait_timeout)
            resolved, changed3, msg3 = rescan_serial_device(
                device, explicit, current_realpath,
            )
            device = resolved
            if changed3:
                actions.append(msg3)
                emit(msg3)

    did_work = bool(actions)
    summary = "; ".join(actions) if actions else "no recovery actions taken"
    return device, did_work, summary