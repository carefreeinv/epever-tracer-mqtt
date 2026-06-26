#!/usr/bin/env python3
"""
solar_data.py

Shared logic for reading data from EPEVER/Tracer BN solar controllers
over RS-485 using minimalmodbus.

Usage:
    from solar_data import get_solar_data, setup_instrument
    data = get_solar_data(device="/dev/ttyACM0")
"""

import fcntl
import json
import os
import time
import datetime
import minimalmodbus

from storage import atomic_write_json, read_json_file
from collections import deque
from contextlib import contextmanager
from typing import Any, Deque, Dict, Iterator, List, Optional, Tuple, Union

CHARGE_AMP_THRESHOLD = 0.05
VOLTAGE_TREND_THRESHOLD = 0.03
STATUS_WINDOW_SEC = 180.0
STATUS_MIN_SPAN_SEC = 150.0
HISTORY_SAMPLE_INTERVAL = 5.0
# Short lookback used to override stale charge trends after discharge starts.
RECENT_TREND_LOOKBACK_SEC = 90.0
RECENT_TREND_MIN_SPAN_SEC = 30.0
DISCHARGE_OVERRIDE_SEC = 120.0

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
    (0x311A, 0, "battery_soc", "Battery SOC", "%", "battery", "measurement"),
    (0x311B, 2, "battery_temp", "Battery Temperature", "°C", "temperature", "measurement"),
    (0x311D, 2, "system_rated_voltage", "System Rated Voltage", "V", "voltage", "measurement"),
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

# Battery/charger configuration (holding registers, function code 3, read-only in app)
CONFIG_REGISTERS = [
    (0x9000, 0, "battery_type"),
    (0x9001, 0, "battery_capacity_ah"),
    (0x9004, 2, "charging_limit_voltage"),
    (0x9006, 2, "equalization_voltage"),
    (0x9007, 2, "boost_voltage"),
    (0x9008, 2, "float_voltage"),
    (0x9009, 2, "boost_reconnect_voltage"),
    (0x900A, 2, "low_voltage_reconnect"),
    (0x900B, 2, "under_voltage_recover"),
    (0x900C, 2, "under_voltage_warning"),
    (0x900D, 2, "low_voltage_disconnect"),
    (0x900E, 2, "discharging_limit_voltage"),
    (0x9067, 0, "battery_rated_voltage_code"),
    (0x906D, 0, "discharging_percentage"),
    (0x906E, 0, "charging_percentage"),
    (0x9070, 0, "management_mode"),
    (0x901E, 2, "night_time_threshold_voltage"),
    (0x9020, 2, "day_time_threshold_voltage"),
]


def derive_is_night(
    pv_voltage: Optional[float],
    night_threshold: Optional[float],
    day_threshold: Optional[float],
) -> Optional[bool]:
    """Derive day/night from PV voltage vs charger NTTV/DTTV settings.

    Matches the Tracer Light ON/OFF logic (without delay timers):
      PV <= NTTV → night
      PV >= DTTV → day
      between    → unknown (twilight band)
    """
    if pv_voltage is None or night_threshold is None or day_threshold is None:
        return None
    if day_threshold <= night_threshold:
        return pv_voltage <= night_threshold
    if pv_voltage <= night_threshold:
        return True
    if pv_voltage >= day_threshold:
        return False
    return None


class TimedMetricHistory:
    """Timestamped samples for trend detection over a fixed time window."""

    def __init__(
        self,
        window_sec: float = STATUS_WINDOW_SEC,
        min_span_sec: float = STATUS_MIN_SPAN_SEC,
    ) -> None:
        self.window_sec = window_sec
        self.min_span_sec = min_span_sec
        self.samples: Deque[Tuple[float, float]] = deque()

    def add(self, value: Optional[float], when: Optional[float] = None) -> None:
        if value is None:
            return
        now = when if when is not None else time.time()
        self.samples.append((now, float(value)))
        self._prune(now)

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_sec
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()

    def trend(self) -> Optional[str]:
        """Return up, down, flat, or None when the window is too short."""
        return self._trend_from_samples(list(self.samples), self.min_span_sec)

    def recent_trend(
        self,
        lookback_sec: float = RECENT_TREND_LOOKBACK_SEC,
        min_span_sec: float = RECENT_TREND_MIN_SPAN_SEC,
    ) -> Optional[str]:
        """Faster trend over a short recent window (reacts before the main window turns)."""
        if len(self.samples) < 2:
            return None
        newest_t = max(sample[0] for sample in self.samples)
        cutoff = newest_t - lookback_sec
        window = [(t, v) for t, v in self.samples if t >= cutoff]
        return self._trend_from_samples(window, min_span_sec)

    @staticmethod
    def _trend_from_samples(
        window: List[Tuple[float, float]],
        min_span_sec: float,
    ) -> Optional[str]:
        if len(window) < 2:
            return None
        oldest = min(window, key=lambda sample: sample[0])
        newest = max(window, key=lambda sample: sample[0])
        if newest[0] - oldest[0] < min_span_sec:
            return None
        delta = newest[1] - oldest[1]
        if delta >= VOLTAGE_TREND_THRESHOLD:
            return "up"
        if delta <= -VOLTAGE_TREND_THRESHOLD:
            return "down"
        return "flat"

    def span_sec(self) -> float:
        """Seconds between earliest and latest sample timestamps."""
        if len(self.samples) < 2:
            return 0.0
        times = [sample[0] for sample in self.samples]
        return max(times) - min(times)

    def seconds_until_estimate_ready(self) -> float:
        """Seconds until the trend window is long enough for time estimates."""
        if len(self.samples) < 2:
            return self.min_span_sec
        return max(0.0, self.min_span_sec - self.span_sec())

    def load_samples(self, samples: Any) -> None:
        """Restore timestamped samples from JSON-friendly [[t, v], ...] data."""
        self.samples.clear()
        if not isinstance(samples, list):
            return
        for item in samples:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            try:
                self.add(float(item[1]), when=float(item[0]))
            except (TypeError, ValueError):
                continue

    def save_to(self, path: str) -> None:
        atomic_write_json(path, [[t, v] for t, v in self.samples])

    @classmethod
    def load_from(cls, path: str, **kwargs: Any) -> "TimedMetricHistory":
        history = cls(**kwargs)
        payload = read_json_file(path)
        if payload is not None:
            history.load_samples(payload)
        return history


BATTERY_LOG_MAX_AGE_SEC = 172800.0  # 48h raw samples
BATTERY_LOG_MIN_DAY_SPAN_SEC = 1800.0  # 30m minimum for prior-day rollup
# Preferred lookback windows for empirical rate (seconds, min span ratio).
BATTERY_LOOKBACKS_SEC: Tuple[Tuple[int, float], ...] = (
    (600, 0.80),   # 10m — typical 27→28 V window
    (180, 0.80),   # 3m
    (1800, 0.80),  # 30m
    (3600, 0.80),  # 60m
)
MIN_SESSION_VOLTAGE_DELTA = 0.02
MIN_SESSION_SOC_DELTA = 0.5


def _local_day_bounds(day: datetime.date) -> Tuple[float, float]:
    start = datetime.datetime.combine(day, datetime.time.min)
    end = start + datetime.timedelta(days=1)
    return start.timestamp(), end.timestamp()


class BatteryRateLog:
    """Charge/discharge log with 3/10/30/60m lookbacks and prior-day grounding."""

    def __init__(self, max_age_sec: float = BATTERY_LOG_MAX_AGE_SEC) -> None:
        self.max_age_sec = max_age_sec
        self.samples: Deque[Tuple[float, float, Optional[float], str]] = deque()
        self.daily_stats: Dict[str, Dict[str, float]] = {}
        self.last_roll_date: Optional[str] = None

    def add(
        self,
        voltage: Optional[float],
        soc: Optional[float] = None,
        *,
        mode: str = "charge",
        when: Optional[float] = None,
    ) -> None:
        if voltage is None or mode not in ("charge", "discharge"):
            return
        now = when if when is not None else time.time()
        soc_val = float(soc) if soc is not None else None
        self.samples.append((now, float(voltage), soc_val, mode))
        self._prune(now)
        self._maybe_roll_daily_stats(now)

    def _prune(self, now: float) -> None:
        cutoff = now - self.max_age_sec
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()
        keep_after = (
            datetime.date.today() - datetime.timedelta(days=2)
        ).isoformat()
        for day in list(self.daily_stats.keys()):
            if day < keep_after:
                del self.daily_stats[day]

    def _window(
        self,
        lookback_sec: float,
        mode: str,
    ) -> List[Tuple[float, float, Optional[float], str]]:
        if not self.samples:
            return []
        now = self.samples[-1][0]
        cutoff = now - lookback_sec
        return [
            sample
            for sample in self.samples
            if sample[0] >= cutoff and sample[3] == mode
        ]

    def _v_per_h(
        self,
        lookback_sec: float,
        min_span_ratio: float,
        mode: str,
    ) -> Optional[float]:
        window = self._window(lookback_sec, mode)
        if len(window) < 2:
            return None
        t0, v0, _, _ = window[0]
        t1, v1, _, _ = window[-1]
        span = t1 - t0
        if span < lookback_sec * min_span_ratio:
            return None
        delta = v1 - v0
        if mode == "charge":
            if delta < MIN_SESSION_VOLTAGE_DELTA:
                return None
            return delta / (span / 3600.0)
        if -delta < MIN_SESSION_VOLTAGE_DELTA:
            return None
        return (-delta) / (span / 3600.0)

    def _soc_per_h(
        self,
        lookback_sec: float,
        min_span_ratio: float,
        mode: str,
    ) -> Optional[float]:
        window = [(t, soc) for t, _, soc, m in self._window(lookback_sec, mode) if soc is not None]
        if len(window) < 2:
            return None
        t0, soc0 = window[0]
        t1, soc1 = window[-1]
        span = t1 - t0
        if span < lookback_sec * min_span_ratio:
            return None
        if mode == "charge":
            if soc1 - soc0 < MIN_SESSION_SOC_DELTA:
                return None
            return (soc1 - soc0) / (span / 3600.0)
        if soc0 - soc1 < MIN_SESSION_SOC_DELTA:
            return None
        return (soc0 - soc1) / (span / 3600.0)

    def _compute_day_stats(self, day: datetime.date) -> Dict[str, float]:
        start_ts, end_ts = _local_day_bounds(day)
        day_samples = [s for s in self.samples if start_ts <= s[0] < end_ts]
        stats: Dict[str, float] = {}

        for mode, v_key, soc_key in (
            ("charge", "charge_v_per_h", "charge_soc_per_h"),
            ("discharge", "discharge_v_per_h", "discharge_soc_per_h"),
        ):
            mode_samples = [s for s in day_samples if s[3] == mode]
            if len(mode_samples) < 2:
                continue
            t0, v0, soc0, _ = mode_samples[0]
            t1, v1, soc1, _ = mode_samples[-1]
            span = t1 - t0
            if span < BATTERY_LOG_MIN_DAY_SPAN_SEC:
                continue
            hours = span / 3600.0
            if mode == "charge" and v1 - v0 >= MIN_SESSION_VOLTAGE_DELTA:
                stats[v_key] = (v1 - v0) / hours
            if mode == "discharge" and v0 - v1 >= MIN_SESSION_VOLTAGE_DELTA:
                stats[v_key] = (v0 - v1) / hours
            if soc0 is not None and soc1 is not None:
                if mode == "charge" and soc1 - soc0 >= MIN_SESSION_SOC_DELTA:
                    stats[soc_key] = (soc1 - soc0) / hours
                if mode == "discharge" and soc0 - soc1 >= MIN_SESSION_SOC_DELTA:
                    stats[soc_key] = (soc0 - soc1) / hours
        return stats

    def _maybe_roll_daily_stats(self, now: float) -> None:
        today = datetime.date.fromtimestamp(now).isoformat()
        if self.last_roll_date == today:
            return
        if self.last_roll_date is not None:
            prev_day = datetime.date.fromtimestamp(now) - datetime.timedelta(days=1)
            stats = self._compute_day_stats(prev_day)
            if stats:
                self.daily_stats[prev_day.isoformat()] = stats
        self.last_roll_date = today

    def prior_day_stats(self) -> Dict[str, float]:
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        return dict(self.daily_stats.get(yesterday, {}))

    def prior_day_stat(self, key: str) -> Optional[float]:
        value = self.prior_day_stats().get(key)
        return float(value) if value is not None else None

    def best_v_per_h(self, mode: str = "charge") -> Tuple[Optional[float], Optional[int]]:
        for lookback, ratio in BATTERY_LOOKBACKS_SEC:
            rate = self._v_per_h(lookback, ratio, mode)
            if rate is not None and rate > 0.01:
                return rate, lookback
        prior_key = f"{mode}_v_per_h"
        prior = self.prior_day_stat(prior_key)
        if prior is not None and prior > 0.01:
            return prior, None
        return None, None

    def best_soc_per_h(self, mode: str = "charge") -> Tuple[Optional[float], Optional[int]]:
        for lookback, ratio in BATTERY_LOOKBACKS_SEC:
            rate = self._soc_per_h(lookback, ratio, mode)
            if rate is not None and rate > 0.05:
                return rate, lookback
        prior_key = f"{mode}_soc_per_h"
        prior = self.prior_day_stat(prior_key)
        if prior is not None and prior > 0.05:
            return prior, None
        return None, None

    def recent_session_mode(self, max_age_sec: float = DISCHARGE_OVERRIDE_SEC) -> Optional[str]:
        """Mode of the latest rate-log sample if it falls within *max_age_sec*."""
        if not self.samples:
            return None
        now = time.time()
        sample_t, _, _, mode = self.samples[-1]
        if now - sample_t > max_age_sec:
            return None
        return mode

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": 2,
            "last_roll_date": self.last_roll_date,
            "daily_stats": self.daily_stats,
            "samples": [[t, v, soc, mode] for t, v, soc, mode in self.samples],
        }

    def load_dict(self, payload: Any) -> None:
        self.samples.clear()
        self.daily_stats = {}
        self.last_roll_date = None
        if not isinstance(payload, dict):
            self._load_legacy_samples(payload)
            return
        self.last_roll_date = payload.get("last_roll_date")
        stats = payload.get("daily_stats")
        if isinstance(stats, dict):
            self.daily_stats = {
                str(day): {str(k): float(v) for k, v in day_stats.items()}
                for day, day_stats in stats.items()
                if isinstance(day_stats, dict)
            }
        samples = payload.get("samples")
        if isinstance(samples, list):
            self._load_legacy_samples(samples)

    def _load_legacy_samples(self, samples: Any) -> None:
        if not isinstance(samples, list):
            return
        for item in samples:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            try:
                t = float(item[0])
                v = float(item[1])
                soc = float(item[2]) if len(item) > 2 and item[2] is not None else None
                mode = str(item[3]) if len(item) > 3 else "charge"
                if mode not in ("charge", "discharge"):
                    mode = "charge"
                self.add(v, soc=soc, mode=mode, when=t)
            except (TypeError, ValueError):
                continue

    def save_to(self, path: str) -> None:
        self._maybe_roll_daily_stats(time.time())
        atomic_write_json(path, self.to_dict())

    @classmethod
    def load_from(cls, path: str) -> "BatteryRateLog":
        log = cls()
        payload = read_json_file(path)
        if payload is not None:
            log.load_dict(payload)
        return log


# Backwards-compatible alias
ChargingRateHistory = BatteryRateLog


def discharge_recently_active(
    data: Dict[str, Any],
    voltage_history: TimedMetricHistory,
    rate_log: Optional[BatteryRateLog] = None,
) -> bool:
    """True when discharge should override stale charge / until-full estimates."""
    if discharge_amps(data) >= CHARGE_AMP_THRESHOLD:
        return True
    if rate_log is not None and rate_log.recent_session_mode() == "discharge":
        return True
    if not tracer_is_charging(data) and voltage_history.recent_trend() == "down":
        return True
    return False


def is_charging_session(
    data: Dict[str, Any],
    voltage_history: TimedMetricHistory,
    rate_log: Optional[BatteryRateLog] = None,
) -> bool:
    """True when the pack is actively charging (Tracer or external)."""
    if discharge_recently_active(data, voltage_history, rate_log):
        return False
    if tracer_is_charging(data):
        return True
    trend = voltage_history.trend()
    if trend == "up" and charge_amps(data) < CHARGE_AMP_THRESHOLD:
        return True
    return compute_display_status(data, voltage_history) in ("Charging", "Charging Externally")


def is_discharging_session(
    data: Dict[str, Any],
    voltage_history: TimedMetricHistory,
) -> bool:
    """True when the pack is actively discharging (measurable current, not drift)."""
    if tracer_is_charging(data):
        return False
    return discharge_amps(data) >= CHARGE_AMP_THRESHOLD


def record_battery_rate_sample(
    rate_log: Optional[BatteryRateLog],
    data: Dict[str, Any],
    voltage_history: TimedMetricHistory,
    *,
    voltage: Optional[float] = None,
) -> None:
    """Append charge/discharge samples while a session is in progress."""
    if rate_log is None:
        return
    pack_v = voltage if voltage is not None else data.get("battery_voltage")
    if pack_v is None:
        return
    soc = data.get("battery_level_pct")
    if soc is None:
        soc = data.get("battery_soc")
    if is_charging_session(data, voltage_history, rate_log):
        rate_log.add(pack_v, soc=soc, mode="charge")
    elif is_discharging_session(data, voltage_history):
        rate_log.add(pack_v, soc=soc, mode="discharge")


def record_charging_sample(
    charging_history: Optional[ChargingRateHistory],
    data: Dict[str, Any],
    voltage_history: TimedMetricHistory,
    *,
    voltage: Optional[float] = None,
) -> None:
    """Backwards-compatible alias for record_battery_rate_sample."""
    record_battery_rate_sample(charging_history, data, voltage_history, voltage=voltage)


def record_voltage_rate_sample(
    rate_log: Optional[BatteryRateLog],
    voltage: float,
    voltage_history: TimedMetricHistory,
) -> None:
    """Lightweight rate log entry from voltage trend alone (no SOC)."""
    if rate_log is None:
        return
    trend = voltage_history.trend()
    if trend == "up":
        rate_log.add(voltage, mode="charge")
    elif trend == "down":
        rate_log.add(voltage, mode="discharge")


def empirical_hours_charging_to_full(
    data: Dict[str, Any],
    charging_history: Optional[ChargingRateHistory],
    float_voltage: Optional[float],
) -> Optional[float]:
    """Hours to float/full using recent and prior-day charge performance."""
    if charging_history is None or float_voltage is None:
        return None

    bat_v = data.get("battery_voltage")
    if bat_v is None:
        return None

    gap = float(float_voltage) - float(bat_v)
    if gap <= 0.05:
        return 0.0

    estimates: List[float] = []

    v_rate, _ = charging_history.best_v_per_h("charge")
    if v_rate is not None:
        estimates.append(gap / v_rate)

    soc = effective_battery_soc_pct(data, data.get("config"))
    soc_rate, _ = charging_history.best_soc_per_h("charge")
    if soc is not None and soc_rate is not None and float(soc) < 99.0:
        estimates.append((100.0 - float(soc)) / soc_rate)

    if not estimates:
        return None
    return min(estimates)


def empirical_hours_discharging_to_empty(
    data: Dict[str, Any],
    rate_log: Optional[BatteryRateLog],
    empty_voltage: Optional[float],
) -> Optional[float]:
    """Hours to empty using recent and prior-day discharge performance."""
    if rate_log is None or empty_voltage is None:
        return None

    bat_v = data.get("battery_voltage")
    if bat_v is None:
        return None

    gap = float(bat_v) - float(empty_voltage)
    if gap <= 0.05:
        return 0.0

    estimates: List[float] = []

    v_rate, _ = rate_log.best_v_per_h("discharge")
    if v_rate is not None:
        estimates.append(gap / v_rate)

    soc = effective_battery_soc_pct(data, data.get("config"))
    soc_rate, _ = rate_log.best_soc_per_h("discharge")
    if soc is not None and soc_rate is not None and float(soc) > 1.0:
        estimates.append(float(soc) / soc_rate)

    if not estimates:
        return None
    return min(estimates)


def read_battery_voltage(device: Optional[str] = None) -> Optional[float]:
    """Read only battery voltage (0x3104) for lightweight trend sampling."""
    if device is None:
        device = os.getenv("SERIAL_DEVICE", "/dev/ttyACM0")
    try:
        with serial_lock(device, timeout=20.0):
            instrument = setup_instrument(device)
            return read_register_safe(instrument, 0x3104, 2)
    except (TimeoutError, OSError) as exc:
        print(f"Battery voltage read failed for {device}: {exc}")
        return None


def charge_amps(data: Dict[str, Any]) -> float:
    return abs(float(data.get("battery_current") or 0.0))


def tracer_is_charging(data: Dict[str, Any]) -> bool:
    """True when this Tracer is delivering measurable charge current."""
    return bool(data.get("is_charging")) and charge_amps(data) >= CHARGE_AMP_THRESHOLD


# Typical LiFePO4 OCV curve (per cell). Wide flat plateau: voltage can sit
# around 3.28-3.32 V/cell (~26.2-26.6 V on 8S) for a large SOC span.
LIFEPO4_CELL_OCV: Tuple[Tuple[float, float], ...] = (
    (2.50, 0.0),
    (2.80, 5.0),
    (3.00, 10.0),
    (3.08, 15.0),
    (3.12, 18.0),
    (3.15, 22.0),
    (3.18, 28.0),
    (3.20, 38.0),
    (3.22, 48.0),
    (3.24, 58.0),
    (3.26, 68.0),
    (3.28, 78.0),
    (3.30, 86.0),
    (3.31, 88.0),
    (3.312, 89.0),
    (3.32, 90.0),
    (3.33, 92.0),
    (3.34, 94.0),
    (3.36, 96.0),
    (3.38, 97.5),
    (3.40, 98.5),
    (3.45, 100.0),
)

# Per-cell voltage band where pack V is unreliable for SOC-rate estimates.
LIFEPO4_PLATEAU_CELL_V: Tuple[float, float] = (3.18, 3.35)

# Mild CC/CV and knee adjustments only — plateau runtime is amp-hour driven.
LIFEPO4_CHARGE_SEGMENTS: Tuple[Tuple[float, float, float], ...] = (
    (0.0, 85.0, 1.0),
    (85.0, 95.0, 1.5),
    (95.0, 100.0, 2.0),
)
LIFEPO4_DISCHARGE_SEGMENTS: Tuple[Tuple[float, float, float], ...] = (
    (10.0, 100.0, 1.0),
    (0.0, 10.0, 1.05),
)

MIN_LIFEPO4_DSOC_DV = 20.0  # discharge slope: only trust steep knee regions
MIN_CHARGE_DSOC_DV_FLOOR = 2.0  # charging slope: floor in flat/plateau regions
PLATEAU_CHARGE_DSOC_DV_FLOOR = 3.0
MAX_CHARGE_VOLTAGE_SLOPE_V_PER_H = 1.5  # cap noisy/fast voltage rises


def _interpolate_curve(points: Tuple[Tuple[float, float], ...], x: float) -> float:
    """Linear interpolation on sorted (x, y) points."""
    if x <= points[0][0]:
        return points[0][1]
    if x >= points[-1][0]:
        return points[-1][1]
    for idx in range(1, len(points)):
        x0, y0 = points[idx - 1]
        x1, y1 = points[idx]
        if x <= x1:
            if x1 == x0:
                return y1
            frac = (x - x0) / (x1 - x0)
            return y0 + frac * (y1 - y0)
    return points[-1][1]


def cell_count_from_data(
    data: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> int:
    """Derive series cell count from system/battery nominal voltage."""
    cfg = config or data.get("config") or {}
    for key in ("system_rated_voltage", "rated_battery_voltage"):
        value = data.get(key) or cfg.get(key)
        if value is None:
            continue
        nominal = float(value)
        if nominal >= 40.0:
            return 16
        if nominal >= 20.0:
            return 8
        if nominal >= 10.0:
            return 4
    return 8


def lifepo4_soc_from_pack_voltage(pack_voltage: float, cell_count: int) -> float:
    """Map pack voltage to SOC% using a typical LiFePO4 OCV curve."""
    cell_v = float(pack_voltage) / cell_count
    return _clamp_pct(_interpolate_curve(LIFEPO4_CELL_OCV, cell_v))


def lifepo4_dsoc_dv_pack(pack_voltage: float, cell_count: int, epsilon: float = 0.05) -> float:
    """Local dSOC/dV for the pack (%SOC per volt) from the LiFePO4 curve."""
    soc_hi = lifepo4_soc_from_pack_voltage(pack_voltage + epsilon, cell_count)
    soc_lo = lifepo4_soc_from_pack_voltage(pack_voltage - epsilon, cell_count)
    return (soc_hi - soc_lo) / (2.0 * epsilon)


def lifepo4_in_plateau(pack_voltage: float, cell_count: int) -> bool:
    """True when pack voltage is in the flat LiFePO4 plateau region."""
    cell_v = float(pack_voltage) / cell_count
    lo, hi = LIFEPO4_PLATEAU_CELL_V
    return lo <= cell_v <= hi


def effective_battery_soc_pct(
    data: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> Optional[float]:
    """Best SOC% estimate: charger register, then LiFePO4 voltage curve."""
    soc = data.get("battery_level_pct")
    if soc is None:
        soc = data.get("battery_soc")
    if soc is not None:
        return float(_clamp_pct(soc))

    voltage = data.get("battery_voltage")
    if voltage is None:
        return None
    cells = cell_count_from_data(data, config)
    return lifepo4_soc_from_pack_voltage(float(voltage), cells)


def lifepo4_charge_hours(soc_start: float, capacity_ah: float, amps: float) -> Optional[float]:
    """Hours to full using LiFePO4 CC/CV-style charge weighting."""
    if amps < CHARGE_AMP_THRESHOLD or capacity_ah <= 0:
        return None
    soc = float(_clamp_pct(soc_start))
    if soc >= 99.5:
        return 0.0

    hours = 0.0
    for lo, hi, factor in LIFEPO4_CHARGE_SEGMENTS:
        if soc >= hi:
            continue
        seg_start = max(soc, lo)
        remaining_ah = capacity_ah * (hi - seg_start) / 100.0
        hours += (remaining_ah / amps) * factor
    return hours


def lifepo4_discharge_hours(soc_start: float, capacity_ah: float, amps: float) -> Optional[float]:
    """Hours to empty using LiFePO4 low-voltage knee weighting."""
    if amps < CHARGE_AMP_THRESHOLD or capacity_ah <= 0:
        return None
    soc = float(_clamp_pct(soc_start))
    if soc <= 0.5:
        return 0.0

    hours = 0.0
    for lo, hi, factor in LIFEPO4_DISCHARGE_SEGMENTS:
        if soc <= lo:
            continue
        seg_top = min(soc, hi)
        usable_ah = capacity_ah * (seg_top - lo) / 100.0
        hours += (usable_ah / amps) * factor
    return hours


def lifepo4_hours_charging_to_full(
    data: Dict[str, Any],
    pack_voltage: float,
    float_voltage: float,
    voltage_slope_v_per_h: float,
    cell_count: int,
    config: Optional[Dict[str, Any]] = None,
) -> Optional[float]:
    """Hours to full from rising voltage (external charging), using charger SOC."""
    if voltage_slope_v_per_h <= 0:
        return None

    soc_now = effective_battery_soc_pct(data, config)
    if soc_now is None:
        soc_now = lifepo4_soc_from_pack_voltage(pack_voltage, cell_count)
    soc_target = max(
        float(soc_now),
        lifepo4_soc_from_pack_voltage(float_voltage, cell_count),
        99.0,
    )
    if float(soc_now) >= 99.0:
        return 0.0

    capped_slope = min(voltage_slope_v_per_h, MAX_CHARGE_VOLTAGE_SLOPE_V_PER_H)
    dsoc_dv = lifepo4_dsoc_dv_pack(pack_voltage, cell_count)
    effective_dsoc_dv = max(abs(dsoc_dv), MIN_CHARGE_DSOC_DV_FLOOR)
    if lifepo4_in_plateau(pack_voltage, cell_count):
        effective_dsoc_dv = max(effective_dsoc_dv, PLATEAU_CHARGE_DSOC_DV_FLOOR)

    soc_rate = capped_slope * effective_dsoc_dv
    if soc_rate <= 0:
        return None
    return (soc_target - float(soc_now)) / soc_rate


def lifepo4_hours_from_voltage_slope(
    pack_voltage: float,
    target_voltage: float,
    voltage_slope_v_per_h: float,
    cell_count: int,
    *,
    toward_full: bool,
) -> Optional[float]:
    """Convert pack V/h trend to time-to-target using the LiFePO4 SOC curve."""
    if voltage_slope_v_per_h == 0:
        return None
    if toward_full:
        return None  # use lifepo4_hours_charging_to_full for charge estimates
    if lifepo4_in_plateau(pack_voltage, cell_count):
        return None

    soc_now = lifepo4_soc_from_pack_voltage(pack_voltage, cell_count)
    soc_target = lifepo4_soc_from_pack_voltage(target_voltage, cell_count)
    dsoc_dv = lifepo4_dsoc_dv_pack(pack_voltage, cell_count)
    if abs(dsoc_dv) < MIN_LIFEPO4_DSOC_DV:
        return None

    soc_rate = voltage_slope_v_per_h * dsoc_dv
    if soc_rate >= 0 or soc_now <= soc_target + 0.5:
        return 0.0 if soc_now <= soc_target + 0.5 else None
    return (soc_now - soc_target) / abs(soc_rate)


def format_estimate_warmup(voltage_history: TimedMetricHistory) -> Optional[str]:
    """Countdown label while the voltage trend window is still warming up."""
    remaining = voltage_history.seconds_until_estimate_ready()
    if remaining <= 0:
        return None
    if remaining < 60:
        secs = int(max(1, round(remaining)))
        return f"{secs}s left until estimate ready"
    total_min = int(max(1, round(remaining / 60)))
    if total_min < 60:
        return f"{total_min}m left until estimate ready"
    hours, minutes = divmod(total_min, 60)
    if minutes:
        return f"{hours}h{minutes:02d}m left until estimate ready"
    return f"{hours}h left until estimate ready"


def format_time_remaining_unavailable(
    data: Dict[str, Any],
    voltage_history: TimedMetricHistory,
) -> str:
    """Human-readable reason when a numeric estimate is not available yet."""
    bat_v = data.get("battery_voltage")
    cfg = data.get("config") or {}
    cells = cell_count_from_data(data, cfg)
    trend = voltage_history.trend()
    status = compute_display_status(data, voltage_history)
    discharge_a = discharge_amps(data)
    charge_a = charge_amps(data)

    if tracer_is_charging(data):
        return "charging — no estimate yet"

    if trend == "up":
        return "voltage rise logged -- no estimate yet"
    if trend == "down":
        return "voltage fall logged -- no estimate yet"

    if status == "Idle":
        if bat_v is not None and lifepo4_in_plateau(float(bat_v), cells):
            return "idle — plateau"
        return "idle"

    if charge_a < CHARGE_AMP_THRESHOLD and discharge_a < CHARGE_AMP_THRESHOLD:
        if bat_v is not None and lifepo4_in_plateau(float(bat_v), cells):
            return "idle — plateau"
        return "idle"

    return "estimate unavailable"


def display_time_remaining(
    data: Dict[str, Any],
    voltage_history: TimedMetricHistory,
    charging_history: Optional[ChargingRateHistory] = None,
) -> Tuple[str, Optional[int]]:
    """Time remaining for UI/MQTT, with warmup countdown when trend data is pending."""
    label, seconds = compute_time_remaining(data, voltage_history, charging_history)
    if label != "TBD":
        return label, seconds
    warmup = format_estimate_warmup(voltage_history)
    if warmup:
        return warmup, None
    return format_time_remaining_unavailable(data, voltage_history), None


def format_time_remaining(
    hours: Optional[float],
    *,
    done_label: str = "full",
) -> str:
    if hours is None:
        return "TBD"
    if hours <= 0 or hours < 1.0 / 60.0:
        return done_label
    total_min = int(round(hours * 60))
    if total_min < 60:
        duration = f"{total_min}m"
    else:
        h, m = divmod(total_min, 60)
        duration = f"{h}h{m:02d}m" if m else f"{h}h"
    suffix = " until full" if done_label == "full" else " until empty"
    return f"{duration}{suffix}"


def _time_remaining_seconds(hours: Optional[float], done_label: str) -> Optional[int]:
    if hours is None:
        return None
    if hours <= 0 or hours < 1.0 / 60.0:
        return 0
    return max(1, int(round(hours * 3600)))


def voltage_slope_v_per_h(history: TimedMetricHistory) -> Optional[float]:
    """Signed V/h from the timed voltage window (positive = rising)."""
    if len(history.samples) < 2:
        return None
    if history.span_sec() < STATUS_MIN_SPAN_SEC:
        return None
    oldest = min(history.samples, key=lambda sample: sample[0])
    newest = max(history.samples, key=lambda sample: sample[0])
    t0, v0 = oldest
    t1, v1 = newest
    dt_h = (t1 - t0) / 3600.0
    if dt_h <= 0:
        return None
    return (v1 - v0) / dt_h


def discharge_amps(data: Dict[str, Any]) -> float:
    """Best estimate of present battery discharge current."""
    load_a = float(data.get("load_current") or 0.0)
    bat_a = float(data.get("battery_current") or 0.0)
    if bat_a < 0:
        return abs(bat_a)
    if not data.get("is_charging"):
        return max(load_a, bat_a)
    return load_a


def compute_display_status(
    data: Dict[str, Any],
    voltage_history: TimedMetricHistory,
) -> str:
    if data.get("battery_voltage") is None:
        return "TBD"

    if tracer_is_charging(data):
        return "Charging"

    discharge_a = discharge_amps(data)
    charge_a = charge_amps(data)
    trend = voltage_history.trend()

    if discharge_a >= CHARGE_AMP_THRESHOLD:
        return "Discharging"

    if trend == "up" and charge_a < CHARGE_AMP_THRESHOLD:
        return "Charging Externally"

    if trend == "down":
        return "Voltage Falling"
    if trend == "up":
        return "Voltage Rising"

    if charge_a < CHARGE_AMP_THRESHOLD and discharge_a < CHARGE_AMP_THRESHOLD:
        return "Idle"

    return "TBD"


def compute_time_remaining(
    data: Dict[str, Any],
    voltage_history: TimedMetricHistory,
    charging_history: Optional[ChargingRateHistory] = None,
) -> Tuple[str, Optional[int]]:
    """Estimate time to full (charging) or empty (discharging).

    Charging uses empirical 3/10/30/60m performance when available, then
    LiFePO4 curve / amp-hour fallbacks.

    Returns (label, seconds) where seconds is None for TBD and 0 for full/empty.
    """
    cfg = data.get("config") or {}
    bat_v = data.get("battery_voltage")
    capacity = cfg.get("battery_capacity_ah")
    float_v = cfg.get("float_voltage")
    empty_v = (
        cfg.get("low_voltage_disconnect")
        or cfg.get("under_voltage_warning")
        or cfg.get("discharging_limit_voltage")
    )
    slope = voltage_slope_v_per_h(voltage_history)
    trend = voltage_history.trend()
    cells = cell_count_from_data(data, cfg)
    soc = effective_battery_soc_pct(data, cfg)
    discharge_a = discharge_amps(data)
    discharge_active = discharge_recently_active(data, voltage_history, charging_history)

    is_discharging = (
        discharge_a >= CHARGE_AMP_THRESHOLD and not tracer_is_charging(data)
    )

    if is_discharging:
        empirical = empirical_hours_discharging_to_empty(data, charging_history, empty_v)
        if empirical is not None:
            return format_time_remaining(empirical, done_label="empty"), _time_remaining_seconds(
                empirical, "empty"
            )

        if soc is not None and capacity is not None and discharge_a >= CHARGE_AMP_THRESHOLD:
            hours = lifepo4_discharge_hours(soc, float(capacity), discharge_a)
            if hours is not None:
                return format_time_remaining(hours, done_label="empty"), _time_remaining_seconds(
                    hours, "empty"
                )

        if (
            slope
            and slope < 0
            and empty_v is not None
            and bat_v is not None
            and not lifepo4_in_plateau(float(bat_v), cells)
        ):
            hours = lifepo4_hours_from_voltage_slope(
                float(bat_v),
                float(empty_v),
                slope,
                cells,
                toward_full=False,
            )
            if hours is not None:
                return format_time_remaining(hours, done_label="empty"), _time_remaining_seconds(
                    hours, "empty"
                )
        return "TBD", None

    if discharge_active:
        return "TBD", None

    if tracer_is_charging(data) or (
        trend == "up" and charge_amps(data) < CHARGE_AMP_THRESHOLD
    ):
        if soc is not None and float(soc) >= 99.0:
            return "full", 0

        empirical = empirical_hours_charging_to_full(data, charging_history, float_v)
        if empirical is not None:
            return format_time_remaining(empirical, done_label="full"), _time_remaining_seconds(
                empirical, "full"
            )

        if tracer_is_charging(data):
            amps = charge_amps(data)
            if soc is not None and capacity is not None:
                hours = lifepo4_charge_hours(soc, float(capacity), amps)
                if hours is not None:
                    return format_time_remaining(hours, done_label="full"), _time_remaining_seconds(
                        hours, "full"
                    )

        if trend == "up" and slope and slope > 0 and float_v is not None and bat_v is not None:
            hours = lifepo4_hours_charging_to_full(
                data,
                float(bat_v),
                float(float_v),
                slope,
                cells,
                cfg,
            )
            if hours is not None:
                return format_time_remaining(hours, done_label="full"), _time_remaining_seconds(
                    hours, "full"
                )
        return "TBD", None

    if soc is not None and soc >= 99.0:
        return "full", 0

    return "TBD", None


BATTERY_TYPE_NAMES = {
    0: "User defined",
    1: "Sealed",
    2: "GEL",
    3: "Flooded",
}

BATTERY_RATED_VOLTAGE_NAMES = {
    0: "Auto",
    1: "12 V",
    2: "24 V",
}

MANAGEMENT_MODE_NAMES = {
    0: "Voltage",
    1: "SOC",
}

# Load output control (holding register 0x903D). Mode 1 = Light ON/OFF uses PV
# day/night thresholds (NTTV/DTTV) configured on the charger — no host software needed.
LOAD_CONTROL_MODE_MANUAL = 0
LOAD_CONTROL_MODE_LIGHT_ON_OFF = 1
LOAD_CONTROL_MODE_LIGHT_TIMER = 2
LOAD_CONTROL_MODE_TIME = 3

LOAD_CONTROL_MODE_NAMES = {
    LOAD_CONTROL_MODE_MANUAL: "Manual",
    LOAD_CONTROL_MODE_LIGHT_ON_OFF: "Light ON/OFF",
    LOAD_CONTROL_MODE_LIGHT_TIMER: "Light ON+Timer",
    LOAD_CONTROL_MODE_TIME: "Time Control",
}

LIGHTS_STATES = ("off", "auto", "on")

# Primary telemetry used to decide whether the charger responded.
REACHABILITY_KEYS = (
    "pv_voltage",
    "pv_current",
    "battery_voltage",
    "battery_current",
    "load_current",
    "charger_temp",
    "charging_equipment_status",
)


def is_charger_reachable(data: Dict[str, Any]) -> bool:
    """True when at least one core register was read from the controller."""
    return any(data.get(key) is not None for key in REACHABILITY_KEYS)


@contextmanager
def serial_lock(device: str, timeout: float = 15.0) -> Iterator[None]:
    """Exclusive lock so only one process uses the RS-485 adapter at a time."""
    safe_name = os.path.basename(device).replace("/", "_") or "rs485"
    lock_path = f"/tmp/solartracer-{safe_name}.lock"
    fd = open(lock_path, "w")
    try:
        deadline = time.time() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    raise TimeoutError(f"Timed out waiting for RS-485 lock on {device}")
                time.sleep(0.05)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


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


def read_register_safe(
    instrument: minimalmodbus.Instrument,
    address: int,
    decimals: int = 2,
    functioncode: int = 4,
) -> Optional[Union[int, float]]:
    """Read a register, return None on any error."""
    try:
        val = instrument.read_register(address, decimals, functioncode)
        # For status registers (decimals=0) return as int
        if decimals == 0 and val is not None:
            return int(val)
        return val
    except Exception:
        return None


def _decode_config_labels(config: Dict[str, Any]) -> Dict[str, Any]:
    """Add human-readable labels for raw configuration codes."""
    labels: Dict[str, Any] = {}
    bat_type = config.get("battery_type")
    if bat_type is not None:
        labels["battery_type_name"] = BATTERY_TYPE_NAMES.get(int(bat_type), f"Unknown ({bat_type})")
    rated_code = config.get("battery_rated_voltage_code")
    if rated_code is not None:
        labels["battery_rated_voltage_name"] = BATTERY_RATED_VOLTAGE_NAMES.get(
            int(rated_code), f"Unknown ({rated_code})"
        )
    mgmt = config.get("management_mode")
    if mgmt is not None:
        labels["management_mode_name"] = MANAGEMENT_MODE_NAMES.get(int(mgmt), f"Unknown ({mgmt})")
    return labels


def read_charger_config(instrument: minimalmodbus.Instrument) -> Dict[str, Any]:
    """Read battery/charger configuration from holding registers."""
    config: Dict[str, Any] = {}
    for address, decimals, key in CONFIG_REGISTERS:
        config[key] = read_register_safe(instrument, address, decimals, functioncode=3)
    config.update(_decode_config_labels(config))
    return config


def compute_battery_level_pct(
    data: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> Optional[float]:
    """Return battery state-of-charge percent using charger data when possible.

    Priority:
      1. battery_soc input register (0x311A) when present
      2. Voltage estimate from configured empty/full thresholds
    """
    soc = data.get("battery_soc")
    if soc is not None:
        return float(_clamp_pct(soc))

    voltage = data.get("battery_voltage")
    if voltage is None:
        return None

    cfg = config or data.get("config") or {}
    cells = cell_count_from_data(data, cfg)
    return lifepo4_soc_from_pack_voltage(float(voltage), cells)


def _clamp_pct(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


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


def get_solar_data(device: Optional[str] = None, include_config: bool = False) -> Dict[str, Any]:
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
        with serial_lock(device):
            try:
                instrument = setup_instrument(device)
            except Exception as e:
                print(f"Failed to open serial device {device}: {e}")
                raise

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

            # Load / lights relay and PV day/night thresholds (NTTV/DTTV)
            nttv: Optional[float] = None
            dttv: Optional[float] = None
            try:
                load_mode = instrument.read_register(0x903D, 0, functioncode=3)
                data["load_control_mode"] = load_mode
                data["lights_manual_mode"] = (load_mode == LOAD_CONTROL_MODE_MANUAL)
                load_default = instrument.read_register(0x906A, 0, functioncode=3)
                if load_mode == LOAD_CONTROL_MODE_MANUAL:
                    data["lights_on"] = bool(load_default)
                else:
                    load_a = data.get("load_current")
                    data["lights_on"] = float(load_a or 0) > 0.05 if load_a is not None else None
                nttv = instrument.read_register(0x901E, 2, functioncode=3)
                dttv = instrument.read_register(0x9020, 2, functioncode=3)
            except Exception:
                data["load_control_mode"] = None
                data["lights_on"] = None
                data["lights_manual_mode"] = None

            data["night_time_threshold_voltage"] = nttv
            data["day_time_threshold_voltage"] = dttv
            data["is_night"] = derive_is_night(data.get("pv_voltage"), nttv, dttv)

            if include_config:
                data["config"] = read_charger_config(instrument)

            data["lights_state"] = get_lights_state(data)
            data["battery_level_pct"] = compute_battery_level_pct(data)
    except (TimeoutError, OSError) as e:
        print(f"RS-485 access failed for {device}: {e}")
        for _, _, key, *_ in REGISTERS + EXTRA_REGISTERS:
            data[key] = None
        for _, _, key in STATUS_REGISTERS:
            data[key] = None
        charging_info = _decode_charging_status(None)
        data.update(charging_info)
        if include_config:
            data["config"] = {key: None for _, _, key in CONFIG_REGISTERS}
        data["battery_level_pct"] = None
    except Exception as e:
        print(f"Failed to open serial device {device}: {e}")
        for _, _, key, *_ in REGISTERS + EXTRA_REGISTERS:
            data[key] = None
        for _, _, key in STATUS_REGISTERS:
            data[key] = None
        charging_info = _decode_charging_status(None)
        data.update(charging_info)
        if include_config:
            data["config"] = {key: None for _, _, key in CONFIG_REGISTERS}
        data["battery_level_pct"] = None

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


def get_lights_state(data: Optional[Dict[str, Any]] = None) -> str:
    """Return lights mode from charger config: off, auto, or on."""
    if data is None:
        return "off"
    mode = data.get("load_control_mode")
    if mode is not None and int(mode) != LOAD_CONTROL_MODE_MANUAL:
        return "auto"
    if data.get("lights_on"):
        return "on"
    return "off"


def lights_state_label(data: Dict[str, Any], compact: bool = False) -> str:
    """Human-readable lights mode for display."""
    state = data.get("lights_state") or get_lights_state(data)
    if state == "auto":
        relay = "on" if data.get("lights_on") else "off"
        if compact:
            return f"AUTO/{relay}"
        mode = data.get("load_control_mode")
        mode_name = (
            LOAD_CONTROL_MODE_NAMES.get(int(mode), "day/night")
            if mode is not None
            else "day/night"
        )
        night = data.get("is_night")
        if night is True:
            daynight = "night"
        elif night is False:
            daynight = "day"
        else:
            daynight = "day/night"
        return f"AUTO/{relay} ({mode_name}, {daynight})"
    return state.upper()


def set_lights_manual(
    enabled: bool,
    device: Optional[str] = None,
    quiet: bool = False,
) -> None:
    """Force manual load control on or off."""
    if device is None:
        device = os.getenv("SERIAL_DEVICE", "/dev/ttyACM0")
    with serial_lock(device):
        inst = setup_instrument(device)
        inst.write_register(0x903D, LOAD_CONTROL_MODE_MANUAL)
        inst.write_bit(2, enabled)
        inst.write_bit(6, enabled)
        inst.write_register(0x906A, 1 if enabled else 0)
    if not quiet:
        print(f"Lights relay {'enabled' if enabled else 'disabled'} (manual)")


def set_lights_auto(
    device: Optional[str] = None,
    quiet: bool = False,
    mode: int = LOAD_CONTROL_MODE_LIGHT_ON_OFF,
) -> None:
    """Enable charger-native load control (default: PV day/night Light ON/OFF)."""
    if device is None:
        device = os.getenv("SERIAL_DEVICE", "/dev/ttyACM0")
    with serial_lock(device):
        inst = setup_instrument(device)
        inst.write_register(0x903D, mode)
        inst.write_bit(2, False)
        inst.write_bit(6, False)
    if not quiet:
        name = LOAD_CONTROL_MODE_NAMES.get(mode, str(mode))
        print(f"Lights relay automatic ({name})")


def set_lights_state(
    state: str,
    device: Optional[str] = None,
    quiet: bool = False,
    data: Optional[Dict[str, Any]] = None,
) -> None:
    """Apply off, auto, or on lights mode on the charger."""
    del data  # kept for callers; auto is configured entirely on the controller
    if state == "auto":
        set_lights_auto(device=device, quiet=quiet)
    elif state == "on":
        set_lights_manual(True, device=device, quiet=quiet)
    elif state == "off":
        set_lights_manual(False, device=device, quiet=quiet)
    else:
        raise ValueError(f"Unknown lights state: {state!r}")


def cycle_lights_state(
    device: Optional[str] = None,
    data: Optional[Dict[str, Any]] = None,
    quiet: bool = False,
) -> str:
    """Advance lights mode: off → auto → on → off."""
    current = get_lights_state(data)
    next_state = LIGHTS_STATES[(LIGHTS_STATES.index(current) + 1) % len(LIGHTS_STATES)]
    set_lights_state(next_state, device=device, quiet=quiet, data=data)
    return next_state


def set_lights_enabled(
    enabled: bool,
    device: Optional[str] = None,
    quiet: bool = False,
):
    """Control the lights relay (load output) in manual mode.

    Backwards-compatible helper used by the MQTT publisher.
    Leaves auto mode when a manual on/off command is received.
    """
    set_lights_manual(enabled, device=device, quiet=quiet)


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
