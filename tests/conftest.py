import sys
import os
import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if SRC.exists():
    sys.path.insert(0, str(SRC))


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