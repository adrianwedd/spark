import sys
import os
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

    return {
        "env": env,
        "log_dir": log_dir,
        "state_dir": state_dir,
        "session_path": session_path,
    }