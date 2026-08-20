"""Tests for yield_alive() in bin/px-env — the GPIO handoff gate (#205).

yield_alive sends SIGUSR1 to px-alive, then polls /proc/PID until the
process exits.  If px-alive is still alive after the poll window,
yield_alive MUST return non-zero so the caller (running under set -e)
aborts before opening Picarx and racing px-alive for GPIO5.
"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_LINUX_ONLY = pytest.mark.skipif(
    sys.platform != "linux", reason="/proc/{pid} only exists on Linux"
)


def _source_and_call(log_dir: Path, poll_iters: str | None = None) -> subprocess.CompletedProcess:
    """Source px-env in a subshell and invoke yield_alive.

    A subshell is used so that ``set -e`` aborts the subshell (and we
    observe the exit code) rather than the pytest process.

    ``poll_iters`` overrides the 25-iteration poll loop via
    PX_YIELD_ALIVE_POLL_ITERS so tests don't wait 5s.  The logic under
    test (timeout → return 1) is identical regardless of iteration count.
    """
    env = os.environ.copy()
    env["LOG_DIR"] = str(log_dir)
    if poll_iters is not None:
        env["PX_YIELD_ALIVE_POLL_ITERS"] = poll_iters

    script = r"""
set -euo pipefail
source "$SCRIPT_DIR/px-env"
yield_alive
echo "yield_alive returned $?"
"""
    return subprocess.run(
        ["bash", "-c", script],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        env={**env, "SCRIPT_DIR": str(PROJECT_ROOT / "bin")},
        timeout=30,
    )


def _make_zombie_proxy(log_dir: Path) -> int:
    """Spawn a sleep process that ignores SIGUSR1 and write its PID.

    This simulates a px-alive that received SIGUSR1 but did not exit —
    the exact race condition described in #205.
    """
    proc = subprocess.Popen(
        ["python3", "-c",
         "import signal, time; signal.signal(signal.SIGUSR1, signal.SIG_IGN); time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pid_file = log_dir / "px-alive.pid"
    pid_file.write_text(str(proc.pid))
    return proc.pid


@pytest.fixture
def long_poll_env(tmp_path):
    """Ensure the poll loop runs long enough to observe the timeout.

    The production default is 25 iterations × 0.2s = 5s.  We keep the
    real default so the test exercises the actual production path.
    """
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    return log_dir


@_LINUX_ONLY
def test_yield_alive_returns_failure_when_px_alive_does_not_exit(long_poll_env):
    """If px-alive is still alive after the poll loop, yield_alive must
    return non-zero — never fall through to sleep and success (#205).

    Uses PX_YIELD_ALIVE_POLL_ITERS=3 so the test waits ~0.6s instead of
    5s; the timeout-is-failure logic is identical regardless of the
    iteration count.
    """
    log_dir = long_poll_env
    pid = _make_zombie_proxy(log_dir)
    try:
        result = _source_and_call(log_dir, poll_iters="3")
        # Under set -e, a non-zero return from yield_alive aborts the
        # subshell before the echo runs.  So the exit code is 1, not 0.
        assert result.returncode != 0, (
            f"yield_alive returned 0 even though px-alive (PID {pid}) was still alive — "
            f"this is the #205 false-success race. stderr: {result.stderr[:500]}"
        )
        assert "did not exit" in result.stderr, (
            f"expected timeout message on stderr, got: {result.stderr[:500]}"
        )
    finally:
        # Clean up the zombie proxy
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@_LINUX_ONLY
def test_yield_alive_returns_success_when_px_alive_exits(long_poll_env):
    """When px-alive does exit within the poll window, yield_alive
    returns 0 as before — the fix must not break the normal path.
    """
    log_dir = long_poll_env
    # Spawn a process that exits on SIGUSR1
    proc = subprocess.Popen(
        ["python3", "-c",
         "import signal, time; "
         "signal.signal(signal.SIGUSR1, lambda *_: exit(0)); "
         "time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pid_file = log_dir / "px-alive.pid"
    pid_file.write_text(str(proc.pid))
    try:
        # poll_iters=25 gives the full 5s window — the process needs
        # time to receive SIGUSR1, run the handler, and exit.  3 iters
        # (0.6s) is not enough on CI runners where signal delivery is
        # slow under load.
        result = _source_and_call(log_dir, poll_iters="25")
        assert result.returncode == 0, (
            f"yield_alive should return 0 when px-alive exits cleanly, "
            f"got {result.returncode}. stderr: {result.stderr[:500]}"
        )
        assert "yield_alive returned 0" in result.stdout
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_yield_alive_no_pid_file_is_success(long_poll_env):
    """No pid file → no px-alive running → yield_alive returns 0."""
    log_dir = long_poll_env
    # Don't create a pid file
    result = _source_and_call(log_dir)
    assert result.returncode == 0
    assert "yield_alive returned 0" in result.stdout


def test_yield_alive_dead_pid_is_success(long_poll_env):
    """A stale pid file pointing to a dead PID → yield_alive returns 0."""
    log_dir = long_poll_env
    # Write a PID that doesn't exist (high number unlikely to be in use)
    pid_file = log_dir / "px-alive.pid"
    pid_file.write_text("999999")
    result = _source_and_call(log_dir)
    assert result.returncode == 0
    assert "yield_alive returned 0" in result.stdout