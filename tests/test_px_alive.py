"""Tests for px-alive idle-alive daemon (dry-run only — no GPIO)."""
import ast
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


def test_sonar_live_is_not_written_to_the_state_dir(isolated_project, tmp_path):
    """sonar_live.json is runtime state and must not fsync the SD card.

    Caught live with a /proc sampler while px-alive was in uninterruptible
    sleep: syscall 82 (fsync) on fd 18 -> state/tmp<rand>.tmp, wchan
    jbd2_log_wait_commit, still blocked 5s later in the next sample. The only
    5s-cadence mkstemp directly into state/ on the loop path is this write
    (health goes to state/health/, the heartbeat is already on tmpfs). That
    stall runs against WatchdogSec=15 and accounted for 66 of 86 watchdog
    kills in a measured 6h window.

    Same argument as the heartbeat: rewritten every PROX_CHECK_S, meaningless
    after a power cut, and every reader is age-gated. os.replace gives readers
    atomicity; fsync only bought durability nobody wanted.
    """
    runtime_dir = tmp_path / "runtime"
    env = _alive_env(isolated_project, heartbeat_dir=runtime_dir)
    alive = load_alive_module({
        "PX_ALIVE_HEARTBEAT_DIR": str(runtime_dir),
        "PX_STATE_DIR": str(isolated_project["state_dir"]),
        "PX_LOG_FILE": env["PX_LOG_FILE"],
    })

    assert alive["SONAR_LIVE_FILE"].parent == runtime_dir, (
        "sonar_live.json still lands on the state dir (SD card): "
        f"{alive['SONAR_LIVE_FILE']}"
    )


def test_sonar_live_readers_resolve_the_path_px_alive_writes(
    isolated_project, tmp_path
):
    """Writer and readers must not drift apart across the tmpfs move.

    px-alive runs as root and writes; pxh.api, pxh.mind and pxh.mcp_server run
    as pi and read. There is no test that would catch them disagreeing except
    this one — the readers all swallow FileNotFoundError and report
    "unavailable", so a split would look exactly like a stopped daemon.
    """
    from pxh.runtime_paths import resolve_runtime_read_path

    runtime_dir = tmp_path / "runtime"
    state_dir = isolated_project["state_dir"]
    alive = load_alive_module({
        "PX_ALIVE_HEARTBEAT_DIR": str(runtime_dir),
        "PX_STATE_DIR": str(state_dir),
        "PX_LOG_FILE": str(isolated_project["log_dir"] / "px-alive.log"),
    })
    written = alive["SONAR_LIVE_FILE"]
    written.parent.mkdir(parents=True, exist_ok=True)
    written.write_text(json.dumps({"ts": 1.0, "distance_cm": 42.0}))

    with mock.patch.dict(
        os.environ, {"PX_ALIVE_HEARTBEAT_DIR": str(runtime_dir)}, clear=False
    ):
        resolved = resolve_runtime_read_path(state_dir, "sonar_live.json")

    assert resolved == written
    assert json.loads(resolved.read_text())["distance_cm"] == 42.0


def test_health_reporting_never_blocks_the_loop(isolated_project, tmp_path):
    """Health reporting must not be able to kill the daemon it reports on.

    health.record_success() already promises "never raises" for this reason,
    but the loop can be killed by a report that *blocks* just as easily as by
    one that throws. It fsyncs into state/health/ on the SD card, and a /proc
    sampler caught px-alive in uninterruptible sleep on
    state/health/tmp<rand>.tmp in 18 samples — 17 of them consecutive on a
    single temp file, parked in jbd2_log_wait_commit — against WatchdogSec=15.

    That stall is invisible in the log because it happens on the night path
    (OBI_DAY_END=20), where no scan or gaze drift runs and the health write is
    the only SD write left on the loop.
    """
    import threading as _threading

    alive = load_alive_module({
        "PX_ALIVE_HEARTBEAT_DIR": str(tmp_path / "runtime"),
        "PX_STATE_DIR": str(isolated_project["state_dir"]),
        "PX_LOG_FILE": str(isolated_project["log_dir"] / "px-alive.log"),
    })

    entered = _threading.Event()
    release = _threading.Event()

    class _BlockingHealth:
        @staticmethod
        def record_success(*_a, **_k):
            entered.set()
            release.wait(10)

        @staticmethod
        def record_failure(*_a, **_k):
            pass

    # Replace in the module namespace, not on the real pxh.health module, so
    # this cannot leak into other tests in the session.
    alive["_health"] = _BlockingHealth

    try:
        start = time.monotonic()
        alive["_report_health_success"](start)
        elapsed = time.monotonic() - start

        assert entered.wait(5), "health success was never dispatched at all"
        assert elapsed < 0.5, (
            f"loop blocked {elapsed:.2f}s on a health write; it must be "
            "dispatched off the watchdog-fed thread"
        )
    finally:
        release.set()


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
    """The happy path still pings systemd once the record is durable.

    This run never reaches READY=1, so the correct datagram is
    EXTEND_TIMEOUT_USEC alone. Asserting the *absence* of WATCHDOG=1 here is
    the point: systemd arms WatchdogSec the moment it sees that keyword,
    regardless of unit state, so a pre-READY beat carrying it starts a 15s
    clock during an acquisition that cannot feed it. The monkeypatched
    equivalent lives in test_alive_frigate.py; this one proves it on the wire,
    through the real script and a real notify socket.

    Post-READY WATCHDOG=1 is covered by
    test_alive_heartbeat_records_loop_mode_atomically.
    """
    runtime_dir = tmp_path / "runtime"
    with _notify_socket() as (notify_sock, sock_path):
        env = _alive_env(isolated_project, heartbeat_dir=runtime_dir)
        env["NOTIFY_SOCKET"] = sock_path

        result = run_alive([], env)
        assert result.returncode == 0, f"stderr: {result.stderr[:500]}"

        received = _drain(notify_sock)
        assert any(b"EXTEND_TIMEOUT_USEC=" in msg for msg in received), \
            f"no start-timeout extension after successful heartbeat: {received}"
        assert not any(b"WATCHDOG=1" in msg for msg in received), \
            f"pre-READY beat armed the watchdog on the wire: {received}"
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


def test_acquire_heartbeat_keeps_beating_during_a_slow_picarx_construction(isolated_project, tmp_path):
    """Picarx() acquisition must not go silent, even right after a park.

    Observed live 2026-08-21: a 7m53s "voice" GPIO lease cleared, idle_loop
    resumed, and the very next acquisition went silent long enough for
    systemd to kill the start (TimeoutStartSec) — twice in a row. Unlike the
    pre-loop lease park (which beats every LEASE_RECHECK_S), nothing beat
    again once idle_loop resumed and called into make_px(). This is
    legitimate acquisition time, not a wedge, so the fix is a heartbeat
    around the blocking call — not a longer timeout.
    """
    alive = load_alive_module({
        "PX_ALIVE_HEARTBEAT_DIR": str(tmp_path / "runtime"),
        "PX_STATE_DIR": str(isolated_project["state_dir"]),
        "PX_LOG_FILE": str(isolated_project["log_dir"] / "px-alive.log"),
    })

    beats = []
    alive["write_alive_heartbeat"] = lambda mode, now=None: beats.append(mode)
    alive["HEARTBEAT_EVERY_S"] = 0.05  # real-time but fast, to keep the test quick

    def _slow_acquire():
        time.sleep(0.35)
        return "picarx-handle"

    result = alive["_with_heartbeat"](_slow_acquire)

    assert result == "picarx-handle"
    assert beats, "a 0.35s blocking acquisition produced no heartbeat at all"
    assert set(beats) == {"acquiring_picarx"}
    assert len(beats) >= 3, f"only {len(beats)} heartbeats across a 0.35s acquisition"


def test_acquire_heartbeat_stops_beating_once_acquisition_returns(isolated_project, tmp_path):
    """The beat thread must not linger or fire after the call completes."""
    alive = load_alive_module({
        "PX_ALIVE_HEARTBEAT_DIR": str(tmp_path / "runtime"),
        "PX_STATE_DIR": str(isolated_project["state_dir"]),
        "PX_LOG_FILE": str(isolated_project["log_dir"] / "px-alive.log"),
    })

    beats = []
    alive["write_alive_heartbeat"] = lambda mode, now=None: beats.append(mode)
    alive["HEARTBEAT_EVERY_S"] = 0.05

    alive["_with_heartbeat"](lambda: "fast")
    count_at_return = len(beats)
    time.sleep(0.3)  # several beat intervals — nothing should fire post-return

    assert len(beats) == count_at_return, (
        "heartbeat thread kept beating after the acquisition already returned"
    )


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


# --- watchdog margin telemetry (#194) ---------------------------------------
#
# Sampled on the live Pi over 34 min of post-#192 tmpfs-era code: 41 in-process
# stalls (same PID, so not restarts), 5.07s min / 7.06s median / 14.93s max
# against WatchdogSec=15. That is a 70ms margin as the daemon's ordinary state,
# and nothing in the journal, systemctl status, or consecutive_failures could
# see it. These tests pin the observation semantics rather than the numbers.


def _alive_ns(tmp_path, watchdog_usec="15000000"):
    return load_alive_module({
        "PX_ALIVE_HEARTBEAT_DIR": str(tmp_path / "runtime"),
        "PX_STATE_DIR": str(tmp_path / "state"),
        "LOG_DIR": str(tmp_path / "logs"),
        "PX_LOG_FILE": str(tmp_path / "logs" / "px-alive.log"),
        "WATCHDOG_USEC": watchdog_usec,
    })


def test_heartbeat_gap_and_margin_are_computed_from_the_live_deadline(tmp_path):
    """Margin is measured against WATCHDOG_USEC, not a constant copied from the unit."""
    ns = _alive_ns(tmp_path)
    assert ns["WATCHDOG_LIMIT_MS"] == 15000.0

    ns["write_alive_heartbeat"]("running", now=1000.0)
    ns["write_alive_heartbeat"]("running", now=1012.5)   # a 12.5s stall

    rec = json.loads((tmp_path / "runtime" / "alive_heartbeat.json").read_text())
    assert rec["heartbeat_gap_last_ms"] == 12500.0
    assert rec["heartbeat_gap_max_ms"] == 12500.0
    # 15000 - 12500: the margin left before systemd would have killed it
    assert rec["watchdog_margin_min_ms"] == 2500.0


def test_gap_mode_names_the_phase_the_loop_stalled_in(tmp_path):
    """The stall follows the earlier beat, so its mode is the one to report.

    Without distinct modes at the ease() call sites this field is inert: the
    loop-top beat and three of five ease() sites all published "running", so
    all 39 sampled "running" stalls were unattributable.
    """
    ns = _alive_ns(tmp_path)
    ns["write_alive_heartbeat"]("ease_gaze", now=2000.0)
    ns["write_alive_heartbeat"]("running", now=2009.0)

    rec = json.loads((tmp_path / "runtime" / "alive_heartbeat.json").read_text())
    assert rec["heartbeat_gap_max_ms"] == 9000.0
    assert rec["heartbeat_gap_max_mode"] == "ease_gaze", (
        "reported the mode after the stall instead of the one that was live "
        "when the loop went quiet"
    )


def test_ease_call_sites_publish_distinguishable_modes():
    """Precondition for the mode field: four paths must not share one label."""
    body = (PROJECT_ROOT / "bin" / "px-alive").read_text()

    for mode in ("ease_oneshot", "ease_proximity", "ease_gaze", "scanning"):
        assert f'mode="{mode}"' in body, f"{mode} call site lost its label"

    # Every ease() call must pass an explicit mode; a bare call silently
    # inherits the "running" default and becomes indistinguishable from the
    # loop-top beat again. Parsed rather than grepped: a substring search also
    # matches _foreign_lease() and can't see a mode= on a continuation line.
    source = body.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    tree = ast.parse(source)
    bare = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "ease"
        and not any(kw.arg == "mode" for kw in node.keywords)
        and len(node.args) < 7          # mode passed positionally is fine too
    ]
    assert not bare, (
        f"ease() calls with no explicit mode at lines {bare} — these inherit "
        f'the "running" default and become indistinguishable from the loop-top beat'
    )


def test_camera_teardown_is_heartbeat_protected_at_every_call_site():
    """stop_face_detection()/drop_px() must never run un-beaten, and must
    never run at all when face detection was never actually opened.

    Observed live 2026-08-21, minutes after the make_px()-acquisition fix
    shipped: a routine yield_alive from px-cron-say delivered SIGUSR1
    successfully (proving that fix works), px-alive logged "stopping so it
    can run" and began teardown, and 14s later systemd's watchdog killed it
    anyway (SIGABRT) — the same restart-storm shape, relocated from
    acquisition to teardown, because stop_face_detection()'s
    Vilib.camera_close() is itself a blocking call with no heartbeat.

    A second, independent defect in the same family was found live
    2026-08-23: with Frigate holding the camera exclusively,
    start_face_detection() always fails and face_active is always False —
    but three of the four teardown call sites called stop_face_detection()
    (or gated on args.no_face, the *intent* to track faces, not whether it
    ever succeeded) regardless, and Vilib.camera_close() on a camera that
    was never opened takes ~20s to fail ("Camera __init__ sequence did not
    complete"), long enough to blow yield_alive's heartbeat-staleness
    threshold on the calling tool. Every call site must now also gate on
    face_active (mirrored at module scope as _face_active for the two sites
    outside idle_loop's own scope).
    """
    body = (PROJECT_ROOT / "bin" / "px-alive").read_text()

    gated_local = (
        '_with_heartbeat(lambda: (drop_px(), stop_face_detection() if face_active else None))'
    )
    gated_shutdown = (
        '_with_heartbeat(lambda: stop_face_detection() if _face_active else None)'
    )
    gated_main_finally = 'if _face_active:\n            _with_heartbeat(stop_face_detection)'

    assert body.count(gated_local) == 2, (
        "the SIGUSR1 yield path and idle_loop's finally-block teardown must "
        "both stay heartbeat-wrapped and gated on face_active"
    )
    assert gated_shutdown in body, "_shutdown()'s camera-release lost its heartbeat/face_active gate"
    assert gated_main_finally in body, "main()'s outer finally must gate on _face_active, not args.no_face"

    # Every remaining direct Call node to stop_face_detection() must sit on a
    # line that also mentions _with_heartbeat — this fails loudly if a new
    # bare call site is added elsewhere without being wrapped. drop_px() alone
    # is not checked here: release_px() deliberately skips the slow px.close()
    # reset and only closes the ultrasonic handle, so it isn't implicated in
    # the observed blocking (only stop_face_detection()'s Vilib.camera_close()
    # is — see the I2C-backoff drop_px(force_reset_on_reacquire=True) call,
    # which stays bare on purpose).
    source = body.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    tree = ast.parse(source)
    bare_teardown_calls = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "stop_face_detection"
    ]
    source_lines = source.splitlines()
    for lineno in bare_teardown_calls:
        line = source_lines[lineno - 1]
        assert "_with_heartbeat" in line, (
            f"unwrapped teardown call at line {lineno}: {line!r}"
        )


def test_gap_buckets_keep_the_distribution_a_single_extremum_discards(tmp_path):
    """The measured spread is continuous 5-15s; one max cannot represent it."""
    ns = _alive_ns(tmp_path)
    t = 3000.0
    for gap in (5.1, 7.0, 7.5, 13.0):
        ns["write_alive_heartbeat"]("running", now=t)
        t += gap
    ns["write_alive_heartbeat"]("running", now=t)

    rec = json.loads((tmp_path / "runtime" / "alive_heartbeat.json").read_text())
    assert rec["heartbeat_gap_buckets"] == {"4-6s": 1, "6-8s": 2, "12-14s": 1}


def test_window_reset_clears_extrema_but_not_the_live_reading(tmp_path):
    """Extrema are windowed, never lifetime.

    A permanent max=14.93s stops being a signal the moment it stops moving, so
    a reader cannot tell an ongoing problem from a historical one.
    """
    ns = _alive_ns(tmp_path)
    ns["write_alive_heartbeat"]("running", now=4000.0)
    ns["write_alive_heartbeat"]("ease_gaze", now=4014.0)

    rec = json.loads((tmp_path / "runtime" / "alive_heartbeat.json").read_text())
    assert rec["heartbeat_gap_max_ms"] == 14000.0

    ns["reset_watchdog_window"](now=5000.0)
    ns["write_alive_heartbeat"]("running", now=5001.0)

    rec = json.loads((tmp_path / "runtime" / "alive_heartbeat.json").read_text())
    assert rec["heartbeat_gap_max_ms"] == 0.0, "extrema survived the window reset"
    assert rec["heartbeat_gap_max_mode"] == ""
    assert rec["heartbeat_gap_buckets"] == {}
    assert rec["window_started_at"] == 5000.0
    assert rec["watchdog_margin_min_ms"] == 15000.0


def test_loop_duration_is_tracked_separately_from_heartbeat_gap(tmp_path):
    """A 30s scan sweep that beats from inside ease() is healthy; a 30s gap is not.

    Collapsing the two would make a legitimate long iteration indistinguishable
    from a wedged I2C write.
    """
    ns = _alive_ns(tmp_path)
    ns["note_loop_duration"](30.0)
    ns["write_alive_heartbeat"]("scanning", now=6000.0)
    ns["write_alive_heartbeat"]("scanning", now=6002.0)

    rec = json.loads((tmp_path / "runtime" / "alive_heartbeat.json").read_text())
    assert rec["loop_duration_max_ms"] == 30000.0
    assert rec["heartbeat_gap_max_ms"] == 2000.0
    assert rec["watchdog_margin_min_ms"] == 13000.0


def test_telemetry_absent_when_systemd_sets_no_deadline(tmp_path):
    """Off systemd there is no watchdog, so there is no margin to claim."""
    ns = _alive_ns(tmp_path, watchdog_usec="0")
    ns["write_alive_heartbeat"]("running", now=7000.0)
    ns["write_alive_heartbeat"]("running", now=7009.0)

    rec = json.loads((tmp_path / "runtime" / "alive_heartbeat.json").read_text())
    assert "watchdog_margin_min_ms" not in rec
    assert rec["heartbeat_gap_max_ms"] == 9000.0  # gaps still observed
