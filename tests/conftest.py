import sys
import os
import json
import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if SRC.exists():
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def _isolate_health_writes(tmp_path, monkeypatch):
    """Keep health reporting out of the live state/health/ directory.

    Autouse and unconditional. `isolated_project` is opt-in and only isolates
    *subprocesses*, so any in-process test that touches a daemon code path
    (a TestClient request, a mocked reflection) writes health records through
    the real PX_STATE_DIR. On this repo that directory belongs to a running
    robot — a test run would overwrite live health with mock values, and the
    dashboard would report whatever the suite last asserted.

    Redirects only the health directory rather than repointing PX_STATE_DIR
    globally, which many existing tests deliberately rely on.
    """
    try:
        from pxh import health
    except ImportError:
        return
    health_dir = tmp_path / "health"
    monkeypatch.setattr(health, "health_dir", lambda: health_dir)
    health._last_success_write.clear()


@pytest.fixture(autouse=True)
def _isolate_brain_mailbox(tmp_path, monkeypatch):
    """Keep brain requests out of the live state/brain/ mailbox.

    Same hazard as _isolate_health_writes, and worse in one respect: an
    unisolated test that reaches run_claude_session for a brain-routed type
    drops a real request into the running robot's inbox, where the resident
    Claude session will pick it up and answer it. A test would spend budget
    and make SPARK act on a prompt nobody meant to send.

    Redirects only the mailbox root, so tests that deliberately set
    PX_STATE_DIR keep working.
    """
    try:
        from pxh import brain
    except ImportError:
        return
    monkeypatch.setattr(brain, "brain_root", lambda: tmp_path / "brain")


@pytest.fixture(autouse=True)
def _isolate_alive_heartbeat(tmp_path, monkeypatch):
    """Keep heartbeat reads off the live robot's /run/spark.

    Same hazard as _isolate_health_writes, one layer down. #192 moved the
    px-alive heartbeat to tmpfs, and resolve_heartbeat_read_path() prefers
    /run/spark unconditionally — correct in production, but on a host where
    px-alive is actually running it means an isolated test reads the *live*
    robot's heartbeat instead of the one it just wrote to tmp_path. Every
    TestHealth heartbeat assertion then sees a permanently fresh "running"
    beat: eight tests that pass on CI fail on the robot, and worse, they would
    pass there for the wrong reason if the fixture were ever inverted.

    PX_ALIVE_HEARTBEAT_DIR is the documented override, so point it at a
    per-test tmp dir rather than repointing PX_STATE_DIR globally.
    """
    monkeypatch.setenv("PX_ALIVE_HEARTBEAT_DIR", str(tmp_path / "run-spark"))


@pytest.fixture(autouse=True)
def _isolate_session(tmp_path, monkeypatch, request):
    """Keep session reads and writes off the live robot's state/session.json.

    The fourth instance of the hazard the three fixtures above already close,
    and the one that stayed open longest (#210). `isolated_project` sets
    PX_SESSION_PATH too, but it is opt-in and only isolates *subprocesses*, so
    any in-process test reaching load_session()/update_session() resolved to
    the running robot's own session file — reading it, and on update_session()
    rewriting it.

    That is not theoretical. It cost six `test_mind_utils` expression tests,
    permanently red on this machine and green everywhere else: the live session
    carries `spark_quiet_mode: true` (#209), mind.expression() is a #174
    enforcement point, and policy.evaluate() therefore blocked each dispatch
    before the test's own mock could fire. The failure was carried as a known
    baseline for months, which is how a red suite stops being read at all.

    Seeded from state.default_state() rather than left absent, for two reasons.
    Readers that fail *closed* on an unreadable session (policy_context, by
    design) would otherwise see every test as "quiet mode indeterminate" and
    suppress; and seeding explicitly keeps the fixture from depending on
    state/session.template.json happening to be present, which is what
    ensure_session() would fall back to. The two agree today — checked — so
    this changes no behaviour, only what the isolation rests on.

    Set via monkeypatch.setenv at setup, so the 22 sites across 9 test files
    that own PX_SESSION_PATH themselves still win: their setenv/os.environ
    assignment runs after this one. Pinned by
    test_a_test_that_sets_its_own_session_path_still_wins.

    Escape hatch: tests marked `live` exercise the real machine and keep the
    real session — isolating those would have them assert against a fiction.
    """
    if request.node.get_closest_marker("live"):
        return
    try:
        from pxh import state
    except ImportError:
        return
    session_path = tmp_path / "session" / "session.json"
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text(
        json.dumps(state.default_state(), indent=2) + "\n", encoding="utf-8"
    )
    monkeypatch.setenv("PX_SESSION_PATH", str(session_path))


@pytest.fixture(autouse=True)
def _isolate_observability(tmp_path, monkeypatch, request):
    """Keep test logs out of logs/ and test supervisors off the live socket.

    The fifth instance of the hazard, and the one #221 turned up. The 43
    duplicated `start` records in logs/tool-brain-daemon.log were not two
    supervisors — they were pytest, writing production-shaped records into the
    production log. Logs are state: a test that writes production-shaped logs
    can falsify later forensics even if it never touches production state.

    Sets LOG_DIR *and* PX_BRAIN_TMUX_SOCKET, and the pairing is the point. The
    supervisor guard is keyed to the socket
    (brain_daemon.supervisor_lock_path), so a synthetic socket implies a
    synthetic guard by construction: a test cannot acquire a namespace without
    also acquiring the guard that belongs to it. Under the old checkout-
    relative key the reverse held — relocating the mailbox silently disabled
    the guard — which is how the defect stayed invisible.

    Bypassing the production guard is now explicit: a test that wants the real
    socket must be marked `live`.

    Set via monkeypatch.setenv at setup, so a test that owns either variable
    itself still wins — its own setenv runs after this one. Pinned by
    test_a_test_that_sets_its_own_log_dir_still_wins.

    Escape hatch: tests marked `live` exercise the real machine and keep the
    real log dir and socket, same as _isolate_session — isolating those would
    have them assert against a fiction.
    """
    if request.node.get_closest_marker("live"):
        return
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    socket_dir = tmp_path / "tmux"
    socket_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LOG_DIR", str(log_dir))
    monkeypatch.setenv("PX_BRAIN_TMUX_SOCKET", str(socket_dir / "px-mind"))


@pytest.fixture
def isolated_project(tmp_path):
    """Creates an isolated project directory for testing."""
    log_dir = tmp_path / "logs"
    state_dir = tmp_path / "state"
    log_dir.mkdir()
    state_dir.mkdir()

    session_path = state_dir / "session.json"

    env = os.environ.copy()
    env["PROJECT_ROOT"] = str(ROOT)
    env["LOG_DIR"] = str(log_dir)
    env["PX_SESSION_PATH"] = str(session_path)
    env["PX_BYPASS_SUDO"] = "1"
    env["PX_VOICE_DEVICE"] = "null"
    env["PX_STATE_DIR"] = str(state_dir)
    # Pin the night-silence window open. bin/tool-voice now evaluates policy
    # (#174) for itself, so without this every subprocess test of a speaking
    # tool would pass by day and return "suppressed" after 19:00 Hobart —
    # the in-process equivalent of what voice_loop._policy_now() exists to
    # prevent. `hour >= 99` is never true, so this window never opens.
    # Tests that mean to exercise night silence override both values.
    env["PX_NIGHT_SILENCE_START_H"] = "99"
    env["PX_NIGHT_SILENCE_END_H"] = "0"

    return {
        "env": env,
        "log_dir": log_dir,
        "state_dir": state_dir,
        "session_path": session_path,
    }