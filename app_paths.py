"""Runtime data paths for local persistence (voltage/rate history)."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Optional, Tuple

from solar_data import BatteryRateLog

DATA_DIRNAME = "var"
VOLTAGE_HISTORY_FILENAME = "voltage-history.json"
BATTERY_RATE_LOG_FILENAME = "battery-rate-log.json"
LEGACY_BATTERY_RATE_LOG_FILENAME = "charging-rate-history.json"


@dataclass(frozen=True)
class AppPaths:
    """Resolved on-disk locations for daemon/dashboard state files."""

    base_dir: str
    repo_dir: str

    @property
    def voltage_history(self) -> str:
        return os.path.join(self.base_dir, VOLTAGE_HISTORY_FILENAME)

    @property
    def battery_rate_log(self) -> str:
        return os.path.join(self.base_dir, BATTERY_RATE_LOG_FILENAME)

    @property
    def legacy_battery_rate_log(self) -> str:
        return os.path.join(self.base_dir, LEGACY_BATTERY_RATE_LOG_FILENAME)

    def ensure_dir(self) -> None:
        os.makedirs(self.base_dir, exist_ok=True)

    @classmethod
    def default(cls, repo_dir: Optional[str] = None) -> "AppPaths":
        root = repo_dir or os.path.dirname(os.path.abspath(__file__))
        root = os.path.abspath(root)
        return cls(base_dir=os.path.join(root, DATA_DIRNAME), repo_dir=root)

    @classmethod
    def from_env(cls) -> "AppPaths":
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        override = os.getenv("SOLARTRACER_DATA_DIR")
        if override:
            data_dir = os.path.abspath(override)
            return cls(base_dir=data_dir, repo_dir=repo_dir)
        return cls.default(repo_dir)


def _migrate_file_if_missing(target: str, sources: Tuple[str, ...]) -> None:
    if os.path.exists(target):
        return
    for source in sources:
        if not os.path.exists(source):
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            shutil.move(source, target)
            print(f"Migrated {source} -> {target}")
        except OSError as exc:
            print(f"Failed to migrate {source}: {exc}")
        return


def migrate_legacy_data_files(paths: AppPaths) -> None:
    """Move repo-root dotfiles and legacy names into ./var when needed."""
    paths.ensure_dir()
    root = paths.repo_dir
    dotted = (
        f".{VOLTAGE_HISTORY_FILENAME}",
        f".{BATTERY_RATE_LOG_FILENAME}",
        f".{LEGACY_BATTERY_RATE_LOG_FILENAME}",
    )
    _migrate_file_if_missing(
        paths.voltage_history,
        (
            os.path.join(root, dotted[0]),
            os.path.join(paths.base_dir, dotted[0]),
        ),
    )
    _migrate_file_if_missing(
        paths.battery_rate_log,
        (
            os.path.join(root, dotted[1]),
            os.path.join(paths.base_dir, dotted[1]),
        ),
    )
    _migrate_file_if_missing(
        paths.battery_rate_log,
        (
            os.path.join(root, dotted[2]),
            os.path.join(paths.base_dir, dotted[2]),
            paths.legacy_battery_rate_log,
        ),
    )


def load_battery_rate_log(paths: AppPaths) -> BatteryRateLog:
    """Load the rate log, migrating legacy files when present."""
    migrate_legacy_data_files(paths)
    if (
        not os.path.exists(paths.battery_rate_log)
        and os.path.exists(paths.legacy_battery_rate_log)
    ):
        rate_log = BatteryRateLog.load_from(paths.legacy_battery_rate_log)
        try:
            rate_log.save_to(paths.battery_rate_log)
            print(f"Migrated battery rate log from {paths.legacy_battery_rate_log}")
        except OSError as exc:
            print(f"Failed to migrate battery rate log: {exc}")
        return rate_log
    return BatteryRateLog.load_from(paths.battery_rate_log)