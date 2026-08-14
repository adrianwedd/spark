"""Tests for px-alive idle-alive daemon (dry-run only — no GPIO)."""
import json
import os
import subprocess
from pathlib import Path

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


def test_non_exploration_process_cannot_strand_alive_via_exploring_state(isolated_project):
    """Regression for #176: a live unrelated PID in exploring.json is state,
    not a GPIO lease, and must not suppress px-alive startup."""
    env = isolated_project["env"].copy()
    state_dir = isolated_project["state_dir"]
    log_dir = isolated_project["log_dir"]
    env["PX_LOG_FILE"] = str(log_dir / "px-alive.log")
    env["PX_ALIVE_PID"] = str(log_dir / "px-alive.pid")
    (state_dir / "exploring.json").write_text(json.dumps({
        "active": True, "pid": os.getpid(), "started": "2026-08-14T00:00:00Z",
    }))

    result = run_alive([], env)

    assert result.returncode == 0
    assert "dry gaze" in (log_dir / "px-alive.log").read_text()


def test_live_gpio_lease_suppresses_alive_startup(isolated_project):
    env = isolated_project["env"].copy()
    state_dir = isolated_project["state_dir"]
    log_dir = isolated_project["log_dir"]
    env["PX_LOG_FILE"] = str(log_dir / "px-alive.log")
    env["PX_ALIVE_PID"] = str(log_dir / "px-alive.pid")
    lease = GpioLeaseStore(state_dir).acquire("voice", ttl_s=60)
    assert lease is not None

    result = run_alive([], env)

    assert result.returncode == 0
    content = (log_dir / "px-alive.log").read_text()
    assert "GPIO lease active" in content
    assert "dry gaze" not in content
