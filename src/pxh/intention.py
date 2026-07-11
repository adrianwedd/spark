"""Goal/intention persistence for SPARK — QA roadmap item 6.

One active intention survives across reflection cycles. SPARK manages it via
the set_goal / update_goal / complete_goal actions dispatched by mind.py.
State file: state/intention-{persona}.json
  {"active": {goal, set_at, updated_at, progress: [{ts, note}], status},
   "history": [last 10 archived intentions]}
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from filelock import FileLock

from pxh.state import atomic_write
from pxh.time import utc_timestamp

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

GOAL_MAX_CHARS = 500
NOTE_MAX_CHARS = 300
MAX_PROGRESS = 10
MAX_HISTORY = 10
STALE_DAYS = 7
LOCK_TIMEOUT_S = 10


def _state_dir() -> Path:
    return Path(os.environ.get("PX_STATE_DIR", PROJECT_ROOT / "state"))


def intention_file(persona: str = "spark") -> Path:
    return _state_dir() / f"intention-{persona or 'spark'}.json"


def _load(persona: str = "spark") -> dict:
    f = intention_file(persona)
    if not f.exists():
        return {"active": None, "history": []}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not a dict")
        data.setdefault("active", None)
        data.setdefault("history", [])
        return data
    except Exception:
        return {"active": None, "history": []}


def _save(data: dict, persona: str = "spark") -> None:
    f = intention_file(persona)
    f.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(f, json.dumps(data, indent=2) + "\n")


def _archive_active(data: dict, status: str) -> None:
    active = data.get("active")
    if not active:
        return
    active["status"] = status
    data["history"] = (data.get("history") or [])[-(MAX_HISTORY - 1):] + [active]
    data["active"] = None


def _lock(persona: str) -> FileLock:
    return FileLock(str(intention_file(persona)) + ".lock", timeout=LOCK_TIMEOUT_S)


def set_goal(text: str, persona: str = "spark") -> dict:
    text = (text or "").strip()[:GOAL_MAX_CHARS]
    if not text:
        return {"status": "error", "error": "empty goal"}
    intention_file(persona).parent.mkdir(parents=True, exist_ok=True)
    with _lock(persona):
        data = _load(persona)
        _archive_active(data, "superseded")
        now = utc_timestamp()
        data["active"] = {"goal": text, "set_at": now, "updated_at": now,
                          "progress": [], "status": "active"}
        _save(data, persona)
    return {"status": "ok", "goal": text}


def update_goal(text: str, persona: str = "spark") -> dict:
    note = (text or "").strip()[:NOTE_MAX_CHARS]
    with _lock(persona):
        data = _load(persona)
        active = data.get("active")
        if not active:
            return {"status": "no_active_intention"}
        active["progress"] = (active.get("progress") or [])[-(MAX_PROGRESS - 1):] + [
            {"ts": utc_timestamp(), "note": note}]
        active["updated_at"] = utc_timestamp()
        _save(data, persona)
    return {"status": "ok"}


def complete_goal(text: str = "", persona: str = "spark") -> dict:
    note = (text or "").strip()[:NOTE_MAX_CHARS]
    with _lock(persona):
        data = _load(persona)
        active = data.get("active")
        if not active:
            return {"status": "no_active_intention"}
        if note:
            active["progress"] = (active.get("progress") or [])[-(MAX_PROGRESS - 1):] + [
                {"ts": utc_timestamp(), "note": note}]
        active["updated_at"] = utc_timestamp()
        _archive_active(data, "done")
        _save(data, persona)
    return {"status": "ok"}


def get_active_goal(persona: str = "spark") -> str:
    active = _load(persona).get("active")
    return (active or {}).get("goal", "") if active else ""


def _age_days(ts_str: str, now: dt.datetime) -> float:
    try:
        ts = dt.datetime.fromisoformat((ts_str or "").replace("Z", "+00:00"))
        return max(0.0, (now - ts).total_seconds() / 86400)
    except (ValueError, TypeError):
        return 0.0


def format_for_context(persona: str = "spark", now: dt.datetime | None = None) -> str:
    """Context paragraph for reflection: active intention, one-shot expiry
    notice (expiring archives the goal, so the notice fires exactly once), or ''."""
    now = now or dt.datetime.now(dt.timezone.utc)
    with _lock(persona):
        data = _load(persona)
        active = data.get("active")
        if not active:
            return ""
        age = _age_days(active.get("set_at", ""), now)
        if age > STALE_DAYS:
            goal = active.get("goal", "")
            _archive_active(data, "expired")
            _save(data, persona)
            return (f'Your intention "{goal}" expired after {STALE_DAYS} days without '
                    f"completion. Let it go, or set it again with set_goal if it still matters.")
    days = int(age)
    when = "set today" if days == 0 else f"set {days} day{'s' if days != 1 else ''} ago"
    lines = [f'Your current intention: "{active.get("goal", "")}" ({when}).']
    progress = active.get("progress") or []
    if progress:
        lines.append("Recent progress:")
        lines.extend(f'  - {p.get("note", "")}' for p in progress[-2:])
    else:
        lines.append("No progress recorded yet — update_goal records progress; complete_goal closes it.")
    return "\n".join(lines)
