#!/usr/bin/env python3
"""
dashboard.py

Full-screen terminal dashboard for the EPEVER/Tracer BN solar charger.
Reads live state from MQTT (solartracer-mqtt.service) and trend/history
artifacts under ./var. Does not use RS-485 — the MQTT publisher owns serial.

Usage:
    python3 dashboard.py              # full-screen live dashboard
    python3 dashboard.py -i 3         # refresh every 3 seconds
    python3 dashboard.py --once       # plain-text snapshot (no TUI)
"""

from __future__ import annotations

import argparse
import curses
import os
import sys
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore

from solar_data import (
    CONFIG_LABEL_KEYS,
    LIGHTS_STATES,
    TimedMetricHistory,
    compute_battery_level_pct,
    compute_display_status,
    config_from_flat_data,
    config_has_values,
    display_time_remaining,
    get_lights_state,
    lights_auto_label,
    is_charger_reachable,
    lights_state_label,
    tracer_is_charging,
)
from telemetry_store import TelemetryStore

SPARK = "▁▂▃▄▅▆▇█"
GAUGE_FILL = "█"
GAUGE_EMPTY = "░"

HISTORY_LEN = 64
DEFAULT_INTERVAL = 5.0
MQTT_FETCH_TIMEOUT = 1.0
MQTT_STARTUP_TIMEOUT = 1.0

_BOOL_KEYS = frozenset({
    "is_charging", "lights_on", "lights_manual_mode", "charger_reachable",
})
_STRING_KEYS = frozenset({
    "charging_status", "lights_mode", "lights_state", "time_remaining",
    *CONFIG_LABEL_KEYS,
})


def _load_env() -> None:
    if load_dotenv is not None:
        load_dotenv()


def _mqtt_settings() -> Dict[str, Any]:
    _load_env()
    return {
        "host": os.getenv("MQTT_HOST", "localhost"),
        "port": int(os.getenv("MQTT_PORT", "1883")),
        "username": os.getenv("MQTT_USERNAME") or None,
        "password": os.getenv("MQTT_PASSWORD") or None,
        "base_topic": os.getenv("MQTT_BASE_TOPIC", "solartracer").rstrip("/"),
    }


def _parse_mqtt_value(key: str, raw: str) -> Any:
    if not raw:
        return None
    if key == "is_night":
        return raw == "true"
    if key in _BOOL_KEYS:
        return raw in ("on", "true", "1")
    if key in _STRING_KEYS:
        return raw
    try:
        return float(raw) if "." in raw else int(raw)
    except ValueError:
        return raw


class MqttStateCache:
    """Persistent MQTT subscriber — avoids reconnecting on every dashboard refresh."""

    def __init__(self) -> None:
        self._state: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._prefix = ""
        self._client: Any = None

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        client.subscribe(f"{self._prefix}#")

    def _on_message(self, client, userdata, msg):
        if not msg.topic.startswith(self._prefix):
            return
        key = msg.topic[len(self._prefix):]
        if "/" in key:
            return
        value = _parse_mqtt_value(key, msg.payload.decode(errors="replace").strip())
        with self._lock:
            self._state[key] = value
        self._ready.set()

    def start(self, startup_timeout: float = MQTT_STARTUP_TIMEOUT) -> bool:
        try:
            import paho.mqtt.client as mqtt
            from paho.mqtt.client import CallbackAPIVersion
        except ImportError:
            return False

        cfg = _mqtt_settings()
        self._prefix = cfg["base_topic"] + "/"
        client = mqtt.Client(
            CallbackAPIVersion.VERSION2,
            client_id=f"solartracer-dashboard-{os.getpid()}",
        )
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        if cfg["username"]:
            client.username_pw_set(cfg["username"], cfg["password"])
        client.connect(cfg["host"], cfg["port"], keepalive=60)
        client.loop_start()
        self._client = client
        self._ready.wait(timeout=startup_timeout)
        return self.has_data()

    def stop(self) -> None:
        if self._client is None:
            return
        self._client.loop_stop()
        self._client.disconnect()
        self._client = None

    def has_data(self) -> bool:
        with self._lock:
            return bool(self._state)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def patch(self, updates: Dict[str, Any]) -> None:
        """Optimistic local updates while waiting for the publisher to refresh."""
        with self._lock:
            self._state.update(updates)


def fetch_mqtt_state(timeout: float = MQTT_FETCH_TIMEOUT) -> Dict[str, Any]:
    """One-shot MQTT read (used by --once). Live TUI uses MqttStateCache instead."""
    try:
        import paho.mqtt.client as mqtt
        from paho.mqtt.client import CallbackAPIVersion
    except ImportError:
        return {}

    cfg = _mqtt_settings()
    prefix = cfg["base_topic"] + "/"
    state: Dict[str, Any] = {}

    def on_connect(client, userdata, flags, reason_code, properties=None):
        client.subscribe(f"{prefix}#")

    def on_message(client, userdata, msg):
        if not msg.topic.startswith(prefix):
            return
        key = msg.topic[len(prefix):]
        if "/" in key:
            return
        state[key] = _parse_mqtt_value(key, msg.payload.decode(errors="replace").strip())

    client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id="solartracer-dashboard")
    client.on_connect = on_connect
    client.on_message = on_message
    if cfg["username"]:
        client.username_pw_set(cfg["username"], cfg["password"])

    try:
        client.connect(cfg["host"], cfg["port"], keepalive=30)
        client.loop_start()
        time.sleep(timeout)
        client.loop_stop()
        client.disconnect()
    except Exception:
        return {}

    return state


def load_artifact_snapshot() -> Dict[str, Any]:
    """Last-known samples from publisher history files (fallback when MQTT is down)."""
    try:
        store = TelemetryStore.load()
        data: Dict[str, Any] = {}
        if store.voltage_history.samples:
            _, voltage = store.voltage_history.samples[-1]
            data["battery_voltage"] = voltage
        rate_log = store.rate_log
        if rate_log.samples:
            _ts, _voltage, soc, _mode = rate_log.samples[-1]
            if soc is not None:
                data["battery_level_pct"] = soc
        return data
    except OSError:
        return {}


def publish_mqtt_command(subtopic: str, payload: str) -> None:
    """Send a command topic handled by solartracer-mqtt.service."""
    try:
        import paho.mqtt.client as mqtt
        from paho.mqtt.client import CallbackAPIVersion
    except ImportError as exc:
        raise RuntimeError("paho-mqtt is required for lights control") from exc

    cfg = _mqtt_settings()
    client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id="solartracer-dashboard-cmd")
    if cfg["username"]:
        client.username_pw_set(cfg["username"], cfg["password"])
    client.connect(cfg["host"], cfg["port"], keepalive=30)
    client.publish(f"{cfg['base_topic']}/{subtopic}", payload, qos=0)
    client.loop_start()
    time.sleep(0.2)
    client.loop_stop()
    client.disconnect()


def _lights_mode_patch(next_state: str) -> Dict[str, Any]:
    """MQTT fields that reflect a lights mode change before the next publish."""
    patch: Dict[str, Any] = {
        "lights_mode": next_state,
        "lights_state": next_state,
    }
    if next_state == "auto":
        patch["load_control_mode"] = 1
        patch["lights_manual_mode"] = False
    else:
        patch["load_control_mode"] = 0
        patch["lights_manual_mode"] = True
        patch["lights_on"] = next_state == "on"
    return patch


def cycle_lights_via_mqtt(data: Dict[str, Any]) -> str:
    """Advance lights mode via MQTT (off → auto → on → off)."""
    current = data.get("lights_mode")
    if current not in LIGHTS_STATES:
        current = get_lights_state(data)
    if current not in LIGHTS_STATES:
        current = "off"
    next_state = LIGHTS_STATES[(LIGHTS_STATES.index(current) + 1) % len(LIGHTS_STATES)]
    publish_mqtt_command("lights_mode/set", next_state)
    return next_state


def fetch_charger_data(
    mqtt_cache: Optional[MqttStateCache] = None,
) -> Tuple[Dict[str, Any], str]:
    """Return (data, source). Never touches RS-485."""
    if mqtt_cache is not None:
        data = mqtt_cache.snapshot()
    else:
        data = fetch_mqtt_state()
    if data:
        data["config"] = config_from_flat_data(data)
        if is_charger_reachable(data):
            data["battery_level_pct"] = compute_battery_level_pct(data, data.get("config"))
            return data, "mqtt"
        if data.get("charger_reachable") in (False, "false"):
            data["battery_level_pct"] = compute_battery_level_pct(data, data.get("config"))
            return data, "mqtt"

    artifact = load_artifact_snapshot()
    if artifact:
        artifact["battery_level_pct"] = compute_battery_level_pct(artifact, artifact.get("config"))
        return artifact, "artifacts"

    return data or {}, "none"


def _pv_power_w(data: Dict[str, Any]) -> float:
    v = data.get("pv_voltage")
    a = data.get("pv_current")
    if v is None or a is None:
        return 0.0
    return float(v) * float(a)


def _battery_power_w(data: Dict[str, Any]) -> float:
    v = data.get("battery_voltage")
    a = data.get("battery_current")
    if v is None or a is None:
        return 0.0
    return float(v) * float(a)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _pct_of(value: float, maximum: float) -> float:
    if maximum <= 0:
        return 0.0
    return _clamp(100.0 * value / maximum, 0.0, 100.0)


def _battery_level_pct(data: Dict[str, Any]) -> float:
    pct = data.get("battery_level_pct")
    if pct is None:
        pct = compute_battery_level_pct(data, data.get("config"))
    return float(pct or 0.0)


def _format_temp(celsius: Optional[float], use_fahrenheit: bool) -> str:
    if celsius is None:
        return "—"
    if use_fahrenheit:
        return f"{celsius * 9.0 / 5.0 + 32.0:.1f}°F"
    return f"{celsius:.1f}°C"


def _config_lines(data: Dict[str, Any], width: int, source_label: str) -> List[str]:
    """Format read-only charger configuration for the dashboard."""
    cfg = data.get("config") or {}
    if not config_has_values(cfg):
        return [
            _inner_line(" Config unavailable (not published to MQTT) ", width),
            _inner_line(f" Source  {source_label} ", width),
        ]

    sys_v = data.get("system_rated_voltage")
    if sys_v is not None:
        system_v = f"{float(sys_v):.0f} V"
    else:
        system_v = str(cfg.get("battery_rated_voltage_name") or "—")
    line1 = (
        f" Config  {cfg.get('battery_rated_voltage_name', '—')} ({system_v})  "
        f"{cfg.get('battery_type_name', '—')}  "
        f"{_fmt(cfg.get('battery_capacity_ah'), 0, 'Ah')}  "
        f"{cfg.get('management_mode_name', '—')} mode"
    )
    line2 = (
        f" Voltages  float {_fmt(cfg.get('float_voltage'))}V  "
        f"boost {_fmt(cfg.get('boost_voltage'))}V  "
        f"eq {_fmt(cfg.get('equalization_voltage'))}V  "
        f"limit {_fmt(cfg.get('charging_limit_voltage'))}V"
    )
    line3 = (
        f" Limits  warn {_fmt(cfg.get('under_voltage_warning'))}V  "
        f"LVD {_fmt(cfg.get('low_voltage_disconnect'))}V  "
        f"disc {_fmt(cfg.get('discharging_limit_voltage'))}V  "
        f"depth {_fmt(cfg.get('discharging_percentage'), 0, '%')}–"
        f"{_fmt(cfg.get('charging_percentage'), 0, '%')}"
    )
    nttv = cfg.get("night_time_threshold_voltage", data.get("night_time_threshold_voltage"))
    dttv = cfg.get("day_time_threshold_voltage", data.get("day_time_threshold_voltage"))
    line4 = (
        f" Day/Night  PV≤{_fmt(nttv)}V night  PV≥{_fmt(dttv)}V day  "
        f"now {_fmt(data.get('pv_voltage'))}V"
    )
    return [
        _inner_line(line1, width),
        _inner_line(line2, width),
        _inner_line(line3, width),
        _inner_line(line4, width),
        _inner_line(f" Source  {source_label} ", width),
    ]


def _day_night_label(data: Dict[str, Any]) -> str:
    night = data.get("is_night")
    if night is True:
        return "night"
    if night is False:
        return "day"
    return "TBD"


def _lights_label(data: Dict[str, Any], compact: bool = False) -> str:
    if data.get("load_control_mode") is None and data.get("lights_on") is None:
        return "—"
    return lights_state_label(data, compact=compact)


def _lights_row_value(data: Dict[str, Any]) -> str:
    """Value column for the Lights gauge row."""
    state = data.get("lights_state") or get_lights_state(data)
    if state == "auto":
        return lights_auto_label(data)
    if state == "on":
        return "ON"
    if state == "off":
        return "OFF"
    return "—"


class MetricHistory:
    """Rolling samples for sparkline rendering."""

    def __init__(self, maxlen: int = HISTORY_LEN) -> None:
        self.samples: Deque[float] = deque(maxlen=maxlen)

    def add(self, value: Optional[float]) -> None:
        self.samples.append(0.0 if value is None else float(value))

    def recent_range(self) -> Tuple[float, float]:
        if not self.samples:
            return 0.0, 0.0
        vals = list(self.samples)
        return min(vals), max(vals)

    def sparkline(self, width: int, lo: float, hi: float) -> str:
        """Sparkline against a fixed scale (not per-window min/max)."""
        if width <= 0:
            return ""
        if not self.samples:
            return GAUGE_EMPTY * width

        recent = list(self.samples)[-width:]
        vals = [lo] * (width - len(recent)) + recent
        span = max(hi - lo, 1e-9)
        out: List[str] = []
        for v in vals:
            idx = int(_clamp((v - lo) / span * (len(SPARK) - 1), 0, len(SPARK) - 1))
            out.append(SPARK[idx])
        return "".join(out)


def render_timeline(
    history: MetricHistory,
    width: int,
    lo: float,
    hi: float,
) -> str:
    """Sparkline with fixed scale (always graph, never text fallback)."""
    if width <= 0:
        return ""
    if not history.samples:
        return GAUGE_EMPTY * width
    return history.sparkline(width, lo, hi)


class Histories:
    def __init__(self, telemetry: Optional[TelemetryStore] = None) -> None:
        self.telemetry = telemetry or TelemetryStore.load()
        self.pv_power = MetricHistory()
        self.battery_level = MetricHistory()
        self.battery_current = MetricHistory()
        self.load_current = MetricHistory()
        self.generated_kwh = MetricHistory()

    @property
    def battery_voltage_timed(self) -> TimedMetricHistory:
        return self.telemetry.voltage_history

    @property
    def battery_rate_log(self):
        return self.telemetry.rate_log

    def reload_artifacts(self) -> None:
        """Reload trend/rate history written by solartracer-mqtt.service."""
        self.telemetry = TelemetryStore.load()

    def update(self, data: Dict[str, Any]) -> None:
        """Update on-screen sparklines only (does not write artifact files)."""
        self.pv_power.add(_pv_power_w(data))
        self.battery_level.add(_battery_level_pct(data))
        self.battery_current.add(abs(data.get("battery_current") or 0.0))
        self.load_current.add(data.get("load_current"))
        self.generated_kwh.add(data.get("generated_energy_today"))


def render_gauge(pct: float, width: int) -> str:
    pct = _clamp(pct, 0.0, 100.0)
    filled = int(round(pct / 100.0 * width))
    filled = _clamp(filled, 0, width)
    return GAUGE_FILL * filled + GAUGE_EMPTY * (width - filled)


def _fmt(value: Optional[float], decimals: int = 2, unit: str = "") -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        text = "yes" if value else "no"
    elif isinstance(value, int) and decimals == 0:
        text = str(value)
    else:
        text = f"{float(value):.{decimals}f}"
    return f"{text}{unit}"


class PartialScreen:
    """Diff-based renderer: only writes cells that changed (char or color)."""

    def __init__(self, stdscr: "curses._CursesWindow") -> None:
        self.stdscr = stdscr
        self.prev: List[List[Tuple[str, int]]] = []
        self.height = 0
        self.width = 0

    def resize(self, height: int, width: int) -> None:
        self.height = height
        self.width = width
        self.prev = [[(" ", 0)] * width for _ in range(height)]

    def put_line(self, y: int, x: int, text: str, attr: int = 0) -> None:
        if y < 0 or y >= self.height:
            return
        max_len = self.width - x
        if max_len <= 0:
            return
        segment = text[:max_len].ljust(max_len)
        for i, ch in enumerate(segment):
            col = x + i
            prev_ch, prev_attr = self.prev[y][col]
            if prev_ch == ch and prev_attr == attr:
                continue
            try:
                self.stdscr.addch(y, col, ch, attr)
            except curses.error:
                pass
            self.prev[y][col] = (ch, attr)


class Theme:
    def __init__(self, stdscr: "curses._CursesWindow") -> None:
        self.enabled = curses.has_colors()
        if self.enabled:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_GREEN, -1)
            curses.init_pair(3, curses.COLOR_YELLOW, -1)
            curses.init_pair(4, curses.COLOR_RED, -1)
            curses.init_pair(5, curses.COLOR_MAGENTA, -1)
            curses.init_pair(6, curses.COLOR_BLUE, -1)
            curses.init_pair(7, curses.COLOR_WHITE, -1)

        self.header = curses.color_pair(1) if self.enabled else 0
        self.good = curses.color_pair(2) if self.enabled else 0
        self.warn = curses.color_pair(3) if self.enabled else 0
        self.bad = curses.color_pair(4) if self.enabled else 0
        self.accent = curses.color_pair(5) if self.enabled else 0
        self.graph = curses.color_pair(6) if self.enabled else 0
        self.dim = curses.color_pair(7) if self.enabled else 0


def _hline(width: int, left: str = "├", mid: str = "─", right: str = "┤") -> str:
    inner = mid * max(0, width - 2)
    return f"{left}{inner}{right}"


def _box_top(width: int, title: str, timestamp: str) -> str:
    inner = max(0, width - 2 - len(title) - len(timestamp))
    return "┌" + title + "─" * inner + timestamp + "┐"


def _box_bottom(width: int, footer: str) -> str:
    inner = max(0, width - 2)
    text = footer.center(inner)[:inner].ljust(inner)
    return "└" + text + "┘"


def _inner_line(content: str, width: int, center: bool = False) -> str:
    """Box row padded/truncated to exactly width."""
    inner_w = max(0, width - 2)
    text = content.strip()
    if center:
        text = text.center(inner_w)
    else:
        text = text.ljust(inner_w)
    return "│" + text[:inner_w] + "│"


def _source_label(data_source: str) -> str:
    cfg = _mqtt_settings()
    topic = cfg["base_topic"]
    if data_source == "mqtt":
        return f"MQTT {topic}/#"
    if data_source == "artifacts":
        paths = TelemetryStore.load().paths
        return f"files {paths.base_dir}/"
    return "unavailable"


def build_layout(
    data: Dict[str, Any],
    histories: Histories,
    width: int,
    height: int,
    interval: float,
    use_fahrenheit: bool = False,
    data_source: str = "mqtt",
    loading: bool = False,
) -> List[Tuple[int, str, int]]:
    """Return list of (row, text, attr) for the current frame."""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    lines: List[Tuple[int, str, int]] = []
    w = max(40, width)

    def add(y: int, text: str, attr: int = 0) -> None:
        if y < height:
            lines.append((y, text[:w], attr))

    title = " EPEVER Tracer BN Dashboard "
    ts = f" {now} "
    add(0, _box_top(w, title, ts), 0)

    source_label = _source_label(data_source)

    if loading:
        add(2, _inner_line("Reading from MQTT…", w, center=True), 0)
        add(4, _inner_line(source_label, w, center=True), 0)
        add(height - 2, _box_bottom(w, "press q to quit"), 0)
        return lines

    if not is_charger_reachable(data):
        add(2, _inner_line("Charger unreachable", w, center=True), 0)
        add(4, _inner_line("Check solartracer-mqtt.service and RS-485 wiring", w), 0)
        add(6, _inner_line(source_label, w), 0)
        add(7, _inner_line(f"Retrying every {interval:.0f}s — press q to quit", w, center=True), 0)
        add(height - 2, _box_bottom(w, "press q to quit"), 0)
        return lines

    if data_source == "artifacts":
        add(1, _inner_line(" Stale snapshot from local history files ", w, center=True), 0)

    pv_w = _pv_power_w(data)
    bat_w = _battery_power_w(data)
    rated_pv_w = float(data.get("rated_pv_voltage") or 0) * float(data.get("rated_pv_current") or 0)
    rated_chg_a = float(data.get("rated_charge_current") or 1)
    bat_v = float(data.get("battery_voltage") or 0)
    bat_a = float(data.get("battery_current") or 0)
    load_a = float(data.get("load_current") or 0)
    gen_kwh = float(data.get("generated_energy_today") or 0)
    use_kwh = float(data.get("consumed_energy_today") or 0)

    bat_pct = _battery_level_pct(data)
    pv_pct = _pct_of(pv_w, rated_pv_w or 9000.0)
    chg_pct = _pct_of(abs(bat_a), rated_chg_a)
    load_pct = _pct_of(load_a, rated_chg_a)
    gen_pct = _pct_of(gen_kwh, max(gen_kwh, 5.0))
    lights_pct = load_pct

    gauge_w = max(12, min(24, (w - 44) // 2))
    graph_w = max(14, w - gauge_w - 36)
    gen_hi = max(5.0, gen_kwh * 1.1, histories.generated_kwh.recent_range()[1] * 1.05)

    status = compute_display_status(data, histories.battery_voltage_timed)
    daynight = _day_night_label(data)
    lights = _lights_label(data, compact=True)
    time_remaining, _ = display_time_remaining(
        data, histories.battery_voltage_timed, histories.battery_rate_log,
    )
    charging_external = status == "Charging Externally"

    add(1, _hline(w), 0)
    add(2, _inner_line(
        f" Status: {status:<18}   {daynight:<5}   Lights: {lights:<12}   "
        f"Time: {time_remaining:<32} ",
        w,
    ), 0)
    add(3, _hline(w), 0)

    row = 5
    sections = [
        (
            "Battery",
            f"{bat_v:.2f} V",
            bat_pct,
            render_timeline(histories.battery_level, graph_w, 0.0, 100.0),
            f"{bat_a:+.2f} A  {bat_w:.0f} W",
        ),
        (
            "PV Power",
            f"{pv_w:.0f} W",
            pv_pct,
            render_timeline(histories.pv_power, graph_w, 0.0, rated_pv_w or 9000.0),
            f"{_fmt(data.get('pv_voltage'))} V × {_fmt(data.get('pv_current'))} A",
        ),
        (
            "Charge I",
            f"{abs(bat_a):.2f} A",
            chg_pct,
            render_timeline(histories.battery_current, graph_w, 0.0, rated_chg_a),
            "external" if charging_external else ("charging" if tracer_is_charging(data) else "idle"),
        ),
        (
            "Load",
            f"{load_a:.2f} A",
            load_pct,
            render_timeline(histories.load_current, graph_w, 0.0, rated_chg_a),
            f"{_fmt(data.get('load_current'))} A draw",
        ),
        (
            "Gen today",
            f"{gen_kwh:.2f} kWh",
            gen_pct,
            render_timeline(histories.generated_kwh, graph_w, 0.0, gen_hi),
            f"use {_fmt(data.get('consumed_energy_today'))} kWh",
        ),
        (
            "Lights",
            _lights_row_value(data),
            lights_pct,
            render_timeline(histories.load_current, graph_w, 0.0, rated_chg_a),
            f"{load_a:.2f} A relay",
        ),
    ]

    for label, value, pct, spark, detail in sections:
        gauge = render_gauge(pct, gauge_w)
        core = f" {label:<10} {value:<10} [{gauge}] {pct:5.1f}%  {spark}  {detail} "
        add(row, _inner_line(core, w), 0)
        row += 1
        if row >= height - 8:
            break

    add(row, _hline(w), 0)
    row += 1

    vmin = _fmt(data.get("min_battery_voltage_today"))
    vmax = _fmt(data.get("max_battery_voltage_today"))
    pvmax = _fmt(data.get("max_pv_voltage_today"))
    add(row, _inner_line(
        f" Today  gen {gen_kwh:.2f} kWh  use {use_kwh:.2f} kWh  "
        f"bat {vmin}–{vmax} V  pv max {pvmax} V ",
        w,
    ), 0)
    row += 1

    temps = (
        f"bat {_format_temp(data.get('battery_temp'), use_fahrenheit)}  "
        f"chg {_format_temp(data.get('charger_temp'), use_fahrenheit)}  "
        f"pwr {_format_temp(data.get('power_temp'), use_fahrenheit)}"
    )
    add(row, _inner_line(f" Temp   {temps} ", w), 0)
    row += 1

    add(row, _hline(w), 0)
    row += 1
    for cfg_line in _config_lines(data, w, source_label):
        if row >= height - 3:
            break
        add(row, cfg_line, 0)
        row += 1
    row += 1

    footer = f"refresh {interval:.0f}s │ space F/C │ enter lights (MQTT) │ q quit"
    add(height - 2, _box_bottom(w, footer), 0)

    return lines


def render_plain_snapshot(
    data: Dict[str, Any],
    data_source: str = "mqtt",
) -> str:
    """Non-TUI fallback for --once."""
    histories = Histories()
    histories.reload_artifacts()
    histories.update(data)
    lines = build_layout(
        data, histories, width=80, height=28,
        interval=DEFAULT_INTERVAL, use_fahrenheit=False,
        data_source=data_source,
    )
    return "\n".join(text for _, text, _ in sorted(lines, key=lambda t: t[0]))


class DashboardApp:
    def __init__(self, interval: float) -> None:
        self.interval = max(1.0, interval)
        self.histories = Histories()
        self.use_fahrenheit = False
        self.data_source = "mqtt"

    def toggle_lights(
        self,
        data: Dict[str, Any],
        mqtt_cache: Optional[MqttStateCache] = None,
    ) -> None:
        next_state = cycle_lights_via_mqtt(data)
        if mqtt_cache is not None:
            mqtt_cache.patch(_lights_mode_patch(next_state))
        time.sleep(1.5)

    def fetch_data(self, mqtt_cache: Optional[MqttStateCache] = None) -> Dict[str, Any]:
        self.histories.reload_artifacts()
        data, self.data_source = fetch_charger_data(mqtt_cache)
        return data

    def _draw_frame(
        self,
        stdscr: "curses._CursesWindow",
        screen: "PartialScreen",
        theme: "Theme",
        data: Dict[str, Any],
        h: int,
        w: int,
        *,
        loading: bool = False,
    ) -> None:
        frame = build_layout(
            data, self.histories, w, h, self.interval,
            use_fahrenheit=self.use_fahrenheit,
            data_source=self.data_source,
            loading=loading,
        )
        for y, text, _attr in frame:
            attr = 0
            if "Dashboard" in text:
                attr = theme.header
            elif loading:
                attr = theme.dim
            elif "unreachable" in text.lower():
                attr = theme.bad
            elif "Stale snapshot" in text:
                attr = theme.warn
            elif "█" in text and "%" in text:
                attr = theme.good if "Battery" in text or "PV" in text else theme.accent
            elif any(c in text for c in SPARK):
                attr = theme.graph
            elif "Charging Externally" in text:
                attr = theme.warn
            elif "Discharging" in text and "│ Status" in text:
                attr = theme.accent
            elif "Voltage Falling" in text or "Voltage Rising" in text:
                attr = theme.warn
            elif text.startswith("│ Status"):
                status = compute_display_status(data, self.histories.battery_voltage_timed)
                if tracer_is_charging(data):
                    attr = theme.good
                elif status in ("TBD", "Idle"):
                    attr = theme.dim
                else:
                    attr = theme.warn
            screen.put_line(y, 0, text, attr)

        drawn_rows = {y for y, _, _ in frame}
        for y in range(h):
            if y not in drawn_rows:
                screen.put_line(y, 0, "")

        stdscr.noutrefresh()
        curses.doupdate()

    def run(self, stdscr: "curses._CursesWindow") -> None:
        curses.curs_set(0)
        stdscr.nodelay(False)
        stdscr.timeout(int(self.interval * 1000))
        theme = Theme(stdscr)
        screen = PartialScreen(stdscr)
        mqtt_cache = MqttStateCache()

        try:
            h, w = stdscr.getmaxyx()
            if mqtt_cache.start() and h >= 12 and w >= 50:
                screen.resize(h, w)
                self._draw_frame(stdscr, screen, theme, {}, h, w, loading=True)

            while True:
                h, w = stdscr.getmaxyx()
                if h < 12 or w < 50:
                    stdscr.clear()
                    stdscr.addstr(0, 0, "Terminal too small (need at least 50×12).")
                    stdscr.refresh()
                    if stdscr.getch() in (ord("q"), ord("Q"), 27):
                        break
                    continue

                if screen.height != h or screen.width != w:
                    screen.resize(h, w)
                    stdscr.clear()
                    screen.prev = [[(" ", 0)] * w for _ in range(h)]

                data = self.fetch_data(mqtt_cache)
                self.histories.update(data)
                self._draw_frame(stdscr, screen, theme, data, h, w)

                ch = stdscr.getch()
                if ch in (ord("q"), ord("Q"), 27):
                    break
                if ch == ord(" "):
                    self.use_fahrenheit = not self.use_fahrenheit
                    continue
                if ch in (10, 13, curses.KEY_ENTER):
                    try:
                        self.toggle_lights(data, mqtt_cache)
                    except Exception:
                        pass
                    continue
        finally:
            mqtt_cache.stop()


def run_tui(interval: float) -> int:
    try:
        curses.wrapper(lambda stdscr: DashboardApp(interval).run(stdscr))
    except KeyboardInterrupt:
        pass
    return 0


def run_once() -> int:
    cfg = _mqtt_settings()
    print(f"Reading from MQTT ({cfg['base_topic']}/#)…", flush=True)
    data, source = fetch_charger_data()
    if source == "artifacts":
        print("(fallback: local history files — MQTT unavailable)", flush=True)
    print(render_plain_snapshot(data, data_source=source))
    return 0 if is_charger_reachable(data) else 1


def main(argv: Optional[Tuple[str, ...]] = None) -> int:
    _load_env()
    parser = argparse.ArgumentParser(
        description="MQTT-backed dashboard for the EPEVER/Tracer BN solar charger.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Print a single plain-text snapshot and exit (no full-screen TUI)",
    )
    parser.add_argument(
        "-i", "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        metavar="SEC",
        help="Refresh interval in seconds (default: 5)",
    )
    args = parser.parse_args(argv)

    if args.once or not sys.stdout.isatty():
        return run_once()
    return run_tui(args.interval)


if __name__ == "__main__":
    sys.exit(main())