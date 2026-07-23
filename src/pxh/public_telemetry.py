"""Privacy projection for unauthenticated telemetry.

The API owns sampling and storage; this module owns the public boundary.
"""

from datetime import datetime
import time
from typing import Any, Iterable, Optional


ACTIVITY_DELAY_S = 15 * 60
ACTIVE_AMBIENT_RMS = 500
PRIVATE_HISTORY_FIELDS = frozenset({"ambient_rms", "person_present"})


def sample_epoch(sample: dict[str, Any]) -> Optional[float]:
    try:
        return datetime.fromisoformat(
            str(sample["ts"]).replace("Z", "+00:00")
        ).timestamp()
    except (KeyError, TypeError, ValueError):
        return None


def delayed_activity(
    samples: Iterable[dict[str, Any]],
    now: Optional[float] = None,
) -> tuple[str, Optional[int]]:
    now_epoch = time.time() if now is None else now
    cutoff = now_epoch - ACTIVITY_DELAY_S

    for sample in reversed(list(samples)):
        recorded_at = sample_epoch(sample)
        if recorded_at is None or recorded_at > cutoff:
            continue
        person_present = sample.get("person_present")
        ambient_rms = sample.get("ambient_rms")
        if person_present is True:
            activity = "active"
        elif isinstance(ambient_rms, (int, float)):
            activity = "active" if ambient_rms >= ACTIVE_AMBIENT_RMS else "quiet"
        elif person_present is False:
            activity = "quiet"
        else:
            activity = "unknown"
        return activity, max(0, round(now_epoch - recorded_at))
    return "unknown", None


def project_history(
    samples: Iterable[dict[str, Any]],
    limit: int,
    now: Optional[float] = None,
) -> list[dict[str, Any]]:
    now_epoch = time.time() if now is None else now
    cutoff = now_epoch - ACTIVITY_DELAY_S
    projected = []
    for sample in samples:
        recorded_at = sample_epoch(sample)
        if recorded_at is None or recorded_at > cutoff:
            continue
        projected.append({
            key: value
            for key, value in sample.items()
            if key not in PRIVATE_HISTORY_FIELDS
        })
    return projected[-limit:]
