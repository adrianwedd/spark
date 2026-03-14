from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from filelock import FileLock

from .logging import log_event
from .time import utc_timestamp

_log = logging.getLogger("pxh.state")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically via temp file + os.replace."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
STATE_DIR = PROJECT_ROOT / "state"
DEFAULT_SESSION_PATH = STATE_DIR / "session.json"
TEMPLATE_PATH = STATE_DIR / "session.template.json"


def default_state() -> Dict[str, Any]:
    return {
        "schema_version": "1.0",
        "mode": "dry-run",
        "last_action": None,
        "last_motion": None,
        "battery_pct": None,
        "battery_ok": None,
        "wheels_on_blocks": False,
        "confirm_motion_allowed": False,
        "watchdog_heartbeat_ts": None,
        "last_weather": None,
        "last_prompt_excerpt": None,
        "last_model_action": None,
        "last_tool_payload": None,
        "persona": None,
        "listening": False,
        "listening_since": None,
        # SPARK child-companion fields
        "obi_routine": None,
        "obi_step": 0,
        "obi_mood": None,
        "obi_streak": 0,
        "spark_quiet_mode": False,
        "history": [],
    }


def session_path() -> Path:
    override = os.environ.get("PX_SESSION_PATH")
    if override:
        return Path(override)
    return DEFAULT_SESSION_PATH


def ensure_session() -> Path:
    path = session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = str(path) + ".lock"
    with FileLock(lock_path):
        if not path.exists():
            if TEMPLATE_PATH.exists():
                _atomic_write(path, TEMPLATE_PATH.read_text(encoding="utf-8"))
            else:
                _atomic_write(path, json.dumps(default_state(), indent=2) + "\n")
    return path


def load_session() -> Dict[str, Any]:
    path = ensure_session()
    lock_path = str(path) + ".lock"
    with FileLock(lock_path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            _log.warning("session.json corrupt — resetting to defaults: %s", path)
            data = default_state()
            _atomic_write(path, json.dumps(data, indent=2) + "\n")
            return data


def save_session(data: Dict[str, Any]) -> None:
    path = ensure_session()
    lock_path = str(path) + ".lock"
    with FileLock(lock_path):
        _atomic_write(path, json.dumps(data, indent=2) + "\n")


def update_session(
    fields: Optional[Dict[str, Any]] = None,
    history_entry: Optional[Dict[str, Any]] = None,
    history_limit: int = 100,
) -> Dict[str, Any]:
    # Call ensure_session BEFORE acquiring the lock — ensure_session acquires
    # the same lock internally and FileLock is not reentrant.
    path = ensure_session()
    lock_path = str(path) + ".lock"
    with FileLock(lock_path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = default_state()
            log_event("state-corruption", {"path": str(path), "message": "session.json was corrupt; reset to default state"})

        if fields:
            data.update(fields)
        if history_entry:
            entry = {"ts": utc_timestamp(), **history_entry}
            history = data.setdefault("history", [])
            history.append(entry)
            if len(history) > history_limit:
                data["history"] = history[-history_limit:]

        _atomic_write(path, json.dumps(data, indent=2) + "\n")
        return data