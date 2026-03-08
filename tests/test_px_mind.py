"""Tests for px-mind cognitive loop daemon (dry-run only — no Ollama/GPIO)."""
import json
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run_mind(extra_args, env):
    return subprocess.run(
        [str(PROJECT_ROOT / "bin" / "px-mind"), "--dry-run"] + extra_args,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def test_px_mind_dry_run_exits_zero(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_MIND_LOG"] = str(isolated_project["log_dir"] / "px-mind.log")
    env["PX_MIND_PID"] = str(isolated_project["log_dir"] / "px-mind.pid")
    result = run_mind([], env)
    assert result.returncode == 0, f"stderr: {result.stderr[:500]}"


def test_px_mind_dry_run_creates_awareness(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_MIND_LOG"] = str(isolated_project["log_dir"] / "px-mind.log")
    env["PX_MIND_PID"] = str(isolated_project["log_dir"] / "px-mind.pid")
    run_mind([], env)
    awareness = Path(PROJECT_ROOT / "state" / "awareness.json")
    assert awareness.exists()
    data = json.loads(awareness.read_text())
    assert "sonar_cm" in data
    assert "time_period" in data
    assert "transitions" in data


def test_px_mind_dry_run_creates_thoughts(isolated_project):
    env = isolated_project["env"].copy()
    env["PX_MIND_LOG"] = str(isolated_project["log_dir"] / "px-mind.log")
    env["PX_MIND_PID"] = str(isolated_project["log_dir"] / "px-mind.pid")
    run_mind([], env)
    thoughts = Path(PROJECT_ROOT / "state" / "thoughts.jsonl")
    assert thoughts.exists()
    lines = thoughts.read_text().strip().splitlines()
    assert len(lines) >= 3  # dry-run does 3 cycles
    thought = json.loads(lines[-1])
    assert "thought" in thought
    assert "mood" in thought
    assert "action" in thought
    assert "salience" in thought


def test_px_mind_dry_run_logs(isolated_project):
    env = isolated_project["env"].copy()
    log_path = isolated_project["log_dir"] / "px-mind.log"
    env["PX_MIND_LOG"] = str(log_path)
    env["PX_MIND_PID"] = str(isolated_project["log_dir"] / "px-mind.pid")
    run_mind([], env)
    content = log_path.read_text()
    assert "starting pid=" in content
    assert "dry-run" in content
    assert "reflection" in content


def test_px_mind_dry_run_no_pid_leftover(isolated_project):
    env = isolated_project["env"].copy()
    pid_file = isolated_project["log_dir"] / "px-mind.pid"
    env["PX_MIND_LOG"] = str(isolated_project["log_dir"] / "px-mind.log")
    env["PX_MIND_PID"] = str(pid_file)
    run_mind([], env)
    assert not pid_file.exists()
