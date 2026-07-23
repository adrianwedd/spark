"""Tests for tool-evolve queue writing and validation."""
import json
import os
import subprocess
import time
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
WRAPPER_TIMEOUT_S = 60

@pytest.fixture
def evolve_env(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    env = os.environ.copy()
    env["PX_STATE_DIR"] = str(state_dir)
    env["LOG_DIR"] = str(log_dir)
    env["PX_DRY"] = "1"
    env["PX_BYPASS_SUDO"] = "1"
    return env, state_dir

def _write_fresh_introspection(state_dir):
    intro = {"ts": time.time(), "config": {}, "mood_distribution": {}}
    (state_dir / "introspection.json").write_text(json.dumps(intro))

def test_evolve_no_introspection_still_queues(evolve_env):
    # No introspection file — previously hard-failed; now defaults to {} (matches
    # conversational path where introspection is optional).
    env, state_dir = evolve_env
    env["PX_EVOLVE_INTENT"] = "Add more science angles to my reflection"
    result = subprocess.run(["bin/tool-evolve"], cwd=str(ROOT), env=env,
        capture_output=True, text=True, timeout=WRAPPER_TIMEOUT_S)
    output = json.loads(result.stdout.strip().splitlines()[-1])
    assert output["status"] == "queued"
    entry = json.loads((state_dir / "evolve_queue.jsonl").read_text().strip())
    assert entry["introspection"] == {}

def test_evolve_rejects_whitespace_only_intent(evolve_env):
    # MIN_INTENT_LEN=20 guard is dropped; enqueue_evolve still rejects empty/whitespace.
    env, state_dir = evolve_env
    env["PX_EVOLVE_INTENT"] = "   "
    result = subprocess.run(["bin/tool-evolve"], cwd=str(ROOT), env=env,
        capture_output=True, text=True, timeout=WRAPPER_TIMEOUT_S)
    output = json.loads(result.stdout.strip().splitlines()[-1])
    assert output["status"] == "error"
    assert "empty" in output["error"]

def test_evolve_queues_valid_request(evolve_env):
    env, state_dir = evolve_env
    _write_fresh_introspection(state_dir)
    env["PX_EVOLVE_INTENT"] = "Add more sound-related angles to my reflection prompts"
    result = subprocess.run(["bin/tool-evolve"], cwd=str(ROOT), env=env,
        capture_output=True, text=True, timeout=WRAPPER_TIMEOUT_S)
    output = json.loads(result.stdout.strip().splitlines()[-1])
    assert output["status"] == "queued"
    assert "id" in output
    queue = (state_dir / "evolve_queue.jsonl").read_text().strip()
    entry = json.loads(queue)
    assert entry["status"] == "pending"
    assert "sound" in entry["intent"]

def test_evolve_rate_limit(evolve_env):
    env, state_dir = evolve_env
    _write_fresh_introspection(state_dir)
    log_entry = {"ts": time.time() - 3600, "status": "pr_created"}
    (state_dir / "evolve_log.jsonl").write_text(json.dumps(log_entry) + "\n")
    env["PX_EVOLVE_INTENT"] = "Add more sound-related angles to my reflection prompts"
    result = subprocess.run(["bin/tool-evolve"], cwd=str(ROOT), env=env,
        capture_output=True, text=True, timeout=WRAPPER_TIMEOUT_S)
    output = json.loads(result.stdout.strip().splitlines()[-1])
    assert output["status"] == "error"
    assert "rate" in output["error"].lower() or "24" in output["error"]

def test_tool_evolve_uses_shared_writer(evolve_env):
    env, state_dir = evolve_env
    (state_dir / "introspection.json").write_text('{"x": 1}')
    env["PX_EVOLVE_INTENT"] = "add a knock-knock joke tool for obi"   # ≥20 chars
    result = subprocess.run(["bin/tool-evolve"], cwd=str(ROOT), env=env,
        capture_output=True, text=True, timeout=WRAPPER_TIMEOUT_S)
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out["status"] == "queued"
    entry = json.loads((state_dir / "evolve_queue.jsonl").read_text().strip())
    assert entry["status"] == "pending"
    assert entry["requester"] == "adrian" and entry["source"] == "cli"
    assert entry["introspection"] == {"x": 1}
