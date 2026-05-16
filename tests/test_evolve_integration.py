"""Integration test: introspect → evolve pipeline (dry-run)."""
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def integration_env(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    # Write sample thoughts
    thoughts = []
    for i in range(20):
        thoughts.append(json.dumps({
            "ts": f"2026-03-20T{10 + i % 12}:00:00Z",
            "thought": f"I notice the quantum fluctuations in thought {i} about science",
            "mood": ["curious", "contemplative", "content", "playful"][i % 4],
            "action": ["comment", "wait", "greet", "look_at"][i % 4],
            "salience": 0.4 + (i % 5) * 0.1,
        }))
    (state_dir / "thoughts-spark.jsonl").write_text("\n".join(thoughts) + "\n")

    env = os.environ.copy()
    env["PX_STATE_DIR"] = str(state_dir)
    env["LOG_DIR"] = str(log_dir)
    env["PX_DRY"] = "1"
    env["PX_BYPASS_SUDO"] = "1"
    return env, state_dir


def test_introspect_then_evolve_dry(integration_env):
    """Full pipeline: introspect writes json, evolve reads it and queues."""
    env, state_dir = integration_env

    # Step 1: Run tool-introspect
    result = subprocess.run(
        ["bin/tool-introspect"], cwd=str(ROOT), env=env,
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0
    output = json.loads(result.stdout.strip().splitlines()[-1])
    assert output["status"] == "ok"

    # Step 2: Verify introspection.json exists and has content
    intro_file = state_dir / "introspection.json"
    assert intro_file.exists()
    intro = json.loads(intro_file.read_text())
    assert "mood_distribution" in intro
    assert "config" in intro
    assert intro.get("ts", "")

    # Step 3: Run tool-evolve with valid intent
    env["PX_EVOLVE_INTENT"] = "Add more sound-related angles to explore acoustic phenomena"
    result = subprocess.run(
        ["bin/tool-evolve"], cwd=str(ROOT), env=env,
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0
    output = json.loads(result.stdout.strip().splitlines()[-1])
    assert output["status"] == "queued"
    assert "id" in output

    # Step 4: Verify queue entry
    queue_file = state_dir / "evolve_queue.jsonl"
    assert queue_file.exists()
    entry = json.loads(queue_file.read_text().strip())
    assert entry["status"] == "pending"
    assert "sound" in entry["intent"]
    assert "introspection" in entry
