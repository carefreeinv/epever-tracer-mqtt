"""Coordinates persisted telemetry used for trend and time-remaining estimates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from app_paths import AppPaths, load_battery_rate_log, migrate_legacy_data_files
from solar_data import (
    BatteryRateLog,
    TimedMetricHistory,
    record_battery_rate_sample,
    record_voltage_rate_sample,
)


@dataclass
class TelemetryStore:
    """Voltage trend window plus charge/discharge rate log."""

    paths: AppPaths
    voltage_history: TimedMetricHistory
    rate_log: BatteryRateLog

    @classmethod
    def load(cls, paths: Optional[AppPaths] = None) -> "TelemetryStore":
        resolved = paths or AppPaths.from_env()
        migrate_legacy_data_files(resolved)
        resolved.ensure_dir()
        return cls(
            paths=resolved,
            voltage_history=TimedMetricHistory.load_from(resolved.voltage_history),
            rate_log=load_battery_rate_log(resolved),
        )

    def add_voltage_sample(self, voltage: float) -> None:
        self.voltage_history.add(voltage)
        record_voltage_rate_sample(self.rate_log, voltage, self.voltage_history)

    def record_charger_sample(self, data: Dict[str, Any]) -> None:
        record_battery_rate_sample(self.rate_log, data, self.voltage_history)

    def persist(self) -> None:
        self.paths.ensure_dir()
        self.voltage_history.save_to(self.paths.voltage_history)
        self.rate_log.save_to(self.paths.battery_rate_log)

    def persist_quiet(self) -> None:
        try:
            self.persist()
        except OSError:
            pass