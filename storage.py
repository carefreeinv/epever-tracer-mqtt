"""Atomic JSON persistence helpers."""

from __future__ import annotations

import json
import os
from typing import Any, Optional


def read_json_file(path: str) -> Optional[Any]:
    """Read JSON from *path*, returning None if missing or invalid."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def atomic_write_json(path: str, payload: Any) -> None:
    """Write JSON atomically via a sibling temporary file."""
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(tmp_path, path)