"""Tests for px-alive idle-alive daemon (dry-run only — no GPIO)."""
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import types
from pathlib import Path
from unittest import mock

import pytest

from pxh.gpio_lease import GpioLeaseStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run_alive(extra_args, env):
    # Run without sudo: dry-run never touches GPIO/picarx, and sudo strips env vars
    # which would prevent LOG_DIR / PX_LOG_FILE from reaching the script.
    result = subprocess.run(
        [str(PROJECT_ROOT / "bin" / "px-alive"), "--dry-run"] + extra_args,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    return result


def test_px_alive_dry_run_exits_zero(isolated_project):
    """--dry-run mode should exit 0 and log expected events."""
    env = isolated_project["env"].copy()
    env["PX_BYPASS_SUDO"] = "1"
    log_dir = isolated_project["log_dir"]
    env["PX_LOG_FILE"] = str(log_dir / "px-alive.log")
    env["PX_ALIVE_PID"] = str(log_dir / "px-alive.pid")

    result = run_alive([], env)
    # dry-run exits 0
    assert result.returncode == 0, f"stderr: {result.stderr[:500]}"


def test_px_alive_dry_run_logs_gaze(isolated_project):
    """Dry-run should log synthetic gaze drift events."""
    env = isolated_project["env"].copy()
    log_dir = isolated_project["log_dir"]
    env["PX_LOG_FILE"] = str(log_dir / "px-alive.log")
    env["PX_ALIVE_PID"] = str(log_dir / "px-alive.pid")

    run_alive([], env)
    log_path = log_dir / "px-alive.log"
    assert log_path.exists(), "log file not created"
    content = log_path.read_text()
    assert "dry gaze" in content, f"no gaze events in log: {content[:300]}"


def test_px_alive_dry_run_scan_sweep(isolated_project):
    """Dry-run should log a simulated idle scan sweep."""
    env = isolated_project["env"].copy()
    log_dir = isolated_project["log_dir"]
    env["PX_LOG_FILE"] = str(log_dir / "px-alive.log")
    env["PX_ALIVE_PID"] = str(log_dir / "px-alive.pid")

    run_alive([], env)
    content = (log_dir / "px-alive.log").read_text()
    assert "simulating idle scan" in content


def test_px_alive_dry_run_no_pid_leftover(isolated_project):
    """PID file should be cleaned up after dry-run exits."""
    env = isolated_project["env"].copy()
    log_dir = isolated_project["log_dir"]
    pid_file = log_dir / "px-alive.pid"
    env["PX_LOG_FILE"] = str(log_dir / "px-alive.log")
    env["PX_ALIVE_PID"] = str(pid_file)

    run_alive([], env)
    assert not pid_file.exists(), "PID file not cleaned up after dry-run"


def test_px_alive_no_prox_flag(isolated_project):
    """--no-prox should disable proximity threshold (exits 0 in dry-run)."""
    env = isolated_project["env"].copy()
    log_dir = isolated_project["log_dir"]
    env["PX_LOG_FILE"] = str(log_dir / "px-alive.log")
    env["PX_ALIVE_PID"] = str(log_dir / "px-alive.pid")

    result = run_alive(["--no-prox"], env)
    assert result.returncode == 0


def test_systemd_service_enables_functional_watchdog():
    service = (PROJECT_ROOT / "systemd" / "px-alive.service").read_text()

    assert "WatchdogSec=15" in service
    assert "NotifyAccess=main" in service


def test_systemd_service_defers_watchdog_until_ready():
    """Startup and steady state need different deadlines.

    With Type=simple the 15s watchdog armed at exec, so a Picarx acquisition
    that contended for I2C with the tool that had just taken GPIO was killed
    mid-init (observed live: started 11:13:38, SIGABRTed at 15s, handle never
    acquired, against a ~6s normal start). Type=notify moves that window under
    TimeoutStartSec and leaves WatchdogSec strict for the loop that follows.
    """
    service = (PROJECT_ROOT / "systemd" / "px-alive.service").read_text()

    assert "Type=notify" in service
    assert "Type=simple" not in service
    assert "TimeoutStartSec=" in service
    assert "WatchdogSec=15" in service


def test_alive_signals_ready_only_after_acquiring_hardware():
    """READY=1 must not be sent from a path that hasn't reached working state.

    Sending it early would re-arm the 15s watchdog over the acquisition this
    change exists to protect.
    """
    body = (PROJECT_ROOT / "bin" / "px-alive").read_text()
    ready_calls = [
        line.strip() for line in body.splitlines() if "notify_ready(" in line
        and not line.strip().startswith("def ")
    ]

    assert ready_calls, "no readiness notification found"
    assert all(
        any(state in call for state in ("Picarx handle", "charger", "I2C backoff"))
        for call in ready_calls
    ), ready_calls


def test_systemd_service_provides_tmpfs_runtime_dir():
    """/run/spark must exist before the daemon starts, or it falls back to SD.

    RuntimeDirectory= makes systemd create (and clean up) the tmpfs dir. Without
    it the heartbeat silently lands back on the SD card and the 21.5s fsync tail
    that trips WatchdogSec=15 comes straight back.
    """
    service = (PROJECT_ROOT / "systemd" / "px-alive.service").read_text()

    assert "RuntimeDirectory=spark" in service


def test_false_exploration_owner_does_not_suppress_alive(isolated_project):
    """#176: exploring state from a live unrelated PID is not GPIO authority."""
    env = isolated_project["env"].copy()
    state_dir = isolated_project["state_dir"]
    log_dir = isolated_project["log_dir"]
    env["PX_LOG_FILE"] = str(log_dir / "px-alive.log")
    env["PX_ALIVE_PID"] = str(log_dir / "px-alive.pid")
    (state_dir / "exploring.json").write_text(json.dumps({
        "active": True,
        "pid": os.getpid(),
        "started": "2026-08-14T00:00:00Z",
    }))

    result = run_alive([], env)

    assert result.returncode == 0
    assert "dry gaze" in (log_dir / "px-alive.log").read_text()


# test_live_gpio_owner_suppresses_alive_until_release was retired here.
#
# It asserted the old contract — that a foreign lease makes px-alive exit
# cleanly and stay gone until systemd respawns it ("dry gaze" not in the log).
# That exit-under-Restart=always is precisely the 15s respawn loop this module
# now prevents, so the assertion encoded the bug. Its real intent (yield, then
# resume once the owner releases authority) is covered end-to-end by
# test_foreign_lease_keeps_daemon_alive_in_lease_wait below, which additionally
# proves the process stays alive while parked.


# --- heartbeat runtime location (#190-adjacent: SD fsync tail kills the watchdog) ---
#
# Measured on the live Pi: a 169-byte fsync+replace into state/ on the SD card
# has a p50 of 12 ms but a tail reaching 21.5 s — past WatchdogSec=15, so systemd
# SIGABRTs a perfectly healthy daemon. The same write to tmpfs is 0.63 ms with no
# tail. The heartbeat is disposable liveness data, so it belongs on tmpfs.


def load_alive_module(env_overrides):
    """Exec px-alive's embedded python body as a module namespace.

    bin/px-alive is bash wrapping a `<<'PY' ... PY` heredoc, so there is nothing
    importable. Extracting the body lets the ordering invariant be tested
    directly instead of inferred from subprocess side effects.
    """
    source = (PROJECT_ROOT / "bin" / "px-alive").read_text()
    body = source.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    ns = {"__name__": "px_alive_under_test"}
    with mock.patch.dict(os.environ, env_overrides, clear=False):
        exec(compile(body, "px-alive", "exec"), ns)
    return ns


@contextlib.contextmanager
def _notify_socket():
    """A bound systemd-style notify socket.

    Bound under /tmp rather than pytest's tmp_path: AF_UNIX paths cap at ~104
    bytes and pytest's nested tmp dirs blow straight past that on macOS.
    """
    sock_dir = tempfile.mkdtemp(prefix="pxa", dir="/tmp")
    sock_path = os.path.join(sock_dir, "n.sock")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(sock_path)
    sock.settimeout(0.5)
    try:
        yield sock, sock_path
    finally:
        sock.close()
        shutil.rmtree(sock_dir, ignore_errors=True)


def _drain(sock):
    received = []
    while True:
        try:
            received.append(sock.recv(256))
        except socket.timeout:
            return received


def _alive_env(isolated_project, heartbeat_dir=None):
    env = isolated_project["env"].copy()
    log_dir = isolated_project["log_dir"]
    env["PX_LOG_FILE"] = str(log_dir / "px-alive.log")
    env["PX_ALIVE_PID"] = str(log_dir / "px-alive.pid")
    if heartbeat_dir is not None:
        env["PX_ALIVE_HEARTBEAT_DIR"] = str(heartbeat_dir)
    return env


def test_heartbeat_written_to_configured_runtime_dir(isolated_project, tmp_path):
    """PX_ALIVE_HEARTBEAT_DIR relocates the heartbeat off the state dir entirely."""
    runtime_dir = tmp_path / "runtime"
    env = _alive_env(isolated_project, heartbeat_dir=runtime_dir)

    result = run_alive([], env)

    assert result.returncode == 0, f"stderr: {result.stderr[:500]}"
    beat = runtime_dir / "alive_heartbeat.json"
    assert beat.exists(), "heartbeat not written to configured runtime dir"
    assert json.loads(beat.read_text())["mode"]
    assert not (isolated_project["state_dir"] / "alive_heartbeat.json").exists(), \
        "heartbeat must not also land on the state dir (SD card)"


def test_heartbeat_falls_back_to_state_dir_when_runtime_dir_unusable(
    isolated_project, tmp_path
):
    """Non-systemd hosts and tests must still get a heartbeat, not a crash."""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    env = _alive_env(isolated_project, heartbeat_dir=blocked / "spark")

    result = run_alive([], env)

    assert result.returncode == 0, f"stderr: {result.stderr[:500]}"
    assert (isolated_project["state_dir"] / "alive_heartbeat.json").exists(), \
        "unusable runtime dir should fall back to the state dir"


def test_watchdog_not_notified_when_heartbeat_write_fails(isolated_project, tmp_path):
    """Ordering invariant: systemd must never see healthy on a stale record.

    Makes persistence fail and asserts no WATCHDOG=1 datagram is sent. This is
    the property the write-then-notify ordering exists to guarantee.
    """
    readonly = tmp_path / "readonly"
    readonly.mkdir()

    try:
        with _notify_socket() as (notify_sock, sock_path):
            alive = load_alive_module({
                "PX_ALIVE_HEARTBEAT_DIR": str(readonly),
                "NOTIFY_SOCKET": sock_path,
                "PX_STATE_DIR": str(isolated_project["state_dir"]),
                "PX_LOG_FILE": str(isolated_project["log_dir"] / "px-alive.log"),
            })
            # Break persistence *after* the dir was resolved, so this exercises a
            # runtime write failure rather than the startup fallback.
            readonly.chmod(0o500)

            assert alive["write_alive_heartbeat"]("running") is False

            received = _drain(notify_sock)
            assert not any(b"WATCHDOG=1" in msg for msg in received), \
                f"watchdog notified despite failed heartbeat persistence: {received}"
    finally:
        readonly.chmod(0o700)


def test_watchdog_notified_after_successful_heartbeat(isolated_project, tmp_path):
    """The happy path still pings systemd once the record is durable."""
    runtime_dir = tmp_path / "runtime"
    with _notify_socket() as (notify_sock, sock_path):
        env = _alive_env(isolated_project, heartbeat_dir=runtime_dir)
        env["NOTIFY_SOCKET"] = sock_path

        result = run_alive([], env)
        assert result.returncode == 0, f"stderr: {result.stderr[:500]}"

        received = _drain(notify_sock)
        assert any(b"WATCHDOG=1" in msg for msg in received), \
            f"no watchdog notification after successful heartbeat: {received}"
        assert (runtime_dir / "alive_heartbeat.json").exists()


def test_long_servo_motion_keeps_heartbeating(isolated_project, tmp_path):
    """A single ease() must not outlast WatchdogSec without a heartbeat.

    Observed live after the tmpfs move: the idle scan sweep still tripped the
    watchdog. It eases through ~17 angles at 1.5s each plus a 2s recentre — over
    30s of servo motion with no heartbeat between the log lines that bracket it:

        10:45:32  idle scan: sweeping pan
        10:45:48  Watchdog timeout (limit 15s)!

    This is legitimate worst-case loop work exceeding the deadline, so the fix
    belongs in heartbeat placement, not in WatchdogSec.
    """
    alive = load_alive_module({
        "PX_ALIVE_HEARTBEAT_DIR": str(tmp_path / "runtime"),
        "PX_STATE_DIR": str(isolated_project["state_dir"]),
        "PX_LOG_FILE": str(isolated_project["log_dir"] / "px-alive.log"),
    })

    beats = []
    alive["write_alive_heartbeat"] = lambda mode, now=None: beats.append(mode)
    # Keep the test fast: real sleeps would make this a 20s test.
    alive["time"] = types.SimpleNamespace(
        sleep=lambda _s: None, time=time.time, monotonic=time.monotonic,
    )

    class FakePx:
        def set_cam_pan_angle(self, _a): pass
        def set_cam_tilt_angle(self, _a): pass

    alive["ease"](FakePx(), 0, 0, 60, 0, 20.0, mode="scanning")

    assert beats, "long ease() produced no heartbeat at all"
    assert set(beats) == {"scanning"}, f"unexpected heartbeat modes: {set(beats)}"
    # 20s of motion must yield beats comfortably inside the 15s deadline.
    assert len(beats) >= 4, f"only {len(beats)} heartbeats across a 20s ease"


# --- lease_wait: yielding GPIO is not a reason to die ---
#
# Observed on the Pi: 47 of 111 px-alive restarts in 6h were *clean* exits
# because px-wake-listen held the GPIO lease. Restart=always turned a correct
# yield into a 15s respawn loop, re-initialising Picarx each time.


def test_foreign_lease_keeps_daemon_alive_in_lease_wait(isolated_project, tmp_path):
    """An active foreign lease parks the loop; it must not return from main()."""
    runtime_dir = tmp_path / "runtime"
    env = _alive_env(isolated_project, heartbeat_dir=runtime_dir)
    state_dir = isolated_project["state_dir"]

    lease = GpioLeaseStore(state_dir).acquire("voice", ttl_s=60)
    assert lease is not None

    proc = subprocess.Popen(
        [str(PROJECT_ROOT / "bin" / "px-alive"), "--dry-run"],
        cwd=PROJECT_ROOT, text=True, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        beat = runtime_dir / "alive_heartbeat.json"
        deadline = time.time() + 15
        mode = None
        while time.time() < deadline:
            if proc.poll() is not None:
                pytest.fail(
                    f"px-alive exited (rc={proc.returncode}) instead of waiting "
                    f"for the lease to clear"
                )
            if beat.exists():
                try:
                    mode = json.loads(beat.read_text())["mode"]
                except (json.JSONDecodeError, KeyError):
                    mode = None
                if mode == "lease_wait":
                    break
            time.sleep(0.2)

        assert mode == "lease_wait", f"expected lease_wait heartbeat, got {mode!r}"
        assert proc.poll() is None, "daemon must stay alive while parked"

        # Releasing the lease lets it resume a normal loop on its own.
        assert GpioLeaseStore(state_dir).release(lease.lease_id) is True
        proc.wait(timeout=30)
        assert proc.returncode == 0
        assert "dry gaze" in (isolated_project["log_dir"] / "px-alive.log").read_text()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_sigusr1_while_parked_is_a_noop(isolated_project, tmp_path):
    """Yielding to a tool must be harmless while parked, not fatal.

    The park exists so a foreign GPIO lease doesn't turn into a respawn loop.
    But SIGUSR1's handler used to be installed inside idle_loop(), which the
    park never reaches — so during the park the signal kept Python's default
    disposition and terminated the process. A tool calling yield_alive against
    a parked daemon killed the very thing the park was protecting, and we hold
    no GPIO while parked, so there is nothing to yield in the first place.
    """
    runtime_dir = tmp_path / "runtime"
    env = _alive_env(isolated_project, heartbeat_dir=runtime_dir)
    state_dir = isolated_project["state_dir"]

    lease = GpioLeaseStore(state_dir).acquire("voice", ttl_s=60)
    assert lease is not None

    proc = subprocess.Popen(
        [str(PROJECT_ROOT / "bin" / "px-alive"), "--dry-run"],
        cwd=PROJECT_ROOT, text=True, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        beat = runtime_dir / "alive_heartbeat.json"
        deadline = time.time() + 15
        while time.time() < deadline:
            if beat.exists() and json.loads(beat.read_text()).get("mode") == "lease_wait":
                break
            time.sleep(0.2)
        assert proc.poll() is None, "daemon died before we could park it"

        proc.send_signal(signal.SIGUSR1)
        time.sleep(2)  # several park re-check passes

        assert proc.poll() is None, (
            f"SIGUSR1 killed the parked daemon (rc={proc.returncode}) — a yield "
            f"against a parked px-alive must be a no-op"
        )

        # And it must still be genuinely parked, not limping on in some other mode.
        assert json.loads(beat.read_text())["mode"] == "lease_wait"

        # Once the lease clears it resumes normally: the ignored yield must not
        # leave a latent flag that makes the loop exit the moment it starts.
        assert GpioLeaseStore(state_dir).release(lease.lease_id) is True
        proc.wait(timeout=30)
        assert proc.returncode == 0
        assert "dry gaze" in (isolated_project["log_dir"] / "px-alive.log").read_text()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_lease_wait_does_not_touch_foreign_lease(isolated_project, tmp_path):
    """While parked the loop is passive: it must not refresh or steal the token."""
    runtime_dir = tmp_path / "runtime"
    env = _alive_env(isolated_project, heartbeat_dir=runtime_dir)
    state_dir = isolated_project["state_dir"]

    lease = GpioLeaseStore(state_dir).acquire("voice", ttl_s=60)
    assert lease is not None
    lease_file = state_dir / "gpio_lease.json"
    before = json.loads(lease_file.read_text())

    proc = subprocess.Popen(
        [str(PROJECT_ROOT / "bin" / "px-alive"), "--dry-run"],
        cwd=PROJECT_ROOT, text=True, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        beat = runtime_dir / "alive_heartbeat.json"
        deadline = time.time() + 15
        while time.time() < deadline:
            if beat.exists() and json.loads(beat.read_text()).get("mode") == "lease_wait":
                break
            time.sleep(0.2)
        time.sleep(2)  # let it take several passive re-check passes

        assert proc.poll() is None, "daemon must still be parked for this to mean anything"
        after = json.loads(lease_file.read_text())
        assert after == before, f"lease mutated while parked: {before} -> {after}"
    finally:
        proc.kill()
        proc.wait(timeout=10)
