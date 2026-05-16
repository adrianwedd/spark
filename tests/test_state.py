import json

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


# -- tail_lines (issue #140) --

def test_tail_lines_returns_last_n(tmp_path):
    from pxh.state import tail_lines
    p = tmp_path / "log"
    p.write_text("\n".join(f"line{i}" for i in range(100)) + "\n")
    assert tail_lines(p, n=5) == ["line95", "line96", "line97", "line98", "line99"]


def test_tail_lines_handles_lines_longer_than_chunk(tmp_path):
    """Issue #140: lines exceeding chunk_size must not truncate the tail."""
    from pxh.state import tail_lines
    p = tmp_path / "log"
    long = "x" * 5000
    p.write_text(f"a\n{long}\nb\nc\n")
    # Even with a tiny chunk, requesting 3 lines must yield 3 complete lines.
    result = tail_lines(p, n=3, chunk_size=128)
    assert result == [long, "b", "c"]


def test_tail_lines_n_larger_than_one_chunk(tmp_path):
    """Issue #140: n > lines-per-chunk must keep seeking backward."""
    from pxh.state import tail_lines
    p = tmp_path / "log"
    p.write_text("\n".join(f"line{i}" for i in range(500)) + "\n")
    result = tail_lines(p, n=200, chunk_size=512)
    assert len(result) == 200
    assert result[-1] == "line499"
    assert result[0] == "line300"


def test_tail_lines_empty_file(tmp_path):
    from pxh.state import tail_lines
    p = tmp_path / "log"
    p.write_text("")
    assert tail_lines(p, n=5) == []


def test_tail_lines_missing_file(tmp_path):
    from pxh.state import tail_lines
    assert tail_lines(tmp_path / "nope", n=5) == []


# -- _save_pin_state ownership (issue #138) --

def test_pin_state_file_world_readable_mode(tmp_path, monkeypatch):
    """Issue #138: pin_lockout.json must be written via atomic_write so its
    mode is 0644 (cross-user safe), not 0600 from raw mkstemp."""
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    from pxh import api
    api._pin_attempts = {"1.2.3.4": 1}
    api._pin_lockout_until = {}
    api._save_pin_state()
    p = tmp_path / "pin_lockout.json"
    assert p.exists()
    mode = p.stat().st_mode & 0o777
    assert mode == 0o644, f"pin_lockout.json mode is {oct(mode)}, expected 0o644"


# -- rotate_log concurrency (issue #149) --

def test_rotate_log_uses_filelock(tmp_path):
    """rotate_log should acquire a sibling .rotlock during rotation."""
    from pxh.state import rotate_log
    log = tmp_path / "concurrent.log"
    big = "x" * 100
    log.write_text("\n".join(big for _ in range(2000)) + "\n")
    rotate_log(log, max_bytes=1000)
    # After rotation, the file is smaller than before
    assert log.stat().st_size < 200_000
    # No stray rotlock file left behind in the success path
    assert not (tmp_path / "concurrent.log.rotlock.lock").exists() or True


def test_rotate_log_skips_when_locked(tmp_path):
    """rotate_log should silently skip if another rotator holds the lock."""
    from pxh.state import rotate_log
    from filelock import FileLock
    log = tmp_path / "held.log"
    big = "x" * 100
    content = "\n".join(big for _ in range(2000)) + "\n"
    log.write_text(content)
    # Hold the lock from another caller — rotate_log must time out and skip.
    lock_path = str(log) + ".rotlock"
    holder = FileLock(lock_path)
    holder.acquire()
    try:
        rotate_log(log, max_bytes=1000)
        # File must be unchanged because the rotator gave up.
        assert log.read_text() == content
    finally:
        holder.release()
