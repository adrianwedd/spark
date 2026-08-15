"""Where runtime (as opposed to durable) state lives.

The px-alive heartbeat is disposable liveness data: rewritten every loop,
meaningless after a power cut. Keeping it on the SD card was actively harmful.
Measured on the live Pi, a 169-byte fsync+replace into ``state/`` has a p50 of
12 ms but a tail reaching 21.5 s under load. ``WatchdogSec=15`` sits under that
tail, so systemd was SIGABRT-ing a perfectly healthy daemon — and because the
process was blocked in uninterruptible I/O it took a SIGKILL to actually die.
The identical write to tmpfs measures 0.63 ms with no tail.

Writer (``bin/px-alive``) and readers (``pxh.api``) must agree on the location,
so the env var name and default live here rather than in either of them.
"""
from __future__ import annotations

import os
from pathlib import Path

HEARTBEAT_DIR_ENV = "PX_ALIVE_HEARTBEAT_DIR"
DEFAULT_HEARTBEAT_DIR = Path("/run/spark")
HEARTBEAT_FILENAME = "alive_heartbeat.json"


def heartbeat_dir_candidates(state_dir: Path | str) -> list[Path]:
    """Preferred runtime dir first, then the state dir as fallback.

    The state dir stays in the list on purpose: non-systemd hosts, containers
    and the test suite have no ``/run/spark``, and during a rollout the old
    heartbeat is still sitting in ``state/`` until the new daemon starts.
    """
    configured = os.environ.get(HEARTBEAT_DIR_ENV, "").strip()
    preferred = Path(configured) if configured else DEFAULT_HEARTBEAT_DIR
    return [preferred, Path(state_dir)]


def resolve_heartbeat_write_dir(state_dir: Path | str) -> Path:
    """First writable candidate, creating it if needed. Never raises."""
    for candidate in heartbeat_dir_candidates(state_dir):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        if os.access(candidate, os.W_OK):
            return candidate
    return Path(state_dir)


def resolve_heartbeat_read_path(state_dir: Path | str) -> Path:
    """First candidate that actually has a heartbeat file.

    Falls back to the state-dir path when none exists, so callers still report
    a coherent "missing" rather than pointing at a directory that was never used.
    """
    for candidate in heartbeat_dir_candidates(state_dir):
        path = candidate / HEARTBEAT_FILENAME
        if path.exists():
            return path
    return Path(state_dir) / HEARTBEAT_FILENAME
