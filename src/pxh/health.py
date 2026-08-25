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
    # Memory consolidation is nightly like px-blog, but it fires inside a
    # four-hour window (02:00-06:00 Hobart) rather than at a fixed hour, so two
    # perfectly healthy runs can sit ~28h apart (02:00 one night, 05:59 the
    # next). px-blog's 86400 would therefore report a working job as stale most
    # afternoons — the classic way an alarm teaches its reader to ignore it.
    # 30h clears that worst case with ~2h to spare, and a night that produced
    # no attempt at all still surfaces by the following morning.
    "px-mind-consolidation": 108000,
    "px-api-server": 300,
    "px-frigate-stream": 300,
    # The brain supervisor ticks every 10s but throttles its success writes to
    # once a minute, so its window only has to clear that comfortably.
    "px-brain": 300,
}
DEFAULT_STALE_AFTER_S = 900

# Components expected to exist. Absent files report "missing" rather than being
# silently omitted — a daemon that never started is exactly what we want to see.
KNOWN_COMPONENTS = tuple(STALE_AFTER_S)

# The nightly consolidation pass that turns a day of thoughts into durable
# memory. Named here because more than one surface asks the same question of it
# ("when did SPARK last form a long-term memory?") and a typo'd component name
# reads as "missing" rather than failing loudly.
CONSOLIDATION_COMPONENT = "px-mind-consolidation"

# Long-term memory that has not formed in this long is worth saying out loud
# even while the component itself still reads within its staleness window. Two
# nights is the threshold: one missed night is a bad night, two is amnesia.
MEMORY_FORMATION_OVERDUE_S = 48 * 3600

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
    try:
        fails = int(rec.get("consecutive_failures", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return "degraded", age
    limit = STALE_AFTER_S.get(component, DEFAULT_STALE_AFTER_S)

    if fails >= FAIL_THRESHOLD:
        return "failing", age
    if age is None:
        return "degraded", None
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


# A stall this close to the deadline is a near-miss worth reporting before it
# becomes a kill. Measured on the live Pi, the loop's ordinary max sits around
# 14.9s against a 15s watchdog, so anything above this is the normal-but-bad
# state that `systemctl status` and consecutive_failures both call healthy.
WATCHDOG_NEAR_MISS_RATIO = 0.66


def read_watchdog_margin(state_dir: Path | str | None = None) -> dict[str, Any]:
    """Interpret px-alive's self-reported timing. Never a second writer.

    px-alive owns these observations and publishes them in its existing
    heartbeat record; this only reads and classifies. Returns ``{}`` when the
    daemon predates the fields or the heartbeat is unreadable, so a missing
    record degrades to "no opinion" rather than a false clean bill.

    The values are in-process and reset with the process, so they describe
    stalls *within* one run of the loop. A restart gap can never be folded in —
    the distinction an external sampler must reconstruct from PIDs is
    structural here.
    """
    from .runtime_paths import resolve_heartbeat_read_path

    base = Path(state_dir) if state_dir is not None else _state_dir()
    try:
        rec = json.loads(resolve_heartbeat_read_path(base).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(rec, dict) or "heartbeat_gap_max_ms" not in rec:
        return {}

    out: dict[str, Any] = {}
    for key in ("heartbeat_gap_last_ms", "heartbeat_gap_max_ms",
                "heartbeat_gap_max_mode", "heartbeat_gap_buckets",
                "loop_duration_last_ms", "loop_duration_max_ms",
                "watchdog_margin_min_ms", "window_started_at"):
        if key in rec:
            out[key] = rec[key]

    gap_max = rec.get("heartbeat_gap_max_ms")
    margin = rec.get("watchdog_margin_min_ms")
    status = "ok"
    if isinstance(margin, (int, float)) and isinstance(gap_max, (int, float)):
        limit = margin + gap_max          # reconstruct the deadline in force
        if margin <= 0:
            status = "exceeded"
        elif limit > 0 and gap_max >= limit * WATCHDOG_NEAR_MISS_RATIO:
            status = "near_miss"
    out["watchdog_status"] = status
    return out


def humanize_age(seconds: float | None) -> str:
    """Coarse "3d ago" phrasing, matching bin/px-motd's `_age_str`."""
    if seconds is None:
        return "never"
    secs = int(seconds)
    if secs < 0:
        return "just now"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def memory_formation(now: dt.datetime | None = None) -> dict[str, Any]:
    """When SPARK last actually formed a long-term memory.

    Deliberately keyed on ``last_success_ts``, not ``updated_ts``: a failed
    consolidation attempt refreshes the record without distilling anything, so
    a component that reports every night and succeeds on none of them would
    otherwise look eternally fresh. That is the exact shape of the failure this
    exists to catch — the store stops growing while every dial reads green.

    ``overdue`` is a separate axis from ``status`` on purpose. A component can
    sit inside its staleness window and below the failure threshold while still
    not having produced a memory for days, because failing attempts keep both
    of those dials quiet.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    rec = _read_record(CONSOLIDATION_COMPONENT)
    status, _ = _derive_status(rec, CONSOLIDATION_COMPONENT, now)
    formed_ts = rec.get("last_success_ts") if rec else None
    age = _age_s(formed_ts, now)
    return {
        "component": CONSOLIDATION_COMPONENT,
        "status": status,
        "last_formed_ts": formed_ts,
        "age_s": round(age) if age is not None else None,
        "age_human": humanize_age(age),
        "overdue": age is None or age > MEMORY_FORMATION_OVERDUE_S,
        "last_error": rec.get("last_error") if rec else None,
    }


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
        if name == "px-alive":
            # A daemon can be stalling to within 70ms of a watchdog kill while
            # every field above reads clean — that is the exact blind spot this
            # module is documented as having. Attach the timing verdict where a
            # reader of px-alive's health cannot miss it.
            margin = read_watchdog_margin()
            if margin:
                entry["watchdog"] = margin
                if margin.get("watchdog_status") != "ok" and status == "ok":
                    status = "degraded"
                    entry["status"] = status
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
        # Promoted out of `components` as well as staying in it: "when did I
        # last form a long-term memory" is a question about SPARK, not about a
        # daemon, and a reader scanning for it should not have to know which
        # component name implements it.
        "memory_formation": memory_formation(now),
    }


def summarize(health: dict[str, Any] | None = None) -> str:
    """One-line human/LLM-readable summary, or "" when everything is well.

    Returns empty on a fully healthy system so callers can inject it into
    reflection context unconditionally without adding noise on good days.
    """
    health = health if health is not None else read_health()
    unhealthy = health.get("unhealthy") or []
    # Overdue memory formation is its own signal, reported even when every
    # component reads ok. Repeated failed attempts keep the staleness window
    # fed and the failure streak short-lived, so the component can look healthy
    # for days while the memory store has not grown at all.
    mem = health.get("memory_formation") or {}
    mem_note = ""
    if mem.get("overdue"):
        mem_note = ("long-term memory: never formed" if not mem.get("last_formed_ts")
                    else f"long-term memory: last formed {mem.get('age_human')}")
    if not unhealthy:
        return mem_note
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
    if mem_note:
        bits.append(mem_note)
    return "; ".join(bits)
