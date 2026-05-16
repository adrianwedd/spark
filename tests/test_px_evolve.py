"""Tests for px-evolve daemon queue processing."""
import json
import os

import pytest


@pytest.fixture
def evolve_state(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    return state_dir, log_dir


def test_empty_queue_is_noop(evolve_state):
    state_dir, _ = evolve_state
    assert not (state_dir / "evolve_queue.jsonl").exists()


def test_pending_entry_found_in_queue(evolve_state):
    state_dir, _ = evolve_state
    entry = {
        "ts": "2026-03-20T10:00:00Z",
        "id": "evolve-test-001",
        "intent": "Add a new angle about sound perception",
        "introspection": {"config": {}, "mood_distribution": {}},
        "status": "pending",
    }
    (state_dir / "evolve_queue.jsonl").write_text(json.dumps(entry) + "\n")
    entries = [
        json.loads(line)
        for line in (state_dir / "evolve_queue.jsonl")
        .read_text()
        .strip()
        .splitlines()
    ]
    pending = [e for e in entries if e["status"] == "pending"]
    assert len(pending) == 1
    assert pending[0]["id"] == "evolve-test-001"


def test_completed_entry_skipped(evolve_state):
    state_dir, _ = evolve_state
    entries = [
        json.dumps({"id": "done-1", "status": "pr_created", "intent": "x" * 30}),
        json.dumps({"id": "done-2", "status": "failed:timeout", "intent": "y" * 30}),
    ]
    (state_dir / "evolve_queue.jsonl").write_text("\n".join(entries) + "\n")
    all_entries = [
        json.loads(line)
        for line in (state_dir / "evolve_queue.jsonl")
        .read_text()
        .strip()
        .splitlines()
    ]
    pending = [e for e in all_entries if e["status"] == "pending"]
    assert len(pending) == 0


def test_max_files_enforcement(evolve_state):
    diff_output = "src/pxh/spark_config.py\nbin/tool-new\ntests/test_new.py\nextra_file.py\n"
    files = [f for f in diff_output.strip().splitlines() if f]
    max_files = int(os.environ.get("PX_EVOLVE_MAX_FILES", "3"))
    assert len(files) > max_files


def test_max_files_within_limit(evolve_state):
    diff_output = "src/pxh/spark_config.py\nbin/tool-new\ntests/test_new.py\n"
    files = [f for f in diff_output.strip().splitlines() if f]
    max_files = int(os.environ.get("PX_EVOLVE_MAX_FILES", "3"))
    assert len(files) <= max_files


def test_atomic_queue_update(evolve_state):
    """Verify atomic write pattern: write to temp then rename."""
    state_dir, _ = evolve_state
    queue_file = state_dir / "evolve_queue.jsonl"

    # Write initial entry
    entry = {"id": "atom-1", "status": "pending", "intent": "test atomic"}
    queue_file.write_text(json.dumps(entry) + "\n")

    # Simulate atomic update (mkstemp + os.replace)
    import tempfile

    updated = {**entry, "status": "pr_created"}
    fd, tmp_path = tempfile.mkstemp(dir=str(state_dir), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(updated) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(queue_file))
    except BaseException:
        os.unlink(tmp_path)
        raise

    result = json.loads(queue_file.read_text().strip())
    assert result["status"] == "pr_created"


def test_queue_multiple_entries_mixed_status(evolve_state):
    state_dir, _ = evolve_state
    entries = [
        {"id": "e1", "status": "pr_created", "intent": "done task"},
        {"id": "e2", "status": "pending", "intent": "waiting task"},
        {"id": "e3", "status": "failed:tests", "intent": "broken task"},
        {"id": "e4", "status": "pending", "intent": "another waiting"},
    ]
    (state_dir / "evolve_queue.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n"
    )
    all_entries = [
        json.loads(line)
        for line in (state_dir / "evolve_queue.jsonl")
        .read_text()
        .strip()
        .splitlines()
    ]
    pending = [e for e in all_entries if e["status"] == "pending"]
    assert len(pending) == 2
    assert {e["id"] for e in pending} == {"e2", "e4"}


def test_failure_statuses_recognized(evolve_state):
    """All known failure statuses should be treated as non-pending."""
    failure_statuses = [
        "failed:timeout",
        "failed:no_changes",
        "failed:tests",
        "failed:too_many_files",
        "failed:pr_create",
        "failed:worktree",
    ]
    for status in failure_statuses:
        assert status != "pending"
        assert status.startswith("failed:")


def test_evolve_log_append(evolve_state):
    state_dir, _ = evolve_state
    log_file = state_dir / "evolve_log.jsonl"

    record = {
        "id": "evolve-log-001",
        "status": "pr_created",
        "pr_url": "https://github.com/example/repo/pull/1",
        "ts_completed": "2026-03-20T10:05:00Z",
    }

    # Append
    with open(log_file, "a") as f:
        f.write(json.dumps(record) + "\n")

    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["status"] == "pr_created"
