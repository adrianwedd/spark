"""Tests for tool-introspect thought statistics and config snapshot."""
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def introspect_env(tmp_path):
    """Set up isolated state for tool-introspect."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    thoughts = []
    for i in range(10):
        thoughts.append(json.dumps({
            "ts": f"2026-03-20T{10+i}:00:00Z",
            "thought": f"Test thought {i} about science and curiosity",
            "mood": "curious" if i % 3 == 0 else "contemplative",
            "action": "comment" if i % 2 == 0 else "wait",
            "salience": 0.5 + (i % 3) * 0.1,
        }))
    (state_dir / "thoughts-spark.jsonl").write_text("\n".join(thoughts) + "\n")

    env = os.environ.copy()
    env["PX_STATE_DIR"] = str(state_dir)
    env["LOG_DIR"] = str(log_dir)
    env["PX_DRY"] = "1"
    env["PX_BYPASS_SUDO"] = "1"
    return env, state_dir


def test_introspect_writes_json(introspect_env):
    env, state_dir = introspect_env
    result = subprocess.run(
        ["bin/tool-introspect"], cwd=str(ROOT), env=env,
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0
    output = json.loads(result.stdout.strip().splitlines()[-1])
    assert output["status"] == "ok"
    intro = json.loads((state_dir / "introspection.json").read_text())
    assert "mood_distribution" in intro
    assert "config" in intro
    assert "architecture" in intro


def test_introspect_mood_distribution(introspect_env):
    env, state_dir = introspect_env
    subprocess.run(["bin/tool-introspect"], cwd=str(ROOT), env=env,
        capture_output=True, text=True, timeout=30)
    intro = json.loads((state_dir / "introspection.json").read_text())
    total = sum(intro["mood_distribution"].values())
    assert abs(total - 100.0) < 1.0


def test_introspect_config_snapshot(introspect_env):
    env, state_dir = introspect_env
    subprocess.run(["bin/tool-introspect"], cwd=str(ROOT), env=env,
        capture_output=True, text=True, timeout=30)
    intro = json.loads((state_dir / "introspection.json").read_text())
    config = intro["config"]
    assert "SIMILARITY_THRESHOLD" in config
    assert "EXPRESSION_COOLDOWN_S" in config
    assert "spark_angles_count" in config
    assert "topic_seeds_count" in config


def test_introspect_empty_thoughts(introspect_env):
    env, state_dir = introspect_env
    (state_dir / "thoughts-spark.jsonl").write_text("")
    result = subprocess.run(["bin/tool-introspect"], cwd=str(ROOT), env=env,
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0
    output = json.loads(result.stdout.strip().splitlines()[-1])
    assert output["status"] == "ok"
