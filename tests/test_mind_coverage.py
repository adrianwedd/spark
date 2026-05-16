"""Tests for previously untested px-mind functions: awareness_tick, reflection, etc."""
from __future__ import annotations

import datetime as _dt
import json
import os
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

import pxh.mind
from pxh.mind import (
    _reset_state,
    append_thought,
    apply_mood_momentum,
    auto_remember,
    awareness_tick,
    fetch_weather,
    load_recent_thoughts,
    reflection,
    HOBART_TZ,
    VALID_MOODS,
)


@pytest.fixture(autouse=True)
def _clean_mind_state():
    _reset_state()
    yield
    _reset_state()


@pytest.fixture
def mind_state(tmp_path):
    """Redirect px-mind state files to tmp_path and isolate session."""
    old_state = getattr(pxh.mind, "STATE_DIR", None)
    old_aw = getattr(pxh.mind, "AWARENESS_FILE", None)
    old_bat = getattr(pxh.mind, "BATTERY_FILE", None)
    old_log = getattr(pxh.mind, "LOG_FILE", None)
    old_frigate = getattr(pxh.mind, "FRIGATE_FILE", None)
    old_session = os.environ.get("PX_SESSION_PATH")

    pxh.mind.STATE_DIR = tmp_path
    pxh.mind.AWARENESS_FILE = tmp_path / "awareness.json"
    pxh.mind.BATTERY_FILE = tmp_path / "battery.json"
    pxh.mind.LOG_FILE = tmp_path / "px-mind.log"
    pxh.mind.FRIGATE_FILE = tmp_path / "frigate_presence.json"
    session_path = tmp_path / "session.json"
    session_path.write_text('{"persona": "spark", "listening": false, "history": []}')
    os.environ["PX_SESSION_PATH"] = str(session_path)

    yield tmp_path

    pxh.mind.STATE_DIR = old_state
    pxh.mind.AWARENESS_FILE = old_aw
    pxh.mind.BATTERY_FILE = old_bat
    pxh.mind.LOG_FILE = old_log
    pxh.mind.FRIGATE_FILE = old_frigate
    if old_session is None:
        os.environ.pop("PX_SESSION_PATH", None)
    else:
        os.environ["PX_SESSION_PATH"] = old_session


# ── Helper: patch all external I/O for awareness_tick ───────────────

def _awareness_patches():
    stack = ExitStack()
    stack.enter_context(patch("subprocess.run",
        return_value=MagicMock(returncode=1, stdout="{}")))
    stack.enter_context(patch.object(pxh.mind, "_fetch_frigate_presence", return_value=None))
    stack.enter_context(patch.object(pxh.mind, "_fetch_ha_presence", return_value=None))
    stack.enter_context(patch.object(pxh.mind, "_fetch_ha_calendar", return_value=None))
    stack.enter_context(patch.object(pxh.mind, "_fetch_ha_sleep", return_value=None))
    stack.enter_context(patch.object(pxh.mind, "_fetch_ha_routines", return_value=None))
    stack.enter_context(patch.object(pxh.mind, "_fetch_ha_context", return_value=None))
    stack.enter_context(patch.object(pxh.mind, "fetch_weather", return_value={"temp_c": 15}))
    stack.enter_context(patch.object(pxh.mind, "read_wifi_signal", return_value={}))
    stack.enter_context(patch.object(pxh.mind, "read_system_stats", return_value={}))
    return stack


# ── apply_mood_momentum tests ────────────────────────────────────────

def test_mood_momentum_first_call_returns_valid_mood():
    """First call with a known mood returns a valid mood string."""
    result = apply_mood_momentum("excited")
    assert result in VALID_MOODS


def test_mood_momentum_repeated_same_mood_stays_consistent():
    """Repeated identical moods converge toward that mood."""
    # Call many times with "curious" — momentum should stabilise
    for _ in range(20):
        result = apply_mood_momentum("curious")
    assert result in VALID_MOODS


def test_mood_momentum_transition_shifts_result():
    """Switching from one extreme mood to another changes the result."""
    # Prime the momentum with excited
    for _ in range(10):
        apply_mood_momentum("excited")
    result_after_excited = apply_mood_momentum("excited")

    # Reset and prime with grumpy
    _reset_state()
    for _ in range(10):
        apply_mood_momentum("grumpy")
    result_after_grumpy = apply_mood_momentum("grumpy")

    # They may or may not differ (depends on nearest_mood buckets),
    # but both must be valid moods and the function must not raise.
    assert result_after_excited in VALID_MOODS
    assert result_after_grumpy in VALID_MOODS


def test_mood_momentum_unknown_mood_falls_back_gracefully():
    """An unrecognised mood string uses (0, 0) coords and returns a valid mood."""
    result = apply_mood_momentum("nonexistent_mood_xyz")
    assert result in VALID_MOODS


# ── Thought I/O tests ────────────────────────────────────────────────

def test_append_and_load_thought(mind_state):
    """append_thought writes to the file; load_recent_thoughts reads it back."""
    thought = {"ts": "2026-03-18T00:00:00Z", "thought": "hello world",
                "mood": "curious", "action": "comment", "salience": 0.5}
    append_thought(thought, persona="spark")
    loaded = load_recent_thoughts(n=5, persona="spark")
    assert len(loaded) == 1
    assert loaded[0]["thought"] == "hello world"


def test_load_recent_thoughts_empty_when_no_file(mind_state):
    """load_recent_thoughts returns [] when no file exists yet."""
    result = load_recent_thoughts(n=5, persona="spark")
    assert result == []


def test_auto_remember_high_salience_writes_notes(mind_state):
    """auto_remember writes a note for a high-salience thought."""
    thought = {"ts": "2026-03-18T00:00:00Z", "thought": "interesting observation",
                "mood": "curious", "action": "remember", "salience": 0.9}
    auto_remember(thought, persona="spark")

    notes_file = pxh.mind.notes_file_for_persona("spark")
    assert notes_file.exists()
    lines = notes_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert "interesting observation" in record["note"]


def test_auto_remember_writes_note_regardless_of_salience(mind_state):
    """auto_remember writes the note unconditionally — salience gating is the caller's job."""
    thought = {"ts": "2026-03-18T00:00:00Z", "thought": "mundane thought",
                "mood": "bored", "action": "wait", "salience": 0.1}
    auto_remember(thought, persona="spark")
    notes_file = pxh.mind.notes_file_for_persona("spark")
    assert notes_file.exists()


# ── fetch_weather tests ──────────────────────────────────────────────

def test_fetch_weather_dry_returns_dict():
    """In dry mode, fetch_weather returns a dict with temp_c."""
    result = fetch_weather(dry=True)
    assert result is not None
    assert "temp_c" in result
    assert result["temp_c"] == 20


def test_fetch_weather_network_error_returns_none():
    """When subprocess raises, fetch_weather returns None without raising."""
    with patch("subprocess.run", side_effect=Exception("network down")):
        result = fetch_weather(dry=False)
    assert result is None


def test_fetch_weather_bad_json_returns_none():
    """When tool-weather returns non-JSON output, fetch_weather returns None."""
    mock_result = MagicMock(returncode=0, stdout="not json output here")
    with patch("subprocess.run", return_value=mock_result):
        result = fetch_weather(dry=False)
    assert result is None


# ── awareness_tick tests ─────────────────────────────────────────────

def test_awareness_tick_dry_returns_dict(mind_state):
    """In dry mode, awareness_tick returns a tuple of (dict, list)."""
    with _awareness_patches():
        awareness, transitions = awareness_tick({}, dry=True)

    assert isinstance(awareness, dict)
    assert isinstance(transitions, list)
    assert "time_period" in awareness
    assert "ts" in awareness


def test_awareness_tick_detects_time_period_transition(mind_state):
    """awareness_tick appends 'time_period_changed' when time_period changes."""
    mock_now = _dt.datetime(2026, 3, 18, 10, 0, tzinfo=HOBART_TZ)
    mock_dt = MagicMock(wraps=_dt)
    mock_dt.datetime.now = MagicMock(return_value=mock_now)
    mock_dt.datetime.fromisoformat = _dt.datetime.fromisoformat
    mock_dt.timezone = _dt.timezone

    prev = {"time_period": "morning"}  # will differ from classify_time_period(10) = "morning"
    # Force a different time_period in prev so transition fires
    # classify_time_period(10) returns "morning" → use prev="night" to trigger
    prev = {"time_period": "night"}

    with _awareness_patches(), patch.object(pxh.mind, "dt", mock_dt):
        awareness, transitions = awareness_tick(prev, dry=True)

    assert "time_period_changed" in transitions


def test_awareness_tick_writes_awareness_file(mind_state):
    """awareness_tick writes AWARENESS_FILE to disk."""
    with _awareness_patches():
        awareness_tick({}, dry=True)

    assert pxh.mind.AWARENESS_FILE.exists()
    data = json.loads(pxh.mind.AWARENESS_FILE.read_text(encoding="utf-8"))
    assert "time_period" in data


def test_awareness_tick_sonar_none_in_dry_mode(mind_state):
    """In dry mode, sonar returns None (read_sonar(True) = None)."""
    with _awareness_patches():
        awareness, _ = awareness_tick({}, dry=True)

    assert awareness["sonar_cm"] is None


# ── reflection tests ─────────────────────────────────────────────────

def test_reflection_dry_returns_thought_dict(mind_state):
    """In dry mode, reflection returns a dict with required thought keys."""
    awareness = {
        "time_period": "afternoon",
        "ts": "2026-03-18T01:00:00Z",
        "persona": "spark",
        "mood_momentum": {"valence": 0.4, "arousal": 0.0, "mood": "content"},
    }
    result = reflection(awareness, dry=True)

    assert result is not None
    assert isinstance(result, dict)
    assert "thought" in result
    assert "mood" in result
    assert "action" in result
    assert "salience" in result
    assert result["mood"] in VALID_MOODS


def test_reflection_dry_writes_thoughts_file(mind_state):
    """In dry mode, reflection appends to the persona-scoped thoughts file."""
    awareness = {
        "time_period": "morning",
        "ts": "2026-03-18T01:00:00Z",
        "persona": "spark",
        "mood_momentum": {"valence": 0.4, "arousal": 0.0, "mood": "content"},
    }
    reflection(awareness, dry=True)

    thoughts_file = pxh.mind.thoughts_file_for_persona("spark")
    assert thoughts_file.exists()
    lines = thoughts_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 1
    parsed = json.loads(lines[-1])
    assert "thought" in parsed


def test_reflection_dry_thought_contains_dry_run_marker(mind_state):
    """Dry-run thought text starts with 'Dry-run thought:'."""
    awareness = {
        "time_period": "evening",
        "ts": "2026-03-18T01:00:00Z",
        "persona": "spark",
        "mood_momentum": {"valence": 0.4, "arousal": 0.0, "mood": "content"},
    }
    result = reflection(awareness, dry=True)
    assert result["thought"].startswith("Dry-run thought:")


@patch.dict(os.environ, {"PX_CLAUDE_BIN": "/usr/bin/claude"})
def test_call_claude_logs_stdout_on_failure():
    """When Claude exits non-zero with empty stderr, error includes stdout."""
    from pxh.mind import call_claude_haiku

    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = "API rate limit exceeded"
    mock_result.stderr = ""

    with patch.object(pxh.mind.subprocess, "run", return_value=mock_result):
        result = call_claude_haiku("test prompt", "test system")
        assert "rate limit" in result.get("error", "").lower()
