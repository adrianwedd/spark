from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

try:
    from filelock import FileLock
except ImportError:
    FileLock = None  # deferred — only needed by session lock functions

from . import quiet_mode
from .logging import log_event
from .time import utc_timestamp

_log = logging.getLogger("pxh.state")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
LOCK_TIMEOUT_S = 10  # seconds — fail fast rather than hang forever


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
        # spark_quiet_mode is derived at read time from quiet_state (#209) —
        # kept so every pre-existing reader (policy.py, ~30 tests) still sees
        # a plain bool without needing to know quiet_state exists.
        "spark_quiet_mode": False,
        "quiet_state": None,
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


def _with_resolved_quiet_mode(data: Dict[str, Any]) -> Dict[str, Any]:
    """Derive the legacy `spark_quiet_mode` bool from `quiet_state` (#209).

    Every existing reader — policy.py's `is True` check chief among them —
    keeps working against a plain bool without knowing `quiet_state` exists.
    Read-time only: this does not write anything back, so a lapsed temporary
    window reads as inactive here without anyone having cleared the record
    on disk (see `quiet_mode.resolve`).
    """
    data["spark_quiet_mode"] = quiet_mode.resolve(data, now=time.time())
    return data


def load_session() -> Dict[str, Any]:
    path = ensure_session()
    lock_path = str(path) + ".lock"
    with FileLock(lock_path, timeout=LOCK_TIMEOUT_S):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            _log.warning("session.json corrupt — resetting to defaults: %s", path)
            corrupt_backup = path.parent / (path.name + f".corrupt.{int(time.time())}")
            try:
                path.rename(corrupt_backup)
                _log.warning("corrupt session backed up to %s", corrupt_backup)
            except OSError:
                pass
            _trim_corrupt_backups(path, keep=3)
            data = default_state()
            atomic_write(path, json.dumps(data, indent=2) + "\n")
        return _with_resolved_quiet_mode(data)


def load_session_readonly() -> Dict[str, Any]:
    """Read session.json without acquiring the FileLock.

    Safe for read-only callers (public API) because writes use atomic
    os.replace — readers always see a complete file. May return slightly
    stale data during a concurrent write, which is acceptable for display.
    """
    path = session_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = default_state()
    return _with_resolved_quiet_mode(data)


def save_session(data: Dict[str, Any]) -> None:
    path = ensure_session()
    lock_path = str(path) + ".lock"
    with FileLock(lock_path, timeout=LOCK_TIMEOUT_S):
        atomic_write(path, json.dumps(data, indent=2) + "\n")


def update_session(
    fields: Optional[Dict[str, Any]] = None,
    history_entry: Optional[Dict[str, Any]] = None,
    history_entry_fn: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    history_limit: int = 100,
    remove_fields: Optional[list] = None,
) -> Dict[str, Any]:
    """`history_entry_fn`, if given, is called with the pre-mutation `data`
    while the lock is held, and its return value is used as `history_entry`.
    This is the only race-safe way for a caller to log "previous state" —
    reading current state before calling update_session() and computing a
    diff outside the lock would TOCTOU against a concurrent writer.

    `remove_fields`, if given, deletes those keys from `data` after `fields`
    is merged in — the only way to retire a persisted key (e.g. a legacy
    field superseded by a newer one) rather than just overwrite its value.
    """
    # Call ensure_session BEFORE acquiring the lock — ensure_session acquires
    # the same lock internally and FileLock is not reentrant.
    path = ensure_session()
    lock_path = str(path) + ".lock"
    with FileLock(lock_path, timeout=LOCK_TIMEOUT_S):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = default_state()
            corrupt_backup = path.parent / (path.name + f".corrupt.{int(time.time())}")
            try:
                path.rename(corrupt_backup)
            except OSError:
                pass
            log_event("state-corruption", {"path": str(path), "message": "session.json was corrupt; reset to default state"})

        if history_entry_fn is not None:
            history_entry = history_entry_fn(data)
        if fields:
            data.update(fields)
        if remove_fields:
            for key in remove_fields:
                data.pop(key, None)
        if history_entry:
            entry = {"ts": utc_timestamp(), **history_entry}
            history = data.setdefault("history", [])
            history.append(entry)
            if len(history) > history_limit:
                data["history"] = history[-history_limit:]

        atomic_write(path, json.dumps(data, indent=2) + "\n")
        return data


def set_quiet_mode(
    *,
    enabled: bool,
    source: str,
    reason: Optional[str] = None,
    ttl_s: Optional[float] = None,
) -> Dict[str, Any]:
    """The one canonical quiet-mode writer (#209) — `bin/tool-quiet`,
    `bin/tool-transition`, `bin/px-spark` and the dashboard PATCH all route
    through this (or `clear_quiet_mode`) instead of writing `spark_quiet_mode`
    directly.

    `quiet_state` is the only thing persisted to disk. `spark_quiet_mode` is
    never written here — it stays a read-time compatibility field derived by
    `load_session()`/`load_session_readonly()` (`_with_resolved_quiet_mode`),
    so there is exactly one on-disk record of quiet mode and it cannot go
    stale relative to itself. Any legacy `spark_quiet_mode` already on disk
    (e.g. from `default_state()`'s bootstrap, written before `quiet_state`
    ever existed) is actively removed on every call, not just left unwritten
    — a stale bool sitting next to a disagreeing `quiet_state` is exactly the
    kind of value that misled `bin/px-spark`'s old direct write. The dict
    returned to the caller is enriched with the same derivation before it
    goes back, so callers that inspect the return value (the dashboard PATCH
    response, tool payloads) still see a correct `spark_quiet_mode` — it just
    was never written to the file.

    Always logs a history entry, closing the gap where the dashboard PATCH
    previously toggled the flag with no record of who or why. The entry
    records `previous_enabled` alongside the new `enabled`, computed from the
    pre-mutation record under the same lock (no TOCTOU against a concurrent
    writer), so a reader can distinguish a real transition from an idempotent
    reaffirmation (`transition: previous_enabled != enabled`) instead of only
    ever seeing the new state. `ttl_s=None` is indefinite (the Three S's
    protocol); a caller that wants a bounded buffer (`bin/tool-transition`)
    passes a TTL in seconds.
    """
    now = time.time()
    expires_at = now + ttl_s if ttl_s is not None else None
    record = quiet_mode.new_state(
        enabled=enabled, source=source, reason=reason, set_at=now, expires_at=expires_at,
    )

    def _history_entry(pre_mutation_data: Dict[str, Any]) -> Dict[str, Any]:
        previous_enabled = quiet_mode.resolve(pre_mutation_data, now=now)
        return {
            "event": "quiet_mode_set" if enabled else "quiet_mode_clear",
            "source": source,
            "reason": reason,
            "previous_enabled": previous_enabled,
            "enabled": enabled,
            "transition": previous_enabled != enabled,
            "expires_at": expires_at,
        }

    data = update_session(
        fields={"quiet_state": record},
        remove_fields=["spark_quiet_mode"],
        history_entry_fn=_history_entry,
    )
    return _with_resolved_quiet_mode(data)


def clear_quiet_mode(*, source: str, reason: Optional[str] = None) -> Dict[str, Any]:
    """Convenience wrapper — `set_quiet_mode(enabled=False, ...)`."""
    return set_quiet_mode(enabled=False, source=source, reason=reason, ttl_s=None)