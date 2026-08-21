"""Tests for yield_alive() in bin/px-env — the GPIO handoff gate (#205).

yield_alive sends SIGUSR1 to px-alive, then polls /proc/PID until the
process exits.  If px-alive is still alive after the poll window,
yield_alive MUST return non-zero so the caller (running under set -e)
aborts before opening Picarx and racing px-alive for GPIO5.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_LINUX_ONLY = pytest.mark.skipif(
    sys.platform != "linux", reason="/proc/{pid} only exists on Linux"
)


def _source_and_call(
    log_dir: Path,
    poll_iters: str | None = None,
    heartbeat_file: Path | None = None,
    max_wait_s: str | None = None,
    stale_after_s: str | None = None,
    subprocess_timeout: float = 30,
) -> subprocess.CompletedProcess:
    """Source px-env in a subshell and invoke yield_alive.

    A subshell is used so that ``set -e`` aborts the subshell (and we
    observe the exit code) rather than the pytest process.

    ``poll_iters`` overrides the 25-iteration poll loop via
    PX_YIELD_ALIVE_POLL_ITERS so tests don't wait 5s.  The logic under
    test (timeout → return 1) is identical regardless of iteration count.

    ``heartbeat_file`` always gets pinned via PX_YIELD_ALIVE_HEARTBEAT_FILE,
    defaulting to a path that does not exist. Without this, yield_alive's
    phase-2 fallback resolves to /run/spark/alive_heartbeat.json — the REAL
    px-alive's heartbeat when tests run on the robot itself (this repo is
    checked out on SPARK, per CLAUDE.md), which would make these tests
    nondeterministic and dependent on live robot state. Every test must
    isolate this explicitly, the same way LOG_DIR is isolated below.
    """
    env = os.environ.copy()
    env["LOG_DIR"] = str(log_dir)
    if poll_iters is not None:
        env["PX_YIELD_ALIVE_POLL_ITERS"] = poll_iters
    env["PX_YIELD_ALIVE_HEARTBEAT_FILE"] = str(
        heartbeat_file if heartbeat_file is not None else log_dir / "no-such-heartbeat.json"
    )
    if max_wait_s is not None:
        env["PX_YIELD_ALIVE_MAX_WAIT_S"] = max_wait_s
    if stale_after_s is not None:
        env["PX_YIELD_ALIVE_HEARTBEAT_STALE_S"] = stale_after_s

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
        timeout=subprocess_timeout,
    )


def _spawn_ignoring_usr1(code: str) -> subprocess.Popen:
    """Run ``code`` under python3 with SIGUSR1 pre-ignored across the exec.

    Mirrors bin/px-alive's own ``trap '' USR1`` before its exec: python3 -c
    takes ~100ms to start on this hardware (measured), which is enough for
    yield_alive's SIGUSR1 — sent essentially the instant the pid file
    exists — to arrive before a bare ``signal.signal(SIGUSR1, SIG_IGN)`` as
    the process's first statement takes effect. Without this, the fake
    process is killed by the default SIGUSR1 disposition mid-startup,
    becomes a zombie (so /proc/{pid} still exists, masking the kill), and
    never runs the rest of its script — which happened to be invisible to
    the pre-existing failure-path tests (they expect non-zero either way)
    but silently broke the heartbeat-evidence tests (the process died
    before ever writing a heartbeat).
    """
    proc = subprocess.Popen(
        ["bash", "-c", 'trap "" USR1; exec python3 -c "$0"', code],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # A subprocess.Popen child that exits without ever being wait()'d/poll()'d
    # becomes a zombie: the kernel keeps its /proc/{pid} entry (status 'Z')
    # until this process reaps it. yield_alive's whole contract is watching
    # /proc/{pid} disappear, so an un-reaped zombie would make it wait past
    # the process's real exit — a test artifact, not something px-alive
    # (whose real parent is systemd/PID 1, which reaps promptly) ever does.
    # A background wait() blocks until the child exits, reaping it the
    # instant that happens.
    threading.Thread(target=proc.wait, daemon=True).start()
    return proc


def _make_zombie_proxy(log_dir: Path) -> int:
    """Spawn a sleep process that ignores SIGUSR1 and write its PID.

    This simulates a px-alive that received SIGUSR1 but did not exit —
    the exact race condition described in #205.
    """
    proc = _spawn_ignoring_usr1(
        "import signal, time; signal.signal(signal.SIGUSR1, signal.SIG_IGN); time.sleep(60)"
    )
    pid_file = log_dir / "px-alive.pid"
    pid_file.write_text(str(proc.pid))
    return proc.pid


def _make_heartbeating_zombie_proxy(
    log_dir: Path,
    heartbeat_file: Path,
    beat_every_s: float,
    exit_after_s: float | None = None,
) -> int:
    """Like _make_zombie_proxy, but also refreshes a heartbeat JSON file —
    simulating px-alive's own _with_heartbeat() (#261) during a slow-but-
    legitimate camera/GPIO teardown: ignores SIGUSR1 (so it never exits on
    its own from the signal), and periodically rewrites ``heartbeat_file``
    with a fresh ``ts``.

    If ``exit_after_s`` is given, the process exits after that many seconds
    total — simulating teardown eventually completing. Left as None, it
    beats forever until killed — simulating a teardown that keeps proving
    progress but never actually finishes, which is what pins the finite
    overall ceiling regardless of heartbeat health.

    Writes the first beat synchronously, in this process, before the child
    even spawns. In production alive_heartbeat.json has existed continuously
    since px-alive started — yield_alive's phase 2 never finds it missing.
    Leaving it to the child's first loop iteration would make these tests
    depend on python3's interpreter-startup latency (measured on this
    hardware: ~90ms idle, several seconds under the load a concurrent test
    run itself creates) racing yield_alive's first phase-2 check — a gap
    that doesn't exist for the real daemon and shouldn't gate this test.
    """
    heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(heartbeat_file.parent), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps({"ts": time.time(), "mode": "acquiring_picarx"}))
    os.replace(tmp, heartbeat_file)
    code = (
        "import json, os, signal, tempfile, time\n"
        "signal.signal(signal.SIGUSR1, signal.SIG_IGN)\n"
        f"heartbeat_file = {str(heartbeat_file)!r}\n"
        f"beat_every_s = {beat_every_s!r}\n"
        f"exit_after_s = {exit_after_s!r}\n"
        "start = time.time()\n"
        "while True:\n"
        "    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(heartbeat_file), suffix='.tmp')\n"
        "    with os.fdopen(fd, 'w') as f:\n"
        "        f.write(json.dumps({'ts': time.time(), 'mode': 'acquiring_picarx'}))\n"
        "    os.replace(tmp, heartbeat_file)\n"
        "    if exit_after_s is not None and time.time() - start >= exit_after_s:\n"
        "        break\n"
        "    time.sleep(beat_every_s)\n"
    )
    proc = _spawn_ignoring_usr1(code)
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
    """When px-alive's PID is already gone (exited and reaped by systemd),
    yield_alive returns 0 — the normal path after px-alive has stopped.

    In production, systemd reaps px-alive on exit so /proc/{pid} disappears
    immediately.  We simulate this by using a PID that has already exited
    and been waited on, rather than spawning a long-running process that
    may linger as a zombie on CI runners.
    """
    log_dir = long_poll_env
    # Fork a process that exits immediately, then wait for it.
    # Its PID is now dead and reaped — /proc/{pid} will not exist,
    # exactly like a systemd-reaped px-alive.
    proc = subprocess.Popen(
        ["true"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc.wait()
    pid_file = log_dir / "px-alive.pid"
    pid_file.write_text(str(proc.pid))
    try:
        result = _source_and_call(log_dir, poll_iters="25")
        assert result.returncode == 0, (
            f"yield_alive should return 0 when px-alive's PID is gone, "
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


# SPARK is a live, resource-constrained robot (per CLAUDE.md, this repo is
# checked out on SPARK itself) — the heartbeat-evidence tests below spawn
# real processes and measure real wall-clock behaviour, so they are exposed
# to whatever the host's scheduler is doing right now. Measured live while
# writing this suite: a trivial python3 subprocess spawn ranged from ~90ms
# to ~6.8s under load (load average 10+, swap exhausted from concurrent test
# runs). These tests therefore use margins an order of magnitude past their
# nominal timing rather than tight bounds, and assert outcomes (which
# message fired, which side of a ceiling it landed on) rather than precise
# durations — a test that only passes on an idle machine would be useless
# here.
@_LINUX_ONLY
def test_yield_alive_waits_through_slow_but_legitimate_teardown(long_poll_env, tmp_path):
    """A caller must not give up while px-alive is provably still tearing
    down (fresh heartbeat), even past the fast poll window (#261 made
    camera/GPIO teardown legitimately outlast it — observed live 8.75s and
    14s). This is #205's remaining gap: the honest-failure fix from PR #250
    still aborted a yield that was seconds from succeeding.
    """
    log_dir = long_poll_env
    heartbeat_file = tmp_path / "heartbeat" / "alive_heartbeat.json"
    pid = _make_heartbeating_zombie_proxy(
        log_dir, heartbeat_file, beat_every_s=0.3, exit_after_s=5.0
    )
    try:
        start = time.monotonic()
        result = _source_and_call(
            log_dir,
            poll_iters="3",  # ~0.6s fast window, well short of the 5s exit
            heartbeat_file=heartbeat_file,
            max_wait_s="120",     # generous ceiling — the exit is what should end this
            stale_after_s="30",   # generous — tolerate scheduler jitter, not a precise probe
            subprocess_timeout=150,
        )
        elapsed = time.monotonic() - start
        assert result.returncode == 0, (
            f"yield_alive should wait out a fresh heartbeat and succeed once "
            f"px-alive actually exits, got {result.returncode}. "
            f"stderr: {result.stderr[:500]}"
        )
        assert elapsed > 0.6, (
            "yield_alive returned success too fast to have gone through "
            "phase 2 at all — this test would pass even if phase 2 were "
            f"deleted. elapsed={elapsed:.2f}s"
        )
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@_LINUX_ONLY
def test_yield_alive_fails_fast_when_heartbeat_goes_stale(long_poll_env, tmp_path):
    """A heartbeat that stops advancing mid-teardown means px-alive is
    wedged, not slow — yield_alive must fail well before the overall
    ceiling, not wait out the full budget on a corpse.
    """
    log_dir = long_poll_env
    heartbeat_file = tmp_path / "heartbeat" / "alive_heartbeat.json"
    # Writes exactly one beat, then sleeps ~forever without exiting —
    # simulating px-alive still holding the process (ignoring SIGUSR1) but
    # wedged mid-teardown rather than making progress.
    pid = _make_heartbeating_zombie_proxy(
        log_dir, heartbeat_file, beat_every_s=999, exit_after_s=None
    )
    try:
        start = time.monotonic()
        result = _source_and_call(
            log_dir,
            poll_iters="3",       # ~0.6s fast window
            heartbeat_file=heartbeat_file,
            max_wait_s="60",      # would wait up to 60s if staleness weren't caught
            stale_after_s="3",    # heartbeat older than 3s is wedged
            subprocess_timeout=90,
        )
        elapsed = time.monotonic() - start
        assert result.returncode != 0, (
            "yield_alive must not succeed against a stalled heartbeat"
        )
        assert "stale" in result.stderr and "wedged" in result.stderr, (
            f"expected a stale/wedged diagnosis, got: {result.stderr[:500]}"
        )
        assert elapsed < 30.0, (
            f"stale-heartbeat detection should fail well before the 60s "
            f"ceiling, took {elapsed:.2f}s — looks like it waited out the "
            f"full budget instead of noticing the stall"
        )
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@_LINUX_ONLY
def test_yield_alive_fails_at_finite_bound_despite_fresh_heartbeat(long_poll_env, tmp_path):
    """Even a heartbeat that never stops must not buy an unbounded wait —
    PX_YIELD_ALIVE_MAX_WAIT_S is a hard ceiling regardless of how healthy
    px-alive looks, so a caller can never hang indefinitely.
    """
    log_dir = long_poll_env
    heartbeat_file = tmp_path / "heartbeat" / "alive_heartbeat.json"
    pid = _make_heartbeating_zombie_proxy(
        log_dir, heartbeat_file, beat_every_s=0.3, exit_after_s=None
    )
    try:
        start = time.monotonic()
        result = _source_and_call(
            log_dir,
            poll_iters="3",      # ~0.6s fast window
            heartbeat_file=heartbeat_file,
            max_wait_s="15",     # finite ceiling, sized to survive scheduler jitter
            stale_after_s="120", # deliberately far above max_wait_s: the ceiling must
                                 # win before staleness could ever be reached, so this
                                 # test can't pass for the wrong reason under jitter.
            subprocess_timeout=60,
        )
        elapsed = time.monotonic() - start
        assert result.returncode != 0, (
            "a heartbeat that never stops must not grant an unbounded wait"
        )
        assert "still shutting down" in result.stderr, (
            f"expected a ceiling-timeout diagnosis, got: {result.stderr[:500]}"
        )
        assert elapsed < 45.0, (
            f"expected failure at or shortly after the 15s ceiling, took "
            f"{elapsed:.2f}s"
        )
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_yield_alive_signals_via_sudo():
    """The USR1 signal must go through sudo, or every non-root caller silently no-ops.

    px-alive runs as root; a bare `kill -USR1 $pid` from a non-root caller
    (most bin/tool-*, bin/px-* scripts run as `pi`) fails EPERM and was
    swallowed by `|| true`, so px-alive never actually saw the yield and the
    caller degraded into the 5s poll timeout below (a #250-class
    regression). Confirmed live: tool-perform.log and tool-emote.log show
    dozens of "did not exit within 5s; aborting to protect GPIO exclusivity"
    failures. `pi` already holds passwordless sudo on this host, so routing
    through it fixes both root and non-root callers uniformly.
    """
    body = (PROJECT_ROOT / "bin" / "px-env").read_text()
    assert "sudo -n kill -USR1 \"$pid\"" in body, (
        "yield_alive must signal px-alive via sudo — a bare kill silently "
        "fails for every non-root caller"
    )