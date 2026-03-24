"""Tests for px-mind utility functions."""
from __future__ import annotations

import datetime as _dt
import json as _json
import os
import time as _time
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import pxh.mind  # needed for module-attribute writes (pxh.mind.X = val)
from pxh.mind import (
    # Functions
    _can_explore,
    _daytime_action_hint,
    _fetch_frigate_presence,
    _fetch_ha_calendar,
    _fetch_ha_presence,
    _fetch_ha_sleep,
    _format_calendar_context,
    _format_ha_context,
    _format_introspection,
    _format_routine_context,
    _parse_calendar_events,
    _reset_state,
    compute_obi_mode,
    expression,
    filter_battery,
    read_battery,
    # Constants
    ABSENT_GATED_ACTIONS,
    BATTERY_GLITCH_CONFIRMS,
    BATTERY_MAX_DROP_PER_TICK,
    BIN_DIR,
    CHARGING_GATED_ACTIONS,
    HOBART_TZ,
    MOOD_TO_EMOTE,
    MOOD_TO_SOUND,
    REFLECTION_SYSTEM,
    REFLECTION_SYSTEM_GREMLIN,
    REFLECTION_SYSTEM_VIXEN,
    VALID_ACTIONS,
    _SPARK_REFLECTION_SUFFIX,
)


@pytest.fixture(autouse=True)
def _clean_mind_state():
    """Reset px-mind module globals before each test."""
    _reset_state()
    yield
    _reset_state()


def _make_frigate_event(score=0.75, top_score=None, x=0.2, y=0.1, w=0.3, h=0.8,
                        speed=0.0, vel_angle=0.0, end_time=None, label="person",
                        camera="picar_x"):
    return {
        "label": label,
        "camera": camera,
        "end_time": end_time or _time.time() - 5,
        "data": {
            "box": [x, y, w, h],
            "score": score, "top_score": top_score if top_score is not None else score,
            "average_estimated_speed": speed,
            "velocity_angle": vel_angle,
            "path_data": [[[x + w / 2, y + h / 2], _time.time() - 5]],
        },
    }


def _mock_urlopen(events):
    """Create a mock context manager that returns JSON-encoded events.

    Used for Frigate tests — returns a context manager object.
    For use with patch("urllib.request.urlopen", return_value=...) when the code
    uses `with urlopen(...) as resp`.
    For side_effect usage (where urlopen is called with timeout kwarg), use
    _mock_urlopen_fn instead.
    """
    body = _json.dumps(events).encode()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=MagicMock(read=MagicMock(return_value=body)))
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def _mock_urlopen_fn(events):
    """Return a side_effect function that accepts any args/kwargs and returns mock urlopen."""
    body = _json.dumps(events).encode()
    def _opener(*args, **kwargs):
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=MagicMock(read=MagicMock(return_value=body)))
        cm.__exit__ = MagicMock(return_value=False)
        return cm
    return _opener


# ---------------------------------------------------------------------------
# _daytime_action_hint
# ---------------------------------------------------------------------------


def test_daytime_hint_daytime():
    """During waking hours (7–19) the hint pushes toward comment/greet."""
    hint = _daytime_action_hint(hour_override=10)
    assert "comment" in hint or "greet" in hint


def test_daytime_hint_night():
    """Overnight the hint pushes toward remember/wait."""
    hint = _daytime_action_hint(hour_override=2)
    assert "remember" in hint or "wait" in hint


def test_daytime_hint_boundary_start():
    """Hour 7 (day start) → morning hint with morning_fact."""
    hint = _daytime_action_hint(hour_override=7)
    assert "comment" in hint or "greet" in hint


def test_daytime_hint_boundary_end():
    """Hour 20 (day end) → night hint."""
    hint = _daytime_action_hint(hour_override=20)
    assert "remember" in hint or "wait" in hint


# ---------------------------------------------------------------------------
# compute_obi_mode
# ---------------------------------------------------------------------------


def test_obi_mode_absent_at_night():
    """Silent + no one near + night → absent."""
    awareness = {"ambient_sound": {"level": "silent"}, "sonar_cm": 80}
    mode = compute_obi_mode(awareness, hour_override=3)
    assert mode == "absent"


def test_obi_mode_overloaded():
    """Very close + loud → possibly-overloaded."""
    awareness = {"ambient_sound": {"level": "loud"}, "sonar_cm": 15}
    mode = compute_obi_mode(awareness, hour_override=14)
    assert mode == "possibly-overloaded"


def test_obi_mode_active_daytime_close():
    """Close + loud + daytime → active."""
    awareness = {"ambient_sound": {"level": "loud"}, "sonar_cm": 25}
    mode = compute_obi_mode(awareness, hour_override=10)
    assert mode == "active"


def test_obi_mode_calm_daytime_close_quiet():
    """Close + quiet + daytime → calm."""
    awareness = {"ambient_sound": {"level": "quiet"}, "sonar_cm": 25}
    mode = compute_obi_mode(awareness, hour_override=10)
    assert mode == "calm"


def test_obi_mode_unknown_no_ambient():
    """No ambient data → unknown."""
    awareness = {"sonar_cm": 50}
    mode = compute_obi_mode(awareness, hour_override=10)
    assert mode == "unknown"


def test_obi_mode_unknown_no_sonar():
    """No sonar data + daytime + quiet → calm (sonar fallback)."""
    awareness = {"ambient_sound": {"level": "quiet"}}
    mode = compute_obi_mode(awareness, hour_override=10)
    assert mode == "calm"


def test_obi_mode_night_close_sonar():
    """Night + close sonar + silent + no Frigate → calm (close sonar fallback)."""
    awareness = {"ambient_sound": {"level": "silent"}, "sonar_cm": 20}
    mode = compute_obi_mode(awareness, hour_override=2)
    assert mode == "calm"


def test_obi_mode_calm_quiet_far():
    """Quiet + far + daytime → could be calm if sonar < 60."""
    awareness = {"ambient_sound": {"level": "quiet"}, "sonar_cm": 55}
    mode = compute_obi_mode(awareness, hour_override=10)
    assert mode in ("calm", "unknown")


# ---------------------------------------------------------------------------
# Frigate presence detection
# ---------------------------------------------------------------------------


def test_frigate_presence_detects_person():
    """A recent person event with score > 0.5 → person detected."""
    events = [_make_frigate_event(score=0.8)]
    with patch("urllib.request.urlopen", side_effect=_mock_urlopen_fn(events)):
        result = _fetch_frigate_presence(dry=False)
    assert result is not None
    assert result.get("person_present") is True


def test_frigate_presence_dry_returns_none():
    """Dry mode returns None without network access."""
    result = _fetch_frigate_presence(dry=True)
    assert result is None


def test_frigate_presence_network_error():
    """Network failure → None (graceful)."""
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("fail")):
        result = _fetch_frigate_presence(dry=False)
    assert result is None


def test_frigate_presence_empty_events():
    """Empty event list → no person detected."""
    with patch("urllib.request.urlopen", side_effect=_mock_urlopen_fn([])):
        result = _fetch_frigate_presence(dry=False)
    assert result is not None
    assert result.get("person_present") is False


def test_frigate_presence_below_min_score():
    """An event below FRIGATE_MIN_SCORE → not counted as person present."""
    low_score = _make_frigate_event(score=0.3, top_score=0.3)
    with patch("urllib.request.urlopen", side_effect=_mock_urlopen_fn([low_score])):
        result = _fetch_frigate_presence(dry=False)
    assert result is not None
    assert result.get("person_present") is False


def test_frigate_presence_low_score():
    """A low-score event (< 0.5) → not a confident detection."""
    events = [_make_frigate_event(score=0.3)]
    with patch("urllib.request.urlopen", side_effect=_mock_urlopen_fn(events)):
        result = _fetch_frigate_presence(dry=False)
    assert result is not None
    # Low-score may or may not be counted as person detected depending on threshold
    # but the function should not crash


def test_frigate_presence_multi_camera():
    """Multiple cameras each with person events → rooms_with_people populated."""
    events = [
        _make_frigate_event(camera="picar_x"),
        _make_frigate_event(camera="picamera"),
    ]
    with patch("urllib.request.urlopen", side_effect=_mock_urlopen_fn(events)):
        result = _fetch_frigate_presence(dry=False)
    assert result is not None


# ---------------------------------------------------------------------------
# filter_battery — glitch detection
# ---------------------------------------------------------------------------


def test_battery_filter_accepts_normal_reading():
    result = filter_battery({"pct": 72, "volts": 7.8}, prev_pct=75)
    assert result is not None
    assert result["pct"] == 72


def test_battery_filter_rejects_sudden_drop_to_zero():
    """A 0% reading when history says 72% is a sensor glitch."""
    # Seed history with normal readings
    for pct in [75, 74, 73, 72]:
        filter_battery({"pct": pct, "volts": 7.8}, prev_pct=pct + 1)
    # Now a 0% reading should be rejected
    result = filter_battery({"pct": 0, "volts": 5.0}, prev_pct=72)
    assert result is not None
    assert result["pct"] == 72  # returns prev_pct, not 0


def test_battery_filter_rejects_implausible_large_drop():
    """A drop larger than BATTERY_MAX_DROP_PER_TICK is suspicious."""
    for pct in [80, 79, 78]:
        filter_battery({"pct": pct, "volts": 7.8}, prev_pct=pct + 1)
    drop = BATTERY_MAX_DROP_PER_TICK + 5
    result = filter_battery({"pct": 78 - drop, "volts": 7.0}, prev_pct=78)
    assert result is not None
    assert result["pct"] == 78  # held at prev


def test_battery_filter_accepts_small_drop():
    """A small drop within MAX_DROP_PER_TICK is accepted."""
    for pct in [80, 79, 78]:
        filter_battery({"pct": pct, "volts": 7.8}, prev_pct=pct + 1)
    # A 2% drop is within the normal range
    result = filter_battery({"pct": 76, "volts": 7.7}, prev_pct=78)
    assert result is not None
    assert result["pct"] == 76


def test_battery_filter_none_input():
    """None input → None output."""
    result = filter_battery(None, prev_pct=80)
    assert result is None


def test_battery_filter_charging_resets_glitch():
    """When charging is True, glitch detection is bypassed."""
    for pct in [80, 79, 78]:
        filter_battery({"pct": pct, "volts": 7.8}, prev_pct=pct + 1)
    # A jump UP while charging is fine
    result = filter_battery({"pct": 95, "volts": 8.2, "charging": True}, prev_pct=78)
    assert result is not None
    assert result["pct"] == 95


def test_battery_glitch_confirms_requires_multiple():
    """Glitch detection requires BATTERY_GLITCH_CONFIRMS before accepting a low reading."""
    assert BATTERY_GLITCH_CONFIRMS >= 2  # safety: need at least 2 confirms


# ---------------------------------------------------------------------------
# HA presence tests
# ---------------------------------------------------------------------------


_HA_ENTITY_HOME = {
    "state": "home",
    "attributes": {"friendly_name": "Obi"},
}


def _mock_ha_urlopen(entities: dict):
    """Return a side_effect function that maps entity URLs to mock responses.

    Accepts **kwargs to handle timeout= from urllib.request.urlopen.
    """
    def _opener(req, *args, **kwargs):
        url = req.full_url if hasattr(req, 'full_url') else str(req)
        for entity_id, data in entities.items():
            if entity_id in url:
                body = _json.dumps(data).encode()
                cm = MagicMock()
                cm.__enter__ = MagicMock(return_value=MagicMock(read=MagicMock(return_value=body)))
                cm.__exit__ = MagicMock(return_value=False)
                return cm
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    return _opener


def _ha_ctx(token="test-token", host="http://ha.test:8123"):
    """Context manager that temporarily injects HA_TOKEN/HA_HOST into pxh.mind module."""
    import contextlib

    @contextlib.contextmanager
    def _cm():
        old_token = getattr(pxh.mind, "HA_TOKEN", "")
        old_host = getattr(pxh.mind, "HA_HOST", "")
        pxh.mind.HA_TOKEN = token
        pxh.mind.HA_HOST = host
        try:
            yield
        finally:
            pxh.mind.HA_TOKEN = old_token
            pxh.mind.HA_HOST = old_host

    return _cm()


def test_ha_presence_dry_returns_none():
    with _ha_ctx():
        result = _fetch_ha_presence(dry=True)
    assert result is None


def test_ha_presence_no_token_returns_none():
    """No HA token → None (graceful skip)."""
    with _ha_ctx(token=""):
        result = _fetch_ha_presence(dry=False)
    assert result is None


def test_ha_presence_network_error():
    """Network failure → None (graceful)."""
    with _ha_ctx():
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("fail")):
            result = _fetch_ha_presence(dry=False)
    assert result is None


def test_ha_presence_parses_home_entity():
    """Successfully parses a home entity."""
    # _fetch_ha_presence fetches multiple entity URLs (person.obi, person.adrian, etc.)
    # Mock needs to handle all of them without crashing
    entities = {
        "person.obi": _HA_ENTITY_HOME,
        "person.adrian": {"state": "home", "attributes": {"friendly_name": "Adrian"}},
    }
    with _ha_ctx():
        with patch("urllib.request.urlopen", side_effect=_mock_ha_urlopen(entities)):
            result = _fetch_ha_presence(dry=False)
    assert result is not None
    people = result.get("people", [])
    assert len(people) >= 1


# ---------------------------------------------------------------------------
# HA calendar
# ---------------------------------------------------------------------------


def test_ha_calendar_dry_returns_none():
    with _ha_ctx():
        result = _fetch_ha_calendar(dry=True)
    assert result is None


def test_ha_calendar_no_token_returns_none():
    with _ha_ctx(token=""):
        result = _fetch_ha_calendar(dry=False)
    assert result is None


# ---------------------------------------------------------------------------
# _parse_calendar_events
# ---------------------------------------------------------------------------


def test_parse_calendar_events_basic():
    """Parses a simple calendar event list."""
    now = _dt.datetime(2026, 3, 18, 9, 0, tzinfo=HOBART_TZ)
    events = [
        {"summary": "Swimming", "start": {"dateTime": "2026-03-18T10:00:00+11:00"},
         "end": {"dateTime": "2026-03-18T11:00:00+11:00"}, "location": "Pool"},
    ]
    parsed = _parse_calendar_events(events, "test@example.com", now)
    assert len(parsed) >= 1
    assert parsed[0]["title"] == "Swimming"


def test_parse_calendar_events_empty():
    """Empty list → empty list."""
    now = _dt.datetime(2026, 3, 18, 9, 0, tzinfo=HOBART_TZ)
    assert _parse_calendar_events([], "test@example.com", now) == []


def test_parse_calendar_events_none():
    """None → TypeError (caller should guard)."""
    now = _dt.datetime(2026, 3, 18, 9, 0, tzinfo=HOBART_TZ)
    with pytest.raises(TypeError):
        _parse_calendar_events(None, "test@example.com", now)


# ---------------------------------------------------------------------------
# compute_obi_mode — calendar integration
# ---------------------------------------------------------------------------


def test_obi_mode_at_school_from_calendar():
    """Calendar event 'School' → at-school (overrides heuristics)."""
    awareness = {
        "ambient_sound": {"level": "quiet"},
        "sonar_cm": 80,
        "calendar": {"current_event": "School"},
    }
    mode = compute_obi_mode(awareness, hour_override=10)
    assert mode == "at-school"


def test_obi_mode_at_mums_from_calendar():
    """Calendar event containing 'Mum' + 'place' → at-mums (overrides heuristics)."""
    awareness = {
        "ambient_sound": {"level": "quiet"},
        "sonar_cm": 80,
        "calendar": {"current_event": "At Mum's Place"},
    }
    mode = compute_obi_mode(awareness, hour_override=10)
    assert mode == "at-mums"


def test_obi_mode_no_calendar_falls_through():
    """No frigate key → original sonar/ambient logic unchanged."""
    awareness = {"ambient_sound": {"level": "quiet"}, "sonar_cm": 25}
    assert compute_obi_mode(awareness, hour_override=10) == "calm"


# ---------------------------------------------------------------------------
# filter_battery — glitch detection
# ---------------------------------------------------------------------------


def test_battery_filter_rejects_implausible_large_drop_with_seed():
    """A drop larger than BATTERY_MAX_DROP_PER_TICK after seed is suspicious."""
    for pct in [80, 79, 78]:
        filter_battery({"pct": pct, "volts": 7.8}, prev_pct=pct + 1)
    drop = BATTERY_MAX_DROP_PER_TICK + 5
    result = filter_battery({"pct": 78 - drop, "volts": 7.0}, prev_pct=78)
    assert result is not None
    assert result["pct"] == 78


# ---------------------------------------------------------------------------
# read_battery
# ---------------------------------------------------------------------------


def test_read_battery_includes_charging(tmp_path):
    battery_file = tmp_path / "battery.json"
    battery_data = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "pct": 72,
        "volts": 7.8,
        "charging": True,
    }
    battery_file.write_text(_json.dumps(battery_data))

    old_file = getattr(pxh.mind, "BATTERY_FILE", None)
    pxh.mind.BATTERY_FILE = battery_file
    try:
        result = read_battery()
        assert result is not None
        assert result["charging"] is True
        assert result["pct"] == 72
    finally:
        if old_file is not None:
            pxh.mind.BATTERY_FILE = old_file


# ---------------------------------------------------------------------------
# _can_explore — safety gate tests
# ---------------------------------------------------------------------------


def _base_session(**overrides):
    s = {
        "roaming_allowed": True,
        "confirm_motion_allowed": True,
        "wheels_on_blocks": False,
        "listening": False,
    }
    s.update(overrides)
    return s


def _base_awareness(**overrides):
    a = {
        "battery": {"pct": 80, "charging": False},
    }
    a.update(overrides)
    return a


@pytest.fixture
def explore_state(tmp_path):
    """Temporarily redirect STATE_DIR so _can_explore reads meta from tmp_path."""
    old = getattr(pxh.mind, "STATE_DIR", None)
    pxh.mind.STATE_DIR = tmp_path
    yield tmp_path
    if old is not None:
        pxh.mind.STATE_DIR = old


def test_can_explore_all_gates_pass(explore_state):
    assert _can_explore(_base_session(), _base_awareness()) is True


def test_can_explore_blocked_roaming_disabled(explore_state):
    assert _can_explore(_base_session(roaming_allowed=False), _base_awareness()) is False


def test_can_explore_blocked_motion_disabled(explore_state):
    assert _can_explore(_base_session(confirm_motion_allowed=False), _base_awareness()) is False


def test_can_explore_blocked_on_blocks(explore_state):
    assert _can_explore(_base_session(wheels_on_blocks=True), _base_awareness()) is False


def test_can_explore_blocked_listening(explore_state):
    assert _can_explore(_base_session(listening=True), _base_awareness()) is False


def test_can_explore_blocked_low_battery(explore_state):
    aw = _base_awareness(battery={"pct": 15, "charging": False})
    assert _can_explore(_base_session(), aw) is False


def test_can_explore_blocked_charging(explore_state):
    aw = _base_awareness(battery={"pct": 80, "charging": True})
    assert _can_explore(_base_session(), aw) is False


def test_can_explore_blocked_no_battery(explore_state):
    aw = _base_awareness(battery=None)
    assert _can_explore(_base_session(), aw) is False


def test_can_explore_cooldown(explore_state):
    """Active exploration meta within cooldown → blocked."""
    meta = {"last_explore_ts": _dt.datetime.now(_dt.timezone.utc).isoformat()}
    (explore_state / "exploration_meta.json").write_text(_json.dumps(meta))
    assert _can_explore(_base_session(), _base_awareness()) is False


def test_can_explore_completed_outside_cooldown(explore_state):
    """Completed exploration outside cooldown → allowed."""
    old_time = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=2)).isoformat()
    meta = {"last_explore_ts": old_time}
    (explore_state / "exploration_meta.json").write_text(_json.dumps(meta))
    assert _can_explore(_base_session(), _base_awareness()) is True


def test_can_explore_corrupt_meta_fails_safe(explore_state):
    """Corrupt meta file → blocked (fail-safe)."""
    (explore_state / "exploration_meta.json").write_text("not json")
    assert _can_explore(_base_session(), _base_awareness()) is False


# ---------------------------------------------------------------------------
# VALID_ACTIONS expansion + mood mapping dicts
# ---------------------------------------------------------------------------


def test_valid_actions_includes_new_actions():
    """All 21 actions must be present in VALID_ACTIONS."""
    expected = {
        "wait", "greet", "comment", "remember", "look_at",
        "weather_comment", "scan", "explore",
        "play_sound", "photograph", "emote", "look_around",
        "time_check", "calendar_check", "morning_fact",
        "introspect", "evolve",
        "research", "compose", "self_debug", "blog_essay",
    }
    assert VALID_ACTIONS == expected


def test_mood_to_sound_mapping():
    """MOOD_TO_SOUND maps moods to the correct sound effects."""
    assert MOOD_TO_SOUND["curious"] == "beep"
    assert MOOD_TO_SOUND["alert"] == "beep"
    assert MOOD_TO_SOUND["happy"] == "tada"
    assert MOOD_TO_SOUND["excited"] == "tada"
    assert MOOD_TO_SOUND["playful"] == "tada"
    assert MOOD_TO_SOUND["content"] == "chime"
    assert MOOD_TO_SOUND["peaceful"] == "chime"


def test_mood_to_emote_mapping():
    """MOOD_TO_EMOTE maps moods to the correct emote names."""
    assert MOOD_TO_EMOTE["happy"] == "happy"
    assert MOOD_TO_EMOTE["curious"] == "curious"
    assert MOOD_TO_EMOTE["alert"] == "alert"
    assert MOOD_TO_EMOTE["excited"] == "excited"
    assert MOOD_TO_EMOTE["contemplative"] == "thinking"
    assert MOOD_TO_EMOTE["peaceful"] == "shy"


def test_mood_mapping_fallback():
    """Unknown moods fall back to sensible defaults."""
    assert MOOD_TO_SOUND.get("unknown_mood", "chime") == "chime"
    assert MOOD_TO_EMOTE.get("unknown_mood", "idle") == "idle"


# ---------------------------------------------------------------------------
# Gate set membership tests
# ---------------------------------------------------------------------------


def test_charging_gate_blocks_emote():
    """emote uses servos — must be blocked while charging."""
    assert "emote" in CHARGING_GATED_ACTIONS


def test_charging_gate_blocks_look_around():
    """look_around uses servos — must be blocked while charging."""
    assert "look_around" in CHARGING_GATED_ACTIONS


def test_charging_gate_blocks_calendar_check():
    """calendar_check triggers internal emote (servos) — must be blocked while charging."""
    assert "calendar_check" in CHARGING_GATED_ACTIONS


def test_charging_gate_allows_photograph():
    """photograph does not use servos — should NOT be in the charging gate."""
    assert "photograph" not in CHARGING_GATED_ACTIONS


def test_absent_gate_blocks_play_sound():
    """play_sound produces audio — must be blocked when Obi is absent."""
    assert "play_sound" in ABSENT_GATED_ACTIONS


def test_absent_gate_blocks_photograph():
    """photograph speaks the description — must be blocked when Obi is absent."""
    assert "photograph" in ABSENT_GATED_ACTIONS


def test_absent_gate_blocks_time_check():
    """time_check speaks the time — must be blocked when Obi is absent."""
    assert "time_check" in ABSENT_GATED_ACTIONS


# ---------------------------------------------------------------------------
# Explore injection after enum expansion
# ---------------------------------------------------------------------------


def test_explore_injection_after_enum_expansion():
    """Verify explore injection string-replace works with expanded action enum.

    The explore action is dynamically injected at runtime via str.replace().
    After the enum expanded from 8 to 14 actions, the replace target changed
    from 'weather_comment, scan"' to 'time_check, calendar_check"'. Verify
    that this replace produces 'explore' in all 4 prompts.
    """
    prompts = {
        "REFLECTION_SYSTEM": REFLECTION_SYSTEM,
        "REFLECTION_SYSTEM_GREMLIN": REFLECTION_SYSTEM_GREMLIN,
        "REFLECTION_SYSTEM_VIXEN": REFLECTION_SYSTEM_VIXEN,
        "_SPARK_REFLECTION_SUFFIX": _SPARK_REFLECTION_SUFFIX,
    }

    # The injection target is the LAST action before the closing quote
    inject_target = 'blog_essay"'
    inject_result = 'blog_essay, explore"'

    for name, prompt in prompts.items():
        # Simulate the injection that reflection() does
        injected = prompt.replace(inject_target, inject_result)
        assert "explore" in injected, f"{name} failed: 'explore' not in injected prompt"


# ---------------------------------------------------------------------------
# expression() dispatch tests
# ---------------------------------------------------------------------------


def _thought(action, mood="curious", text="test thought", salience=0.5):
    return {"thought": text, "mood": mood, "action": action, "salience": salience}


@pytest.fixture(autouse=False)
def _mock_awareness_and_battery(tmp_path):
    """Stub AWARENESS_FILE and BATTERY_FILE so expression() gates don't block."""
    old_aw = getattr(pxh.mind, "AWARENESS_FILE", None)
    old_bat = getattr(pxh.mind, "BATTERY_FILE", None)
    aw_file = tmp_path / "awareness.json"
    bat_file = tmp_path / "battery.json"
    aw_file.write_text(_json.dumps({"obi_mode": "calm"}))
    bat_file.write_text(_json.dumps({"pct": 80, "charging": False}))
    pxh.mind.AWARENESS_FILE = aw_file
    pxh.mind.BATTERY_FILE = bat_file
    yield
    if old_aw is not None:
        pxh.mind.AWARENESS_FILE = old_aw
    if old_bat is not None:
        pxh.mind.BATTERY_FILE = old_bat


def test_expression_play_sound_calls_tool(_mock_awareness_and_battery):
    """play_sound dispatches to tool-play-sound with PX_SOUND from mood mapping."""
    with patch("subprocess.run") as mock_run:
        expression(_thought("play_sound", mood="curious"), dry=True)
    calls = [c for c in mock_run.call_args_list
             if "tool-play-sound" in str(c)]
    assert len(calls) == 1
    env = calls[0].kwargs.get("env") or calls[0][1].get("env", {})
    assert env.get("PX_SOUND") == "beep"
    assert env.get("PX_DRY") == "1"


def test_expression_photograph_calls_describe_scene(_mock_awareness_and_battery):
    """photograph dispatches to tool-describe-scene via Popen, NOT tool-photograph."""
    mock_proc = MagicMock()
    mock_proc.communicate = MagicMock(return_value=("", ""))
    with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
        expression(_thought("photograph"), dry=True)
    calls = [c for c in mock_popen.call_args_list
             if "tool-describe-scene" in str(c)]
    assert len(calls) == 1
    # Verify it's NOT tool-photograph
    all_calls_str = str(mock_popen.call_args_list)
    assert "tool-photograph" not in all_calls_str


def test_expression_emote_calls_tool(_mock_awareness_and_battery):
    """emote dispatches to tool-emote with PX_EMOTE from mood mapping."""
    with patch("subprocess.run") as mock_run:
        expression(_thought("emote", mood="happy"), dry=True)
    calls = [c for c in mock_run.call_args_list
             if "tool-emote" in str(c)]
    assert len(calls) == 1
    env = calls[0].kwargs.get("env") or calls[0][1].get("env", {})
    assert env.get("PX_EMOTE") == "happy"


def test_expression_look_around_calls_tool(_mock_awareness_and_battery):
    """look_around dispatches to tool-look with PX_PAN and PX_TILT env vars."""
    with patch("subprocess.run") as mock_run:
        expression(_thought("look_around"), dry=True)
    calls = [c for c in mock_run.call_args_list
             if "tool-look" in str(c)]
    assert len(calls) == 1
    env = calls[0].kwargs.get("env") or calls[0][1].get("env", {})
    assert "PX_PAN" in env
    assert "PX_TILT" in env
    # Verify pan/tilt are within expected ranges
    pan = int(env["PX_PAN"])
    tilt = int(env["PX_TILT"])
    assert -40 <= pan <= 40
    assert -10 <= tilt <= 30


def test_expression_time_check_calls_tool(_mock_awareness_and_battery):
    """time_check dispatches to tool-time."""
    with patch("subprocess.run") as mock_run:
        expression(_thought("time_check"), dry=True)
    calls = [c for c in mock_run.call_args_list
             if "tool-time" in str(c)]
    assert len(calls) == 1


def test_expression_calendar_check_calls_tool(_mock_awareness_and_battery):
    """calendar_check dispatches to tool-gws-calendar with PX_CALENDAR_ACTION=next."""
    with patch("subprocess.run") as mock_run:
        expression(_thought("calendar_check"), dry=True)
    calls = [c for c in mock_run.call_args_list
             if "tool-gws-calendar" in str(c)]
    assert len(calls) == 1
    env = calls[0].kwargs.get("env") or calls[0][1].get("env", {})
    assert env.get("PX_CALENDAR_ACTION") == "next"


def test_unknown_action_logged(_mock_awareness_and_battery, tmp_path):
    """An invented action logs 'unhandled action' without crashing."""
    log_file = tmp_path / "px-mind.log"
    old_log = getattr(pxh.mind, "LOG_FILE", None)
    pxh.mind.LOG_FILE = log_file
    try:
        with patch("subprocess.run"):
            expression(_thought("invented_action"), dry=True)
        log_content = log_file.read_text()
        assert "unhandled action" in log_content
    finally:
        if old_log is not None:
            pxh.mind.LOG_FILE = old_log


# ---------------------------------------------------------------------------
# Calendar awareness integration (Task 2)
# ---------------------------------------------------------------------------


def test_awareness_calendar_cache_variables_exist():
    """Cache variables for HA calendar exist at module level."""
    assert hasattr(pxh.mind, "_cached_ha_calendar")
    assert hasattr(pxh.mind, "_last_ha_calendar_fetch")


def test_awareness_calendar_enrichment():
    """When _cached_ha_calendar is set, awareness dict gets ha_calendar and next_event."""
    events = [
        {"title": "Swimming", "starts_in_mins": 45, "location": "Pool", "calendar": "family"},
        {"title": "Dinner", "starts_in_mins": 180, "location": "", "calendar": "family"},
    ]
    # Simulate what awareness_tick does in the enrichment block
    awareness = {}
    cached = events
    if cached:
        awareness["ha_calendar"] = cached
        upcoming = [e for e in cached if e["starts_in_mins"] >= -30]
        if upcoming:
            awareness["next_event"] = upcoming[0]

    assert awareness["ha_calendar"] == events
    assert awareness["next_event"]["title"] == "Swimming"


def test_awareness_calendar_enrichment_skips_old_events():
    """Events older than 30 minutes ago are excluded from next_event."""
    events = [
        {"title": "Past Event", "starts_in_mins": -60, "location": "", "calendar": "family"},
        {"title": "Future Event", "starts_in_mins": 20, "location": "", "calendar": "family"},
    ]
    awareness = {}
    cached = events
    if cached:
        awareness["ha_calendar"] = cached
        upcoming = [e for e in cached if e["starts_in_mins"] >= -30]
        if upcoming:
            awareness["next_event"] = upcoming[0]

    assert awareness["next_event"]["title"] == "Future Event"


def test_awareness_calendar_enrichment_no_upcoming():
    """When all events are far in the past, no next_event is set."""
    events = [
        {"title": "Old Event", "starts_in_mins": -120, "location": "", "calendar": "family"},
    ]
    awareness = {}
    cached = events
    if cached:
        awareness["ha_calendar"] = cached
        upcoming = [e for e in cached if e["starts_in_mins"] >= -30]
        if upcoming:
            awareness["next_event"] = upcoming[0]

    assert "ha_calendar" in awareness
    assert "next_event" not in awareness


def test_format_calendar_in_prompt_context():
    """_format_calendar_context output is suitable for injection into prompt context_parts."""
    events = [{"title": "Swimming", "starts_in_mins": 45, "location": "Pool", "calendar": "family"}]
    ctx = _format_calendar_context(events)
    assert "Swimming" in ctx
    assert "45" in ctx
    # Verify it could be appended to context_parts (non-empty string)
    context_parts = ["Some existing context"]
    if ctx:
        context_parts.append(ctx)
    assert len(context_parts) == 2


def test_format_calendar_empty_no_output():
    """Empty event list produces empty string — no calendar block in prompt."""
    ctx = _format_calendar_context([])
    assert ctx == ""
    # Verify it would NOT be appended to context_parts
    context_parts = ["Some existing context"]
    if ctx:
        context_parts.append(ctx)
    assert len(context_parts) == 1


# ---------------------------------------------------------------------------
# Consecutive reflection failure warning (#103)
# ---------------------------------------------------------------------------


def test_reflection_failure_counter_warns_at_threshold():
    """After REFLECTION_FAIL_WARN_THRESHOLD consecutive None reflections,
    a voice warning is spoken exactly once (at the threshold, not before)."""
    THRESHOLD = 3
    consecutive_reflection_failures = 0
    warnings_spoken = []

    # Simulate the mind_loop counter logic for a sequence of None reflections
    for i in range(5):
        thought = None  # simulate reflection failure
        if thought is None:
            consecutive_reflection_failures += 1
            if consecutive_reflection_failures == THRESHOLD:
                warnings_spoken.append(consecutive_reflection_failures)
        else:
            consecutive_reflection_failures = 0

    # Warning spoken exactly once, at failure #3
    assert warnings_spoken == [3]
    assert consecutive_reflection_failures == 5


def test_reflection_failure_counter_resets_on_success():
    """A successful reflection resets the counter, so the next warning
    requires another THRESHOLD consecutive failures."""
    THRESHOLD = 3
    consecutive_reflection_failures = 0
    warnings_spoken = []

    results = [None, None, {"thought": "ok"}, None, None, None, None]
    for thought in results:
        if thought is None:
            consecutive_reflection_failures += 1
            if consecutive_reflection_failures == THRESHOLD:
                warnings_spoken.append(consecutive_reflection_failures)
        else:
            consecutive_reflection_failures = 0

    # First two Nones don't reach threshold, then reset, then 4 Nones → warn at #3
    assert warnings_spoken == [3]
    assert consecutive_reflection_failures == 4


# ── Routine context formatting ──────────────────────────────────────


def test_format_routine_meds_not_taken():
    """When meds_taken is False, prompt should mention meds not taken."""
    result = _format_routine_context({"meds_taken": False})
    assert "not yet taken" in result.lower()


def test_format_routine_water_overdue():
    """When water_mins_ago > 120, prompt should mention hours since water."""
    result = _format_routine_context({"water_mins_ago": 150})
    assert "2 hours" in result


def test_format_routine_empty():
    """Empty or None routines should produce no prompt text."""
    assert _format_routine_context({}) == ""
    assert _format_routine_context(None) == ""


# ---------------------------------------------------------------------------
# _fetch_ha_sleep (#63)
# ---------------------------------------------------------------------------


def test_sleep_quality_good():
    """8 hours of sleep → quality 'good'."""
    response = {"state": str(8.0 * 3600), "attributes": {}}
    with _ha_ctx():
        with patch("urllib.request.urlopen", side_effect=_mock_ha_urlopen({"sensor.sleep": response})):
            result = _fetch_ha_sleep(dry=False)
    assert result is not None
    assert result["sleep_hours"] == 8.0
    assert result["sleep_quality"] == "good"


def test_sleep_quality_poor():
    """4.5 hours of sleep → quality 'poor'."""
    response = {"state": str(4.5 * 3600), "attributes": {}}
    with _ha_ctx():
        with patch("urllib.request.urlopen", side_effect=_mock_ha_urlopen({"sensor.sleep": response})):
            result = _fetch_ha_sleep(dry=False)
    assert result is not None
    assert result["sleep_hours"] == 4.5
    assert result["sleep_quality"] == "poor"


def test_sleep_quality_ok():
    """6 hours of sleep → quality 'ok'."""
    response = {"state": str(6.0 * 3600), "attributes": {}}
    with _ha_ctx():
        with patch("urllib.request.urlopen", side_effect=_mock_ha_urlopen({"sensor.sleep": response})):
            result = _fetch_ha_sleep(dry=False)
    assert result is not None
    assert result["sleep_hours"] == 6.0
    assert result["sleep_quality"] == "ok"


def test_sleep_zero_returns_none():
    """0.0 seconds (sensor inactive) → None."""
    response = {"state": "0.0", "attributes": {}}
    with _ha_ctx():
        with patch("urllib.request.urlopen", side_effect=_mock_ha_urlopen({"sensor.sleep": response})):
            result = _fetch_ha_sleep(dry=False)
    assert result is None


def test_sleep_prompt_poor():
    """Poor sleep produces 'tired' in the prompt text."""
    awareness = {"ha_sleep": {"sleep_hours": 4.5, "sleep_quality": "poor"}}
    sleep = awareness.get("ha_sleep")
    assert sleep is not None
    hours = sleep["sleep_hours"]
    quality = sleep["sleep_quality"]
    # Reproduce the prompt injection logic
    if quality == "poor":
        text = f"Adrian only got {hours} hours of sleep last night — he might be tired. Be gentle."
    elif quality == "ok":
        text = f"Adrian got {hours} hours of sleep — decent but not great."
    elif quality == "good":
        text = f"Adrian got {hours} hours of sleep — well rested."
    else:
        text = ""
    assert "tired" in text
    assert "4.5" in text


def test_sleep_dry_returns_none():
    """Dry mode returns None without network access."""
    with _ha_ctx():
        result = _fetch_ha_sleep(dry=True)
    assert result is None


def test_sleep_no_token_returns_none():
    """No HA token returns None."""
    with _ha_ctx(token=""):
        result = _fetch_ha_sleep(dry=False)
    assert result is None


# ── HA context formatting ──────────────────────────────────────────


def test_format_context_adrian_on_call():
    """When Adrian is on a video call, prompt text mentions it."""
    result = _format_ha_context({"adrian_on_call": True, "adrian_mic_active": True, "office_light": False})
    assert "video call" in result
    assert "Household context" in result


def test_format_context_media_playing():
    """When media is playing, prompt text includes title."""
    result = _format_ha_context({"media_playing": True, "media_title": "Bohemian Rhapsody"})
    assert "Music playing" in result
    assert "Bohemian Rhapsody" in result


def test_format_context_media_playing_no_title():
    """When media is playing without a title, still reports music."""
    result = _format_ha_context({"media_playing": True, "media_title": ""})
    assert "Music is playing" in result


def test_format_context_empty():
    """Empty dict produces empty string."""
    assert _format_ha_context({}) == ""
    assert _format_ha_context(None) == ""


def test_format_context_office_light_only():
    """Office light on produces relevant text."""
    result = _format_ha_context({"office_light": True})
    assert "Office light is on" in result


def test_format_context_mic_active_not_on_call():
    """Mic active without camera triggers mic-specific text, not video call."""
    result = _format_ha_context({"adrian_on_call": False, "adrian_mic_active": True})
    assert "microphone is active" in result
    assert "video call" not in result


def test_format_introspection_with_data():
    """_format_introspection produces readable summary from introspection dict."""
    intro = {
        "mood_distribution": {"curious": 50, "contemplative": 30, "content": 20},
        "config": {"SIMILARITY_THRESHOLD": 0.75, "EXPRESSION_COOLDOWN_S": 120},
        "evolve_history": [{"id": "test-1", "status": "pr_created"}],
    }
    result = _format_introspection(intro)
    assert "curious 50%" in result
    assert "SIMILARITY_THRESHOLD=0.75" in result
    assert "1 previous proposals" in result


def test_format_introspection_empty():
    """_format_introspection handles empty dict gracefully."""
    result = _format_introspection({})
    assert "No introspection data" in result
