from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Mapping

try:
    from filelock import FileLock, Timeout as FileLockTimeout
except ImportError:
    FileLock = None
    FileLockTimeout = None

from .time import utc_timestamp

_log = logging.getLogger("pxh.logging")

# Throttle FileLockTimeout stderr warnings: once per 60 s per log name.
_last_timeout_warn: dict[str, float] = {}
_TIMEOUT_WARN_INTERVAL_S = 60.0


def _warn_lock_timeout(name: str) -> None:
    now = time.monotonic()
    if now - _last_timeout_warn.get(name, 0.0) >= _TIMEOUT_WARN_INTERVAL_S:
        _last_timeout_warn[name] = now
        print(f"[pxh.logging] rotlock timeout on {name} — dropping entry", file=sys.stderr)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _resolve_log_dir() -> Path:
    """Resolve the log directory, honoring an optional LOG_DIR override."""
    env_dir = os.environ.get("LOG_DIR")
    if not env_dir:
        return PROJECT_ROOT / "logs"
    candidate = Path(env_dir)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return candidate

LOG_DIR = _resolve_log_dir()


_LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per log file


def log_event(name: str, payload: Mapping[str, Any]) -> None:
    """Append a structured log entry under logs/tool-<name>.log."""
    log_path = LOG_DIR / f"tool-{name}.log"
    lock_path = str(LOG_DIR / f"tool-{name}.log") + ".rotlock"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": utc_timestamp(),
        **payload,
    }
    if FileLock is None:
        with log_path.open("a", encoding="utf-8") as handle:
            json.dump(record, handle)
            handle.write("\n")
        from .state import rotate_log  # late import to avoid circular dependency
        rotate_log(log_path, max_bytes=_LOG_MAX_BYTES)
    else:
        from .state import rotate_log  # late import to avoid circular dependency
        try:
            # First try a short timeout; on contention retry with a longer
            # one (rotation is fast, so a real wedge is rare). If both fail,
            # drop the entry with a throttled stderr warning rather than
            # writing unlocked — an unlocked append would race with the
            # rotator's os.replace and silently lose the line anyway.
            try:
                _lock = FileLock(lock_path, timeout=2)
                _lock.acquire()
            except FileLockTimeout:
                _lock = FileLock(lock_path, timeout=10)
                _lock.acquire()
            try:
                with log_path.open("a", encoding="utf-8") as handle:
                    json.dump(record, handle)
                    handle.write("\n")
                rotate_log(log_path, max_bytes=_LOG_MAX_BYTES, held_lock=_lock)
            finally:
                _lock.release()
        except FileLockTimeout:
            _warn_lock_timeout(name)
