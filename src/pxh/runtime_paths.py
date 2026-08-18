"""Where runtime (as opposed to durable) state lives.

px-alive publishes two files that look like state but are not: the heartbeat
and the live sonar reading. Both are rewritten every loop and meaningless
after a power cut. Keeping them on the SD card was actively harmful.

Measured on the live Pi, a 169-byte fsync+replace into ``state/`` has a p50 of
12 ms but a tail reaching 21.5 s under load. ``WatchdogSec=15`` sits under that
tail, so systemd was SIGABRT-ing a perfectly healthy daemon — and because the
process was blocked in uninterruptible I/O it took a SIGKILL to actually die.
The identical write to tmpfs measures 0.63 ms with no tail.

The heartbeat moved first. The sonar write was left behind and kept the storm
alive on its own: a /proc sampler caught px-alive in uninterruptible sleep on
``fsync`` of ``state/tmp<rand>.tmp`` in 27 of 58 samples, 24 of them parked in
``jbd2_log_wait_commit``, with 23 consecutive samples on a single temp file.
That was 66 of 86 watchdog kills in a measured 6h window.

Anything else that lands here later gets the same treatment, which is why the
helpers below are generic in the filename rather than one pair per file.

Writer (``bin/px-alive``, root) and readers (``pxh.api``, ``pxh.health``,
``pxh.mind``, ``pxh.mcp_server``, all as ``pi``) must agree on the location, so
the env var name and default live here rather than in any of them.
"""
from __future__ import annotations

import os
from pathlib import Path

# Historical name — it predates the sonar file and is baked into the test
# suite and conftest. It governs the whole runtime directory, not just the
# heartbeat.
RUNTIME_DIR_ENV = "PX_ALIVE_HEARTBEAT_DIR"
HEARTBEAT_DIR_ENV = RUNTIME_DIR_ENV  # back-compat alias

DEFAULT_RUNTIME_DIR = Path("/run/spark")
DEFAULT_HEARTBEAT_DIR = DEFAULT_RUNTIME_DIR  # back-compat alias

HEARTBEAT_FILENAME = "alive_heartbeat.json"
SONAR_LIVE_FILENAME = "sonar_live.json"


def runtime_dir_candidates(state_dir: Path | str) -> list[Path]:
    """Preferred runtime dir first, then the state dir as fallback.

    The state dir stays in the list on purpose: non-systemd hosts, containers
    and the test suite have no ``/run/spark``, and during a rollout the old
    file is still sitting in ``state/`` until the new daemon starts.
    """
    configured = os.environ.get(RUNTIME_DIR_ENV, "").strip()
    preferred = Path(configured) if configured else DEFAULT_RUNTIME_DIR
    return [preferred, Path(state_dir)]


def resolve_runtime_write_dir(state_dir: Path | str) -> Path:
    """First writable candidate, creating it if needed. Never raises."""
    for candidate in runtime_dir_candidates(state_dir):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        if os.access(candidate, os.W_OK):
            return candidate
    return Path(state_dir)


def resolve_runtime_read_path(state_dir: Path | str, filename: str) -> Path:
    """First candidate that actually has ``filename``.

    Falls back to the state-dir path when none exists, so callers still report
    a coherent "missing" rather than pointing at a directory that was never
    used. Readers are all age-gated, so a stale pre-migration copy left in
    ``state/`` reads as stale rather than as a fresh value.
    """
    for candidate in runtime_dir_candidates(state_dir):
        path = candidate / filename
        if path.exists():
            return path
    return Path(state_dir) / filename


# --- Back-compat wrappers -------------------------------------------------
# Named for the heartbeat because that is what they were introduced for; kept
# so existing call sites and tests do not have to churn.

heartbeat_dir_candidates = runtime_dir_candidates


def resolve_heartbeat_write_dir(state_dir: Path | str) -> Path:
    return resolve_runtime_write_dir(state_dir)


def resolve_heartbeat_read_path(state_dir: Path | str) -> Path:
    return resolve_runtime_read_path(state_dir, HEARTBEAT_FILENAME)


def resolve_sonar_live_read_path(state_dir: Path | str) -> Path:
    """Where readers should look for px-alive's live sonar reading."""
    return resolve_runtime_read_path(state_dir, SONAR_LIVE_FILENAME)
