"""Validated current readings; unavailable sensors never inherit cached values."""
from __future__ import annotations

import datetime as dt
import json
import math
import time
from pathlib import Path


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("not a numeric reading")
    if not math.isfinite(value):
        raise ValueError("non-finite reading")
    return value


def read_battery(path: Path, *, now: float | None = None) -> dict | None:
    """Accept a valid 2S battery sample for three 30-second polling intervals."""
    try:
        data = json.loads(path.read_text())
        stamp = dt.datetime.fromisoformat(data["ts"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return None
        age = (time.time() if now is None else now) - stamp.timestamp()
        pct, volts = _number(data["pct"]), _number(data["volts"])
        charging = data.get("charging", False)
        if not 0 <= age <= 90 or not 0 <= pct <= 100 or not 5 <= volts <= 9:
            return None
        if not isinstance(charging, bool):
            return None
        return {"pct": pct, "volts": volts, "charging": charging, "ts": data["ts"]}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return None


def read_sonar(path: Path, *, max_age: float = 60, now: float | None = None) -> dict | None:
    """Reject stale, future-dated, non-finite and nonpositive distances."""
    try:
        data = json.loads(path.read_text())
        stamp, distance = _number(data["ts"]), _number(data["distance_cm"])
        age = (time.time() if now is None else now) - stamp
        if not 0 <= age <= max_age or distance <= 0:
            return None
        return {"ts": stamp, "distance_cm": distance, "age_seconds": round(age)}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return None
