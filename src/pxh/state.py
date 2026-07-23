from __future__ import annotations

import json
import hashlib
import logging
import os
import random
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from filelock import FileLock, Timeout as FileLockTimeout
except ImportError:
    FileLock = None  # deferred — only needed by session lock functions
    FileLockTimeout = None

from .logging import log_event
from .time import utc_timestamp

_log = logging.getLogger("pxh.state")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SESSION_LOCK_TIMEOUT_S = 0.25
SESSION_LOCK_ATTEMPTS = 4


class SessionBusyError(RuntimeError):
    """Raised when a session write cannot acquire its bounded shared lock."""


def _trim_corrupt_backups(path: Path, keep: int = 3) -> None:
    """Delete all but the newest `keep` .corrupt.* backups next to `path`."""
    pattern = path.name + ".corrupt."
    backups = sorted(path.parent.glob(pattern + "*"), key=lambda p: p.stat().st_mtime)
    for old in backups[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass


def atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically via temp file + os.replace.

    Attempts to preserve original file's ownership (skipped silently if caller
    lacks privileges) and sets mode 0o644 so that cross-user writers
    (root px-alive, pi px-mind) don't lock each other out.
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
    chunk_size and n larger than fits in one chunk.

    Note: uses .splitlines() which silently drops a trailing empty line for
    files ending in ``\\n`` (POSIX convention). Callers that need exact line
    counts should be aware that the result may contain up to 1 fewer line
    than ``n`` when the file ends with a newline (issue #140)."""
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


def rotate_log(path: Path, max_bytes: int = 5_000_000, held_lock: "FileLock | None" = None) -> None:
    """Rotate log file by keeping the last half of lines when it exceeds max_bytes.

    Uses atomic_write for SD card durability. Callers should hold the .rotlock
    across the append + rotate to prevent TOCTOU races (issue #149). Pass the
    held lock via held_lock to skip re-acquisition (FileLock is not reentrant).
    """
    # Fast-path size check before any lock acquisition — avoids hitting the
    # filesystem for FileLock on every tiny log append.
    try:
        if not path.exists() or path.stat().st_size <= max_bytes:
            return
    except OSError:
        return

    def _rotate_inner() -> None:
        if not path.exists() or path.stat().st_size <= max_bytes:
            return
        lines = path.read_text(encoding="utf-8").splitlines()
        half = len(lines) // 2
        atomic_write(path, "\n".join(lines[half:]) + "\n")

    if FileLock is None:
        # Best-effort fallback when filelock isn't available; legacy behavior.
        try:
            _rotate_inner()
        except OSError as exc:
            _log.warning("rotate_log: %s: %s", path, exc)
        return

    try:
        if held_lock is not None:
            # Caller already holds the lock — rotate directly.
            _rotate_inner()
        else:
            from filelock import Timeout as _FLTimeout
            lock_path = str(path) + ".rotlock"
            try:
                with FileLock(lock_path, timeout=2):
                    _rotate_inner()
            except _FLTimeout:
                # Another rotator owns the lock — they'll handle it.
                return
    except OSError as exc:
        # Disk full, permissions, etc. — surface but don't crash the caller.
        _log.warning("rotate_log: %s: %s", path, exc)


STATE_DIR = Path(os.environ.get("PX_STATE_DIR", str(PROJECT_ROOT / "state")))
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
        # Robot's name — Obi calls it Spark (consumed by mcp_server status)
        "robot_name": "Spark",
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


def _session_lock_path(path: Path) -> Path:
    root = Path(os.environ.get(
        "PX_SESSION_LOCK_DIR",
        str(Path(tempfile.gettempdir()) / "pxh-session-locks"),
    ))
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o1777)
    except OSError:
        pass
    key = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:24]
    return root / f"{key}.lock"


@contextmanager
def _session_write_lock(path: Path):
    _require_filelock()
    timeout = float(os.environ.get("PX_SESSION_LOCK_TIMEOUT", SESSION_LOCK_TIMEOUT_S))
    attempts = int(os.environ.get("PX_SESSION_LOCK_ATTEMPTS", SESSION_LOCK_ATTEMPTS))
    lock = FileLock(str(_session_lock_path(path)), timeout=timeout, mode=0o666)
    for attempt in range(max(1, attempts)):
        try:
            lock.acquire()
            try:
                _session_lock_path(path).chmod(0o666)
            except OSError:
                pass
            break
        except FileLockTimeout as exc:
            if attempt + 1 >= attempts:
                raise SessionBusyError(
                    f"session state busy after {max(1, attempts)} bounded attempts"
                ) from exc
            time.sleep(random.uniform(0.01, 0.04))
    try:
        yield
    finally:
        if lock.is_locked:
            lock.release()


def _read_session_snapshot(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default_state()


def _load_session_for_write(path: Path) -> Dict[str, Any]:
    if not path.exists():
        if TEMPLATE_PATH.exists():
            try:
                return json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return default_state()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = default_state()
        corrupt_backup = path.parent / (path.name + f".corrupt.{int(time.time())}")
        try:
            path.rename(corrupt_backup)
        except OSError:
            pass
        _trim_corrupt_backups(path, keep=3)
        log_event(
            "state-corruption",
            {"path": str(path), "message": "session.json was corrupt; reset to default state"},
        )
        return data


def ensure_session() -> Path:
    path = session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _session_write_lock(path):
        if not path.exists():
            data = _load_session_for_write(path)
            atomic_write(path, json.dumps(data, indent=2) + "\n")
    return path


def load_session() -> Dict[str, Any]:
    """Read an atomic session snapshot without waiting for a writer."""
    path = session_path()
    if not path.exists():
        try:
            ensure_session()
        except SessionBusyError:
            return default_state()
    return _read_session_snapshot(path)


def load_session_readonly() -> Dict[str, Any]:
    """Read session.json without acquiring the FileLock.

    Safe for read-only callers (public API) because writes use atomic
    os.replace — readers always see a complete file. May return slightly
    stale data during a concurrent write, which is acceptable for display.
    """
    return _read_session_snapshot(session_path())


def save_session(data: Dict[str, Any]) -> None:
    path = session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _session_write_lock(path):
        atomic_write(path, json.dumps(data, indent=2) + "\n")


def update_session(
    fields: Optional[Dict[str, Any]] = None,
    history_entry: Optional[Dict[str, Any]] = None,
    history_limit: int = 100,
) -> Dict[str, Any]:
    path = session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _session_write_lock(path):
        data = _load_session_for_write(path)

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
