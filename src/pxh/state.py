from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from filelock import FileLock
except ImportError:
    FileLock = None  # deferred — only needed by session lock functions

from .logging import log_event
from .time import utc_timestamp

_log = logging.getLogger("pxh.state")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
LOCK_TIMEOUT_S = 10  # seconds — fail fast rather than hang forever


def atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically via temp file + os.replace.

    Preserves original file's ownership and sets mode 0o644 so that
    cross-user writers (root px-alive, pi px-mind) don't lock each other out.
    """
    # Capture original ownership before replacing
    try:
        st = path.stat()
        orig_uid, orig_gid = st.st_uid, st.st_gid
    except FileNotFoundError:
        orig_uid, orig_gid = None, None

    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        if orig_uid is not None:
            try:
                os.chown(tmp, orig_uid, orig_gid)
            except OSError:
                pass  # non-root can't chown — mode 0o644 is sufficient
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def tail_lines(path: "Path", n: int = 10, chunk_size: int = 8192) -> list:
    """Read the last n lines of a file by seeking backward in chunks until
    n+1 newlines are accumulated or BOF is reached. Handles lines longer than
    chunk_size and n larger than fits in one chunk."""
    if n <= 0:
        return []
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            end = f.tell()
            if end == 0:
                return []
            buf = b""
            pos = end
            while pos > 0 and buf.count(b"\n") <= n:
                read_size = min(chunk_size, pos)
                pos -= read_size
                f.seek(pos)
                buf = f.read(read_size) + buf
            return buf.decode("utf-8", errors="replace").splitlines()[-n:]
    except (FileNotFoundError, OSError):
        return []


def rotate_log(path: Path, max_bytes: int = 5_000_000) -> None:
    """Rotate log file by keeping the last half of lines when it exceeds max_bytes.

    Uses atomic_write for SD card durability and a sibling FileLock so concurrent
    appenders don't lose tail entries between the read and the os.replace
    (issue #149). Silently handles missing files and write errors.
    """
    if FileLock is None:
        # Best-effort fallback when filelock isn't available; legacy behavior.
        try:
            if not path.exists() or path.stat().st_size <= max_bytes:
                return
            lines = path.read_text(encoding="utf-8").splitlines()
            half = len(lines) // 2
            atomic_write(path, "\n".join(lines[half:]) + "\n")
        except Exception:
            pass
        return
    try:
        if not path.exists() or path.stat().st_size <= max_bytes:
            return
        lock_path = str(path) + ".rotlock"
        # Short timeout — if we can't acquire, another process is rotating
        # (or appending heavily); skip this round, try again next cycle.
        with FileLock(lock_path, timeout=2):
            # Re-check size under the lock — another rotator may have already run.
            if not path.exists() or path.stat().st_size <= max_bytes:
                return
            lines = path.read_text(encoding="utf-8").splitlines()
            half = len(lines) // 2
            atomic_write(path, "\n".join(lines[half:]) + "\n")
    except Exception:
        pass  # log rotation failure is not fatal


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
        "roaming_allowed": False,
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
        "obi_story_lines": [],
        "spark_quiet_mode": False,
        "history": [],
    }


def session_path() -> Path:
    override = os.environ.get("PX_SESSION_PATH")
    if override:
        return Path(override)
    return DEFAULT_SESSION_PATH


def _require_filelock():
    """Raise a clear error if filelock is not installed."""
    if FileLock is None:
        raise ImportError(
            "filelock is required for session management. "
            "Install it: pip install filelock"
        )


def ensure_session() -> Path:
    _require_filelock()
    path = session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = str(path) + ".lock"
    with FileLock(lock_path, timeout=LOCK_TIMEOUT_S):
        if not path.exists():
            if TEMPLATE_PATH.exists():
                atomic_write(path, TEMPLATE_PATH.read_text(encoding="utf-8"))
            else:
                atomic_write(path, json.dumps(default_state(), indent=2) + "\n")
    return path


def load_session() -> Dict[str, Any]:
    path = ensure_session()
    lock_path = str(path) + ".lock"
    with FileLock(lock_path, timeout=LOCK_TIMEOUT_S):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            _log.warning("session.json corrupt — resetting to defaults: %s", path)
            data = default_state()
            atomic_write(path, json.dumps(data, indent=2) + "\n")
            return data


def load_session_readonly() -> Dict[str, Any]:
    """Read session.json without acquiring the FileLock.

    Safe for read-only callers (public API) because writes use atomic
    os.replace — readers always see a complete file. May return slightly
    stale data during a concurrent write, which is acceptable for display.
    """
    path = session_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default_state()


def save_session(data: Dict[str, Any]) -> None:
    path = ensure_session()
    lock_path = str(path) + ".lock"
    with FileLock(lock_path, timeout=LOCK_TIMEOUT_S):
        atomic_write(path, json.dumps(data, indent=2) + "\n")


def update_session(
    fields: Optional[Dict[str, Any]] = None,
    history_entry: Optional[Dict[str, Any]] = None,
    history_limit: int = 100,
) -> Dict[str, Any]:
    # Call ensure_session BEFORE acquiring the lock — ensure_session acquires
    # the same lock internally and FileLock is not reentrant.
    path = ensure_session()
    lock_path = str(path) + ".lock"
    with FileLock(lock_path, timeout=LOCK_TIMEOUT_S):
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

        atomic_write(path, json.dumps(data, indent=2) + "\n")
        return data