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


def test_default_state_includes_robot_name(tmp_path, monkeypatch):
    """robot_name is consumed by mcp_server status; it must be initialised
    in both default_state() and the on-disk template (ensure_session prefers
    the template) so it is never the empty string in a real session."""
    session_file = tmp_path / "session.json"
    monkeypatch.setenv("PX_SESSION_PATH", str(session_file))
    assert state.default_state()["robot_name"] == "Spark"
    # ensure_session writes from the repo template — robot_name must round-trip
    state.ensure_session()
    loaded = json.loads(session_file.read_text())
    assert loaded["robot_name"] == "Spark"


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
    assert not (tmp_path / "concurrent.log.rotlock.lock").exists()


# -- set_quiet_mode / clear_quiet_mode (#209) --

def test_set_quiet_mode_does_not_persist_spark_quiet_mode(tmp_path, monkeypatch):
    """Once quiet_state exists, spark_quiet_mode must never be written to
    disk as a competing field — only derived at read time. A stray on-disk
    True/False here would be exactly the kind of value that could go stale
    relative to quiet_state (e.g. after a TTL lapses with nothing rewriting
    it) and mislead any future reader that doesn't go through resolve()."""
    session_file = tmp_path / "session.json"
    monkeypatch.setenv("PX_SESSION_PATH", str(session_file))
    state.set_quiet_mode(enabled=True, source="test", reason="r")
    raw = json.loads(session_file.read_text())
    assert "quiet_state" in raw
    assert raw["quiet_state"]["enabled"] is True
    assert "spark_quiet_mode" not in raw


def test_set_quiet_mode_return_value_has_derived_spark_quiet_mode(tmp_path, monkeypatch):
    """Callers that inspect the return value (dashboard PATCH response, tool
    payloads) must still see a correct spark_quiet_mode, even though it is
    never written to the file."""
    session_file = tmp_path / "session.json"
    monkeypatch.setenv("PX_SESSION_PATH", str(session_file))
    result = state.set_quiet_mode(enabled=True, source="test")
    assert result["spark_quiet_mode"] is True
    result = state.clear_quiet_mode(source="test")
    assert result["spark_quiet_mode"] is False


def test_set_quiet_mode_history_records_previous_state_and_transition(tmp_path, monkeypatch):
    """A real False->True transition is distinguishable from a subsequent
    idempotent True->True reaffirmation by previous_enabled/transition."""
    session_file = tmp_path / "session.json"
    monkeypatch.setenv("PX_SESSION_PATH", str(session_file))

    state.set_quiet_mode(enabled=True, source="test", reason="first")
    entry = json.loads(session_file.read_text())["history"][-1]
    assert entry["previous_enabled"] is False
    assert entry["enabled"] is True
    assert entry["transition"] is True

    state.set_quiet_mode(enabled=True, source="test", reason="reaffirm")
    entry = json.loads(session_file.read_text())["history"][-1]
    assert entry["previous_enabled"] is True
    assert entry["enabled"] is True
    assert entry["transition"] is False

    state.clear_quiet_mode(source="test", reason="end")
    entry = json.loads(session_file.read_text())["history"][-1]
    assert entry["previous_enabled"] is True
    assert entry["enabled"] is False
    assert entry["transition"] is True


def test_set_quiet_mode_history_previous_state_reflects_expired_ttl(tmp_path, monkeypatch):
    """previous_enabled must come from resolve(), not a raw bool — a lapsed
    TTL buffer should read as previous_enabled=False even though the stale
    record on disk still says enabled=True."""
    session_file = tmp_path / "session.json"
    monkeypatch.setenv("PX_SESSION_PATH", str(session_file))
    state.set_quiet_mode(enabled=True, source="test", ttl_s=-1)  # already expired
    state.set_quiet_mode(enabled=True, source="test", reason="new_buffer")
    entry = json.loads(session_file.read_text())["history"][-1]
    assert entry["previous_enabled"] is False


def test_load_session_never_persists_derived_spark_quiet_mode(tmp_path, monkeypatch):
    """load_session()'s derivation must stay read-only — a lapsed quiet_state
    should read as inactive without load_session() rewriting the record."""
    session_file = tmp_path / "session.json"
    monkeypatch.setenv("PX_SESSION_PATH", str(session_file))
    state.set_quiet_mode(enabled=True, source="test", ttl_s=-1)  # already expired
    data = state.load_session()
    assert data["spark_quiet_mode"] is False
    raw = json.loads(session_file.read_text())
    assert raw["quiet_state"]["enabled"] is True  # unchanged on disk
    assert "spark_quiet_mode" not in raw


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


# -- one-shot durable legacy-quiet migration (#303) --
#
# The #285 model made quiet_state authoritative but migrated the legacy
# spark_quiet_mode bool at READ time only, so a legacy-only file (the #209
# latch) kept the naked bool as perpetual authority. These pin the durable
# half: the first persist converts the file, exactly once, conservatively,
# and the legacy key can never again be persisted as authority.

def _write_raw(session_file, payload):
    session_file.write_text(json.dumps(payload, indent=2) + "\n")


def test_legacy_true_migrates_conservatively_on_first_write(tmp_path, monkeypatch):
    session_file = tmp_path / "session.json"
    monkeypatch.setenv("PX_SESSION_PATH", str(session_file))
    _write_raw(session_file, {"spark_quiet_mode": True, "history": []})

    state.update_session(fields={"mode": "live"})

    raw = json.loads(session_file.read_text())
    assert "spark_quiet_mode" not in raw
    qs = raw["quiet_state"]
    assert qs["enabled"] is True
    assert qs["source"] == "unknown"
    assert qs["reason"] == "legacy_migration"
    assert qs["set_at"] is None
    assert qs["expires_at"] is None  # indefinite — no TTL invented
    # Effective quiet is unchanged by the migration: still latched.
    assert state.load_session()["spark_quiet_mode"] is True
    # The conversion itself is on the record.
    events = [e for e in raw["history"] if e.get("event") == "quiet_mode_migrated"]
    assert len(events) == 1
    assert events[0]["enabled"] is True
    assert events[0]["source"] == "unknown"


def test_legacy_false_migrates_to_disabled_record(tmp_path, monkeypatch):
    session_file = tmp_path / "session.json"
    monkeypatch.setenv("PX_SESSION_PATH", str(session_file))
    _write_raw(session_file, {"spark_quiet_mode": False, "history": []})

    state.update_session(fields={"mode": "live"})

    raw = json.loads(session_file.read_text())
    assert "spark_quiet_mode" not in raw
    assert raw["quiet_state"]["enabled"] is False
    assert raw["quiet_state"]["reason"] == "legacy_migration"
    assert state.load_session()["spark_quiet_mode"] is False


def test_explicit_null_quiet_state_still_migrates(tmp_path, monkeypatch):
    # default_state() historically wrote quiet_state: null next to the bool —
    # null and a missing key are the same "never written" claim.
    session_file = tmp_path / "session.json"
    monkeypatch.setenv("PX_SESSION_PATH", str(session_file))
    _write_raw(session_file, {"spark_quiet_mode": True, "quiet_state": None, "history": []})

    state.update_session(fields={"mode": "live"})

    raw = json.loads(session_file.read_text())
    assert raw["quiet_state"]["enabled"] is True
    assert "spark_quiet_mode" not in raw


def test_migration_happens_exactly_once(tmp_path, monkeypatch):
    """Writing (and re-reading) old state repeatedly must not remigrate or
    rewrite history — once quiet_state exists the trigger can never re-fire."""
    session_file = tmp_path / "session.json"
    monkeypatch.setenv("PX_SESSION_PATH", str(session_file))
    _write_raw(session_file, {"spark_quiet_mode": True, "history": []})

    state.update_session(fields={"mode": "live"})
    first = json.loads(session_file.read_text())["quiet_state"]

    state.load_session()
    state.update_session(fields={"mode": "dry-run"})
    state.update_session(history_entry={"event": "noise"})

    raw = json.loads(session_file.read_text())
    assert raw["quiet_state"] == first  # record byte-identical, not rewritten
    events = [e for e in raw["history"] if e.get("event") == "quiet_mode_migrated"]
    assert len(events) == 1


def test_legacy_bool_cannot_relatch_once_canonical_state_exists(tmp_path, monkeypatch):
    """A hand-reintroduced (or stale) legacy bool next to canonical state
    changes nothing and is swept from disk on the next write."""
    session_file = tmp_path / "session.json"
    monkeypatch.setenv("PX_SESSION_PATH", str(session_file))
    state.clear_quiet_mode(source="test", reason="baseline")

    # Someone writes the naked bool back by hand, disagreeing with canonical.
    raw = json.loads(session_file.read_text())
    raw["spark_quiet_mode"] = True
    _write_raw(session_file, raw)

    assert state.load_session()["spark_quiet_mode"] is False  # canonical wins
    state.update_session(fields={"mode": "live"})
    raw = json.loads(session_file.read_text())
    assert "spark_quiet_mode" not in raw  # swept
    assert raw["quiet_state"]["enabled"] is False
    # And removing the legacy key can't change effective quiet either.
    assert state.load_session()["spark_quiet_mode"] is False


def test_update_session_refuses_direct_legacy_field_write(tmp_path, monkeypatch):
    """No writer may persist spark_quiet_mode as authority — a direct
    fields={} write is dropped (with a warning), not honoured."""
    session_file = tmp_path / "session.json"
    monkeypatch.setenv("PX_SESSION_PATH", str(session_file))
    state.clear_quiet_mode(source="test", reason="baseline")

    state.update_session(fields={"spark_quiet_mode": True, "mode": "live"})

    raw = json.loads(session_file.read_text())
    assert raw["mode"] == "live"  # the legitimate field went through
    assert "spark_quiet_mode" not in raw
    assert raw["quiet_state"]["enabled"] is False
    assert state.load_session()["spark_quiet_mode"] is False


def test_save_session_round_trip_does_not_recreate_legacy_shape(tmp_path, monkeypatch):
    """load_session() injects the derived bool into the dict it returns; a
    whole-dict save must not persist that injection back as durable state."""
    session_file = tmp_path / "session.json"
    monkeypatch.setenv("PX_SESSION_PATH", str(session_file))
    state.set_quiet_mode(enabled=True, source="test", reason="r")

    data = state.load_session()
    assert data["spark_quiet_mode"] is True  # injected for readers
    state.save_session(data)

    raw = json.loads(session_file.read_text())
    assert "spark_quiet_mode" not in raw
    assert raw["quiet_state"]["enabled"] is True
    assert data["spark_quiet_mode"] is True  # caller's dict not mutated


def test_save_session_migrates_a_legacy_only_dict(tmp_path, monkeypatch):
    session_file = tmp_path / "session.json"
    monkeypatch.setenv("PX_SESSION_PATH", str(session_file))
    state.save_session({"spark_quiet_mode": True, "history": []})

    raw = json.loads(session_file.read_text())
    assert "spark_quiet_mode" not in raw
    assert raw["quiet_state"]["enabled"] is True
    assert raw["quiet_state"]["reason"] == "legacy_migration"


def test_malformed_quiet_state_is_left_untouched_and_fails_closed(tmp_path, monkeypatch):
    """Garbage quiet_state is evidence of a broken writer: the durable
    migration must not launder it, and reads keep failing closed."""
    session_file = tmp_path / "session.json"
    monkeypatch.setenv("PX_SESSION_PATH", str(session_file))
    garbage = {"enabled": "yes", "who": "wrote this?"}
    _write_raw(session_file, {"spark_quiet_mode": False, "quiet_state": garbage, "history": []})

    state.update_session(fields={"mode": "live"})

    raw = json.loads(session_file.read_text())
    assert raw["quiet_state"] == garbage        # untouched — forensics intact
    assert raw["spark_quiet_mode"] is False     # untouched too
    assert state.load_session()["spark_quiet_mode"] is True  # fails closed


def test_set_quiet_mode_on_legacy_only_file_logs_no_spurious_migration(tmp_path, monkeypatch):
    """The canonical writer's own persist brings quiet_state in via fields,
    so it must never double-log a quiet_mode_migrated entry for the same
    transition it is already recording as quiet_mode_set."""
    session_file = tmp_path / "session.json"
    monkeypatch.setenv("PX_SESSION_PATH", str(session_file))
    _write_raw(session_file, {"spark_quiet_mode": True, "history": []})

    state.clear_quiet_mode(source="tool_quiet", reason="three_ss_end")

    raw = json.loads(session_file.read_text())
    events = [e.get("event") for e in raw["history"]]
    assert "quiet_mode_migrated" not in events
    assert "quiet_mode_clear" in events
    # previous_enabled was still computed from the legacy shape correctly.
    entry = [e for e in raw["history"] if e.get("event") == "quiet_mode_clear"][-1]
    assert entry["previous_enabled"] is True
    assert entry["transition"] is True
    assert "spark_quiet_mode" not in raw


def test_operator_clear_works_after_migration(tmp_path, monkeypatch):
    """The end-to-end #303 story: an unattributable legacy latch is imported
    conservatively (still quiet, origin visible as unknown), then a later
    deliberate operator clear through the canonical writer releases it."""
    session_file = tmp_path / "session.json"
    monkeypatch.setenv("PX_SESSION_PATH", str(session_file))
    _write_raw(session_file, {"spark_quiet_mode": True, "history": []})

    state.update_session(fields={})  # any write migrates
    assert state.load_session()["spark_quiet_mode"] is True

    state.clear_quiet_mode(source="tool_quiet", reason="three_ss_end")
    data = state.load_session()
    assert data["spark_quiet_mode"] is False
    raw = json.loads(session_file.read_text())
    assert raw["quiet_state"]["enabled"] is False
    assert raw["quiet_state"]["source"] == "tool_quiet"


def test_default_state_has_no_legacy_quiet_key(tmp_path, monkeypatch):
    """The derived field must never exist as a durable default — a fresh
    session with the bool would be exactly the legacy-only shape the
    migration retires (and would migrate with a spurious history entry)."""
    session_file = tmp_path / "session.json"
    monkeypatch.setenv("PX_SESSION_PATH", str(session_file))
    assert "spark_quiet_mode" not in state.default_state()
    # But every loaded view still carries the derived bool for readers.
    assert state.load_session()["spark_quiet_mode"] is False
    raw = json.loads(session_file.read_text())
    assert "spark_quiet_mode" not in raw
    events = [e for e in raw.get("history", []) if e.get("event") == "quiet_mode_migrated"]
    assert events == []  # fresh sessions have nothing to migrate
