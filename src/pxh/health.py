"""Daemon health tracking — the instrument that makes silent failures visible.

Every long-running daemon reports whether it is *doing its job*, not merely
whether its process is alive. ``systemctl status`` answers the second question;
this module answers the first.

Store: ``state/health/<component>.json``, one file per component::

    {"component", "last_success_ts", "last_success_detail",
     "last_error", "last_error_ts", "consecutive_failures",
     "success_count", "failure_count", "updated_ts"}

**One file per component, deliberately.** A single shared ``health.json`` would
need a ``FileLock`` for its read-modify-write, and this system has cross-user
writers — ``px-alive`` and ``px-battery-poll`` run as root, everything else as
``pi``. ``filelock`` creates its lock file with the creator's umask, so a
root-created lock at 0644 locks out every ``pi`` daemon with EACCES. Giving each
component sole ownership of one file removes the lock, the race, and the
ownership hazard in one move; ``atomic_write()`` handles durability.

Status is derived at *read* time, never stored — "stale" is a function of now,
so a component that dies cannot leave a lying "ok" behind.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
from pathlib import Path
from typing import Any

from pxh.state import atomic_write
from pxh.time import utc_timestamp

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# consecutive_failures at or above this reads as "failing" rather than "degraded"
FAIL_THRESHOLD = 3

# How long a component may go without reporting before it reads as "stale".
# Roughly 3x its natural cycle — long enough to absorb one slow tick and a
# systemd restart (most units restart after 10s) without crying wolf.
STALE_AFTER_S: dict[str, int] = {
    "px-mind": 300,           # awareness ticks every 60s
    # Reflection backs off to 8x its 300s base when nobody is around (40 min),
    # so its silence window has to clear that or an idle house reads as broken.
    "px-mind-reflection": 3600,
    "px-alive": 300,          # idle actions are sporadic; heartbeat is periodic
    "px-battery-poll": 300,
    "px-wake-listen": 900,    # only reports on wake events + periodic heartbeat
    "px-post": 3600,          # only runs when a postable thought appears
    "px-blog": 86400,         # daily cadence at its most frequent
    "px-api-server": 300,
    "px-frigate-stream": 300,
}
DEFAULT_STALE_AFTER_S = 900

# Components expected to exist. Absent files report "missing" rather than being
# silently omitted — a daemon that never started is exactly what we want to see.
KNOWN_COMPONENTS = tuple(STALE_AFTER_S)

_STATUS_RANK = {"ok": 0, "degraded": 1, "stale": 2, "failing": 3, "missing": 3}

# Per-process monotonic clock of the last throttled success write, per component.
# Process-local by design: it only guards write frequency, never correctness.
_last_success_write: dict[str, float] = {}


def _state_dir() -> Path:
    root = Path(os.environ.get("PROJECT_ROOT", PROJECT_ROOT))
    return Path(os.environ.get("PX_STATE_DIR", root / "state"))


def health_dir() -> Path:
    return _state_dir() / "health"


def _component_path(component: str) -> Path:
    # Components are internal constants, but a traversal here would let a
    # caller-supplied name escape state/ — normalise defensively.
    safe = component.replace("/", "_").replace("..", "_").strip()
    return health_dir() / f"{safe}.json"


def _read_record(component: str) -> dict[str, Any]:
    try:
        return json.loads(_component_path(component).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


# Sticky + world-writable, like /tmp. px-alive and px-battery-poll run as root
# while everything else runs as pi, and atomic_write() needs directory write
# permission for its mkstemp — a root-created 0755 dir would lock out every pi
# daemon. Sticky keeps one user from deleting another's record.
_HEALTH_DIR_MODE = 0o1777


def _ensure_health_dir() -> Path | None:
    d = health_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None  # read-only fs or permissions — health must never break a daemon
    try:
        if (d.stat().st_mode & 0o7777) != _HEALTH_DIR_MODE:
            os.chmod(d, _HEALTH_DIR_MODE)
    except OSError:
        pass  # not the owner — the creating user already set the mode, or we lose
    return d


def _write_record(component: str, record: dict[str, Any]) -> None:
    if _ensure_health_dir() is None:
        return
    record["component"] = component
    record["updated_ts"] = utc_timestamp()
    try:
        atomic_write(_component_path(component), json.dumps(record, indent=2))
    except OSError:
        pass


def record_success(component: str, detail: Any = None, min_interval_s: float = 0.0) -> None:
    """Note that `component` completed its work. Resets the failure streak.

    `min_interval_s` throttles writes for fast loops — px-alive ticks twice a
    second, and an fsync per tick would wear the SD card for no added signal.
    Successes coalesce safely; failures never throttle, because each one
    advances the streak that separates a glitch from a dead sensor.

    Never raises: health reporting must not be able to take down the daemon it
    is reporting on.
    """
    try:
        if min_interval_s > 0:
            last = _last_success_write.get(component)
            now = time.monotonic()
            if last is not None and (now - last) < min_interval_s:
                return
            _last_success_write[component] = now
        rec = _read_record(component)
        rec["last_success_ts"] = utc_timestamp()
        if detail is not None:
            rec["last_success_detail"] = detail
        rec["consecutive_failures"] = 0
        rec["success_count"] = int(rec.get("success_count", 0)) + 1
        _write_record(component, rec)
    except Exception:
        pass


def record_failure(component: str, error: str, detail: Any = None) -> None:
    """Note that `component` failed. Increments the consecutive failure streak."""
    try:
        # Drop the throttle so the recovery that follows is written immediately.
        # Without this, a component that alternates failure/success faster than
        # its throttle window would accumulate failures while its successes were
        # silently discarded — and read as "failing" while working.
        _last_success_write.pop(component, None)
        rec = _read_record(component)
        rec["last_error"] = str(error)[:500]
        rec["last_error_ts"] = utc_timestamp()
        if detail is not None:
            rec["last_error_detail"] = detail
        rec["consecutive_failures"] = int(rec.get("consecutive_failures", 0)) + 1
        rec["failure_count"] = int(rec.get("failure_count", 0)) + 1
        _write_record(component, rec)
    except Exception:
        pass


def _age_s(ts: str | None, now: dt.datetime) -> float | None:
    if not ts:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return (now - parsed).total_seconds()


def _derive_status(rec: dict[str, Any], component: str, now: dt.datetime) -> tuple[str, float | None]:
    if not rec:
        return "missing", None
    age = _age_s(rec.get("updated_ts"), now)
    fails = int(rec.get("consecutive_failures", 0) or 0)
    limit = STALE_AFTER_S.get(component, DEFAULT_STALE_AFTER_S)

    if fails >= FAIL_THRESHOLD:
        return "failing", age
    # Staleness outranks a partial failure streak: a component that stopped
    # reporting entirely is a bigger problem than one that failed twice and
    # is still trying.
    if age is not None and age > limit:
        return "stale", age
    if fails > 0:
        return "degraded", age
    if rec.get("last_success_ts") is None:
        return "degraded", age  # has reported, but has never yet succeeded
    return "ok", age


def read_health(components: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Aggregate every component record into a single live view.

    Reads the per-component files directly, so it stays truthful even when
    px-mind (which publishes the cached snapshot) is itself dead.
    """
    now = dt.datetime.now(dt.timezone.utc)
    names = set(components or KNOWN_COMPONENTS)
    # Include any component that has written a file but isn't in KNOWN_COMPONENTS
    # (new daemons, tools) so nothing reports invisibly.
    try:
        names.update(p.stem for p in health_dir().glob("*.json"))
    except OSError:
        pass

    out: dict[str, Any] = {}
    overall = "ok"
    unhealthy: list[str] = []
    for name in sorted(names):
        rec = _read_record(name)
        status, age = _derive_status(rec, name, now)
        entry: dict[str, Any] = {"status": status}
        if age is not None:
            entry["age_s"] = round(age)
        for key in ("last_success_ts", "last_error", "last_error_ts",
                    "consecutive_failures", "success_count", "failure_count"):
            if key in rec:
                entry[key] = rec[key]
        out[name] = entry
        if _STATUS_RANK.get(status, 0) > _STATUS_RANK.get(overall, 0):
            overall = status
        if status != "ok":
            unhealthy.append(name)

    return {
        "ts": utc_timestamp(),
        "overall": overall,
        "unhealthy": unhealthy,
        "components": out,
    }


def summarize(health: dict[str, Any] | None = None) -> str:
    """One-line human/LLM-readable summary, or "" when everything is well.

    Returns empty on a fully healthy system so callers can inject it into
    reflection context unconditionally without adding noise on good days.
    """
    health = health if health is not None else read_health()
    unhealthy = health.get("unhealthy") or []
    if not unhealthy:
        return ""
    bits = []
    for name in unhealthy:
        entry = health["components"][name]
        status = entry.get("status")
        if status == "missing":
            bits.append(f"{name}: never reported")
        elif status == "stale":
            mins = round((entry.get("age_s") or 0) / 60)
            bits.append(f"{name}: silent for {mins} min")
        else:
            err = (entry.get("last_error") or "").strip()
            fails = entry.get("consecutive_failures", 0)
            detail = f" ({err[:80]})" if err else ""
            bits.append(f"{name}: {status} after {fails} failures{detail}")
    return "; ".join(bits)
