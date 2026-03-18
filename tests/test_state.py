import json
from pathlib import Path

import pxh.state as state

def test_update_session_appends_history(tmp_path, monkeypatch):
    session_file = tmp_path / "session.json"
    monkeypatch.setenv("PX_SESSION_PATH", str(session_file))
    data = state.update_session(
        fields={"mode": "live"}, history_entry={"event": "alpha"}
    )
    assert data["mode"] == "live"
    assert data["history"]
    assert data["history"][0]["event"] == "alpha"

    # Exceed history limit to ensure truncation works
    for idx in range(1, 105):
        state.update_session(history_entry={"event": f"e{idx}"})
    data = state.load_session()
    assert len(data["history"]) == 100
    assert data["history"][0]["event"].startswith("e")


def test_rotate_log_under_threshold(tmp_path):
    """File under 5MB is not rotated."""
    log = tmp_path / "test.log"
    log.write_text("line1\nline2\n")
    from pxh.state import rotate_log
    rotate_log(log)
    assert log.read_text() == "line1\nline2\n"


def test_rotate_log_over_threshold(tmp_path):
    """File over threshold keeps last half of lines."""
    log = tmp_path / "test.log"
    lines = [f"line{i}" for i in range(100)]
    log.write_text("\n".join(lines) + "\n")
    from pxh.state import rotate_log
    rotate_log(log, max_bytes=50)  # force rotation with low threshold
    content = log.read_text()
    result_lines = content.strip().split("\n")
    assert len(result_lines) == 50  # kept last half
    assert result_lines[0] == "line50"
    assert result_lines[-1] == "line99"


def test_rotate_log_missing_file(tmp_path):
    """Missing file does not raise."""
    log = tmp_path / "nonexistent.log"
    from pxh.state import rotate_log
    rotate_log(log)  # should not raise


def test_default_state_contains_tracking_fields(tmp_path, monkeypatch):
    session_file = tmp_path / "session.json"
    monkeypatch.setenv("PX_SESSION_PATH", str(session_file))
    defaults = state.default_state()
    expected_keys = {
        "last_weather",
        "last_prompt_excerpt",
        "last_model_action",
        "last_tool_payload",
    }
    assert expected_keys.issubset(defaults.keys())
    # Ensure ensure_session creates file matching template
    state.ensure_session()
    loaded = json.loads(session_file.read_text())
    assert all(key in loaded for key in expected_keys)
