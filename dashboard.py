#!/usr/bin/env python3
"""
dashboard.py

Full-screen terminal dashboard for the EPEVER/Tracer BN solar charger.
Uses partial screen updates, sparkline graphs, and percentage gauges.

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
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore

from solar_data import (
    TimedMetricHistory,
    compute_battery_level_pct,
    compute_display_status,
    display_time_remaining,
    cycle_lights_state,
    get_lights_state,
    get_solar_data,
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
CONFIG_REFRESH_SEC = 60.0


def load_serial_device() -> str:
    if load_dotenv is not None:
        load_dotenv()
    return os.getenv("SERIAL_DEVICE", "/dev/ttyACM0")


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


def _config_lines(data: Dict[str, Any], width: int, device: str) -> List[str]:
    """Format read-only charger configuration for the dashboard."""
    cfg = data.get("config") or {}
    if not cfg or not any(v is not None for k, v in cfg.items() if not k.endswith("_name")):
        return [
            _inner_line(" Config unavailable ", width),
            _inner_line(f" Device  {device} ", width),
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
        _inner_line(f" Device  {device} ", width),
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


def _lights_gauge_pct(data: Dict[str, Any]) -> float:
    state = data.get("lights_state") or get_lights_state(data)
    if state == "on":
        return 100.0
    if state == "off":
        return 0.0
    return 100.0 if data.get("lights_on") else 0.0


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

    def update(self, data: Dict[str, Any]) -> None:
        self.pv_power.add(_pv_power_w(data))
        self.battery_level.add(_battery_level_pct(data))
        self.telemetry.voltage_history.add(data.get("battery_voltage"))
        self.telemetry.record_charger_sample(data)
        self.battery_current.add(abs(data.get("battery_current") or 0.0))
        self.load_current.add(data.get("load_current"))
        self.generated_kwh.add(data.get("generated_energy_today"))
        self.telemetry.persist_quiet()


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


def build_layout(
    data: Dict[str, Any],
    device: str,
    histories: Histories,
    width: int,
    height: int,
    interval: float,
    use_fahrenheit: bool = False,
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

    if not is_charger_reachable(data):
        add(2, _inner_line("Charger unreachable — no RS-485 response", w, center=True), 0)
        add(5, _inner_line("Check USB adapter, wiring, and .env SERIAL_DEVICE", w), 0)
        add(7, _inner_line(f"Device: {device}", w), 0)
        add(8, _inner_line(f"Retrying every {interval:.0f}s — press q to quit", w, center=True), 0)
        add(height - 2, _box_bottom(w, "press q to quit"), 0)
        return lines

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
    lights_pct = _lights_gauge_pct(data)

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
            _lights_label(data),
            lights_pct,
            render_timeline(histories.load_current, graph_w, 0.0, rated_chg_a),
            "relay output",
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
    for cfg_line in _config_lines(data, w, device):
        if row >= height - 3:
            break
        add(row, cfg_line, 0)
        row += 1
    row += 1

    footer = f"refresh {interval:.0f}s │ space F/C │ enter lights │ q quit"
    add(height - 2, _box_bottom(w, footer), 0)

    return lines


def render_plain_snapshot(data: Dict[str, Any], device: str) -> str:
    """Non-TUI fallback for --once."""
    histories = Histories()
    histories.update(data)
    lines = build_layout(
        data, device, histories, width=80, height=28,
        interval=DEFAULT_INTERVAL, use_fahrenheit=False,
    )
    return "\n".join(text for _, text, _ in sorted(lines, key=lambda t: t[0]))


class DashboardApp:
    def __init__(self, device: str, interval: float) -> None:
        self.device = device
        self.interval = max(1.0, interval)
        self.histories = Histories()
        self.cached_config: Dict[str, Any] = {}
        self.last_config_read = 0.0
        self.use_fahrenheit = False

    def _config_has_values(self, config: Dict[str, Any]) -> bool:
        return any(v is not None for k, v in config.items() if not k.endswith("_name"))

    def toggle_lights(self, data: Dict[str, Any]) -> None:
        cycle_lights_state(device=self.device, data=data, quiet=True)
        time.sleep(0.7)

    def fetch_data(self) -> Dict[str, Any]:
        now = time.time()
        need_config = (
            not self.cached_config
            or (now - self.last_config_read) >= CONFIG_REFRESH_SEC
        )
        data = get_solar_data(device=self.device, include_config=need_config)
        if need_config and data.get("config") and self._config_has_values(data["config"]):
            self.cached_config = data["config"]
            self.last_config_read = now
        if self.cached_config:
            data["config"] = self.cached_config
        data["battery_level_pct"] = compute_battery_level_pct(data, data.get("config"))
        return data

    def run(self, stdscr: "curses._CursesWindow") -> None:
        curses.curs_set(0)
        stdscr.nodelay(False)
        stdscr.timeout(int(self.interval * 1000))
        theme = Theme(stdscr)
        screen = PartialScreen(stdscr)

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

            data = self.fetch_data()
            self.histories.update(data)
            frame = build_layout(
                data, self.device, self.histories, w, h, self.interval,
                use_fahrenheit=self.use_fahrenheit,
            )

            for y, text, _attr in frame:
                attr = 0
                if "Dashboard" in text:
                    attr = theme.header
                elif "unreachable" in text.lower():
                    attr = theme.bad
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

            ch = stdscr.getch()
            if ch in (ord("q"), ord("Q"), 27):
                break
            if ch == ord(" "):
                self.use_fahrenheit = not self.use_fahrenheit
                continue
            if ch in (10, 13, curses.KEY_ENTER):
                try:
                    self.toggle_lights(data)
                except Exception:
                    pass
                continue


def run_tui(device: str, interval: float) -> int:
    try:
        curses.wrapper(lambda stdscr: DashboardApp(device, interval).run(stdscr))
    except KeyboardInterrupt:
        pass
    return 0


def run_once(device: str) -> int:
    data = get_solar_data(device=device, include_config=True)
    print(render_plain_snapshot(data, device))
    return 0 if is_charger_reachable(data) else 1


def main(argv: Optional[Tuple[str, ...]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Full-screen dashboard for the EPEVER/Tracer BN solar charger.",
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
    parser.add_argument(
        "-d", "--device",
        default=None,
        help="Serial device (default: SERIAL_DEVICE from .env or /dev/ttyACM0)",
    )
    args = parser.parse_args(argv)

    device = args.device or load_serial_device()

    if args.once or not sys.stdout.isatty():
        return run_once(device)
    return run_tui(device, args.interval)


if __name__ == "__main__":
    sys.exit(main())