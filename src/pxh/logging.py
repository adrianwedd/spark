from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
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

def log_dir() -> Path:
    """The log directory, resolved at call time.

    LOG_DIR below is bound at import, and log_event read that module global —
    so the documented LOG_DIR override could never take effect in-process. It
    remains as a module attribute for back-compat; new code calls this.
    """
    return _resolve_log_dir()


LOG_DIR = _resolve_log_dir()


_LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per log file

# Sticky + world-writable, like state/health/. Motion tools
# (tool-drive, tool-circle, tool-look, ...) sudo-elevate to root for GPIO and
# are the ones most likely to log first; everything else, including the same
# tool name reached a different way, runs as pi. A root-created 0755 logs/
# would lock pi out of creating a new file there at all; sticky keeps one uid
# from deleting another's file once it exists.
_LOG_DIR_MODE = 0o1777


def _ensure_log_dir_mode(d: Path) -> None:
    try:
        if (d.stat().st_mode & 0o7777) != _LOG_DIR_MODE:
            os.chmod(d, _LOG_DIR_MODE)
    except OSError:
        pass  # not the owner — the creating user already set the mode, or we lose


def _relax_mode(path, owner_like_dir: Path) -> None:
    """Make a rotlock or log file writable by every uid that might need it.

    chmod alone is not enough on this host: fs.protected_regular=2 (a live,
    intentional Debian kernel hardening default — confirmed via `sysctl
    fs.protected_regular`, not something to weaken) blocks *opening* a
    regular file for writing whenever the file's owner is neither the
    directory's owner nor the calling uid, regardless of the file's own mode
    bits, whenever the open call carries O_CREAT — which every plain
    open(path, "a") does, and which FileLock's own first acquire() does too.
    A root-created rotlock or log file chmod'd to 0666 still 13/EACCES's a
    later pi append under this sysctl; only matching the file's owner to the
    directory's owner satisfies the kernel's exemption.

    Only the uid that currently holds the file (its creator, or root) can
    chown it — a later, different-uid caller that can't even open the file
    never reaches this line. That is exactly the failure this closes:
    whichever uid creates a file first relaxes it immediately, so every
    subsequent acquirer/appender of any uid can open it too.
    """
    try:
        os.chmod(path, 0o666)
    except OSError:
        pass
    try:
        dir_stat = owner_like_dir.stat()
        os.chown(path, dir_stat.st_uid, dir_stat.st_gid)
    except OSError:
        pass  # not privileged to chown — the creating user's uid is what we get


class StallProofLineWriter:
    """Write log lines from a background thread so a caller never blocks on IO.

    Built for the capture path (#283). `ArecordStream`'s reader thread is what
    keeps `arecord` drained, and it logs — ring-buffer drops, and the stderr
    thread's ALSA overruns — while holding the stream's condition lock, so a log
    write that blocks stalls the drain *and* every consumer waiting on that lock.
    A blocked `print` to a journald socket or a file write to a card that cannot
    take it therefore turns a short hiccup into arecord overflowing its own ALSA
    buffer, which is then logged by the same blocked path — a feedback loop that
    converts hundreds of milliseconds of trouble into seconds of lost audio.

    The invariant this encodes: **a daemon's evidence must not be produced by the
    machinery that is losing the data.**

    FIFO through one writer thread. The queue is bounded and drops the *oldest*
    line, recording the gap as a marker line rather than growing without limit —
    losing the oldest lines of a stall is survivable, growing until the OOM
    killer arrives is not. `write()` never raises and never blocks; `flush()`
    exists for exit paths that must not lose the last lines.
    """

    _FLUSH = object()

    def __init__(self, path: Path, *, queue_max: int = 4096, echo=None,
                 checks_per_rotate: int = 200):
        self.path = Path(path)
        self._echo = echo
        self._queue: "queue.Queue" = queue.Queue(maxsize=queue_max)
        self._dropped = 0
        self._dropped_reported = 0
        self._lines_since_rotate_check = 0
        self._checks_per_rotate = max(1, checks_per_rotate)
        self._flushed = threading.Event()
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="log-writer", daemon=True)
        self._thread.start()

    # -- producer side -----------------------------------------------------

    def write(self, line: str) -> None:
        """Queue one line. Never blocks, never raises."""
        if self._closed:
            return
        try:
            self._queue.put_nowait(line)
        except queue.Full:
            # Drop the *oldest* line and say so, rather than losing the newest
            # (which is the one that describes the stall in progress).
            try:
                self._queue.get_nowait()
                self._queue.task_done()
                self._dropped += 1
            except (queue.Empty, ValueError):
                pass
            try:
                self._queue.put_nowait(line)
            except queue.Full:
                pass
        except Exception:  # pragma: no cover — logging never load-bearing
            pass

    def flush(self, timeout: float = 2.0) -> None:
        """Block until everything queued so far has been written (or timeout)."""
        if self._closed:
            return
        try:
            self._flushed.clear()
            self._queue.put(self._FLUSH, timeout=timeout)
            self._flushed.wait(timeout=timeout)
        except Exception:
            pass

    def close(self, timeout: float = 2.0) -> None:
        self.flush(timeout=timeout)
        self._closed = True

    @property
    def dropped(self) -> int:
        return self._dropped

    # -- writer side -------------------------------------------------------

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._FLUSH:
                    self._flushed.set()
                    continue
                self._write_line(item)
            except Exception:
                pass  # a logging failure must never take the writer down
            finally:
                try:
                    self._queue.task_done()
                except ValueError:
                    pass

    def _write_line(self, line: str) -> None:
        if self._dropped > self._dropped_reported:
            gap = self._dropped - self._dropped_reported
            self._dropped_reported = self._dropped
            self._append(f"[pxh.logging] dropped {gap} log line(s) under IO pressure")
        self._append(line)
        if self._echo is not None:
            try:
                self._echo(line)
            except Exception:
                pass

    def _append(self, line: str) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            return
        self._lines_since_rotate_check += 1
        if self._lines_since_rotate_check >= self._checks_per_rotate:
            self._lines_since_rotate_check = 0
            try:
                if self.path.stat().st_size > _LOG_MAX_BYTES:
                    self._rotate()
            except OSError:
                pass

    def _rotate(self) -> None:
        """Keep the last half — the same policy the wake listener had inline."""
        lines = self.path.read_text(encoding="utf-8").splitlines()
        half = lines[len(lines) // 2:]
        tmp = self.path.with_suffix(self.path.suffix + ".rot")
        tmp.write_text("\n".join(half) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)


def log_event(name: str, payload: Mapping[str, Any]) -> None:
    """Append a structured log entry under logs/tool-<name>.log."""
    base = log_dir()
    log_path = base / f"tool-{name}.log"
    lock_path = str(log_path) + ".rotlock"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_log_dir_mode(log_path.parent)
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
            #
            # A rotlock or log file created by a different uid before this fix
            # shipped (or before _relax_mode below ever ran on it) can raise
            # PermissionError/OSError right out of os.open(), before the
            # library's own timeout logic ever gets a chance to run — a mixed
            # root/pi log target must never crash its caller over a missing
            # log line, so this is caught exactly like a timeout.
            try:
                _lock = FileLock(lock_path, timeout=2)
                _lock.acquire()
            except FileLockTimeout:
                _lock = FileLock(lock_path, timeout=10)
                _lock.acquire()
            _relax_mode(lock_path, log_path.parent)
            try:
                with log_path.open("a", encoding="utf-8") as handle:
                    json.dump(record, handle)
                    handle.write("\n")
                _relax_mode(log_path, log_path.parent)
                rotate_log(log_path, max_bytes=_LOG_MAX_BYTES, held_lock=_lock)
            finally:
                _lock.release()
        except (FileLockTimeout, OSError):
            _warn_lock_timeout(name)
