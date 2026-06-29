"""Optional debug logging for RS-485 / MQTT daemon troubleshooting."""

from __future__ import annotations

import os
import sys
import time
from typing import Optional

_TRUE = frozenset({"1", "true", "yes", "on"})
_DEBUG: Optional[bool] = None


def is_debug_enabled() -> bool:
    """True when SOLARTRACER_DEBUG or DEBUG is set to a truthy value."""
    for key in ("SOLARTRACER_DEBUG", "DEBUG"):
        if os.getenv(key, "").strip().lower() in _TRUE:
            return True
    return False


def debug_enabled() -> bool:
    global _DEBUG
    if _DEBUG is None:
        _DEBUG = is_debug_enabled()
    return _DEBUG


def configure_stdio() -> None:
    """Use line-buffered stdout/stderr when debug mode is on."""
    if not debug_enabled():
        return
    os.environ["PYTHONUNBUFFERED"] = "1"
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except (AttributeError, OSError, ValueError):
            pass


def _emit(level: str, msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {level} {msg}", flush=True)


def log(msg: str) -> None:
    """Emit a debug line when debug mode is enabled."""
    if debug_enabled():
        _emit("DEBUG", msg)


def info(msg: str) -> None:
    """Emit an informational line (always shown)."""
    _emit("INFO", msg)


def warn(msg: str) -> None:
    """Emit a warning line (always shown)."""
    _emit("WARN", msg)