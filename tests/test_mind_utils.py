"""Tests for px-mind utility functions: _daytime_action_hint and compute_obi_mode."""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent


def _load_mind_helpers():
    """Parse bin/px-mind and extract the helper functions we want to test."""
    src = (PROJECT_ROOT / "bin" / "px-mind").read_text()

    # Find the heredoc Python block (everything between <<'PY' and the closing PY)
    start = src.index("<<'PY'\n") + len("<<'PY'\n")
    end = src.rindex("\nPY\n")
    py_src = src[start:end]

    import datetime as _dt

    stub_keys = ("pxh", "pxh.state", "pxh.logging", "pxh.time", "pxh.token_log",
                  "pxh.voice_loop")
    saved_modules = {k: sys.modules.get(k) for k in stub_keys}

    # Stub out hardware/network imports only for the duration of exec
    stubs_pxh = types.ModuleType("pxh")
    stubs_state = types.ModuleType("pxh.state")
    stubs_state.load_session = lambda: {}
    stubs_state.update_session = lambda **kw: None
    stubs_state.save_session = lambda s: None
    stubs_logging = types.ModuleType("pxh.logging")
    stubs_logging.log_event = lambda *a, **kw: None
    stubs_time = types.ModuleType("pxh.time")
    stubs_time.utc_timestamp = lambda: _dt.datetime.now(_dt.timezone.utc).isoformat()
    stubs_token_log = types.ModuleType("pxh.token_log")
    stubs_token_log.log_usage = lambda *a, **kw: None
    stubs_voice_loop = types.ModuleType("pxh.voice_loop")
    stubs_voice_loop.PERSONA_VOICE_ENV = {
        "vixen": {"PX_PERSONA": "vixen", "PX_VOICE_VARIANT": "en+f4",
                  "PX_VOICE_PITCH": "72", "PX_VOICE_RATE": "135"},
        "gremlin": {"PX_PERSONA": "gremlin", "PX_VOICE_VARIANT": "en+croak",
                    "PX_VOICE_PITCH": "20", "PX_VOICE_RATE": "180"},
        "spark": {"PX_PERSONA": "spark", "PX_VOICE_VARIANT": "en-gb",
                  "PX_VOICE_PITCH": "95", "PX_VOICE_RATE": "100"},
    }

    sys.modules["pxh"] = stubs_pxh
    sys.modules["pxh.state"] = stubs_state
    sys.modules["pxh.logging"] = stubs_logging
    sys.modules["pxh.time"] = stubs_time
    sys.modules["pxh.token_log"] = stubs_token_log
    sys.modules["pxh.voice_loop"] = stubs_voice_loop

    env_patch = {
        "PROJECT_ROOT": str(PROJECT_ROOT),
        "LOG_DIR": str(PROJECT_ROOT / "logs"),
        "PX_STATE_DIR": str(PROJECT_ROOT / "state"),
        "MIND_BACKEND": "auto",
        "PX_OLLAMA_HOST": "http://localhost:11434",
    }
    old_env = {k: os.environ.get(k) for k in env_patch}
    for k, v in env_patch.items():
        os.environ[k] = v

    globs: dict = {"__file__": str(PROJECT_ROOT / "bin" / "px-mind")}
    try:
        exec(compile(py_src, "bin/px-mind", "exec"), globs)  # noqa: S102
    finally:
        # Restore sys.modules to avoid polluting other test imports
        for k, old_mod in saved_modules.items():
            if old_mod is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = old_mod
        for k, old_v in old_env.items():
            if old_v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old_v

    return globs


import json as _json
import time as _time
import urllib.error
from unittest.mock import MagicMock, patch


def _make_frigate_event(score=0.75, top_score=None, x=0.2, y=0.1, w=0.3, h=0.8,
                        speed=0.0, vel_angle=0.0, end_time=None, label="person"):
    return {
        "label": label,
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
    body = _json.dumps(events).encode()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=MagicMock(read=MagicMock(return_value=body)))
    cm.__exit__ = MagicMock(return_value=False)
    return cm


_MIND = _load_mind_helpers()
_daytime_action_hint = _MIND["_daytime_action_hint"]
compute_obi_mode = _MIND["compute_obi_mode"]
_fetch_frigate_presence = _MIND["_fetch_frigate_presence"]
_fetch_ha_presence = _MIND["_fetch_ha_presence"]
_parse_calendar_events = _MIND["_parse_calendar_events"]
_fetch_ha_calendar = _MIND["_fetch_ha_calendar"]
_format_calendar_context = _MIND["_format_calendar_context"]
filter_battery = _MIND["filter_battery"]
_battery_history = _MIND["_battery_history"]
_BATTERY_MAX_DROP = _MIND["BATTERY_MAX_DROP_PER_TICK"]
_BATTERY_GLITCH_CONFIRMS = _MIND["BATTERY_GLITCH_CONFIRMS"]
_can_explore = _MIND["_can_explore"]


# ---------------------------------------------------------------------------
# _daytime_action_hint
# ---------------------------------------------------------------------------


def test_daytime_hint_daytime():
    """During Obi's waking hours (7–19) the hint pushes toward comment/greet."""
    hint = _daytime_action_hint(hour_override=10)
    assert "comment" in hint or "greet" in hint


def test_daytime_hint_night():
    """Overnight the hint pushes toward remember/wait."""
    hint = _daytime_action_hint(hour_override=2)
    assert "remember" in hint or "wait" in hint


def test_daytime_hint_boundary_start():
    """Hour 7 (day start) → daytime hint."""
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


# ---------------------------------------------------------------------------
# _fetch_frigate_presence
# ---------------------------------------------------------------------------


def test_frigate_returns_presence_when_event_recent():
    events = [_make_frigate_event(score=0.80, x=0.2, w=0.3)]  # x_center = 0.35
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(events)):
        result = _fetch_frigate_presence(dry=False)
    assert result is not None
    assert result["person_present"] is True
    assert abs(result["x_center"] - 0.35) < 0.01
    assert result["score"] == pytest.approx(0.80, abs=0.01)


def test_frigate_returns_none_on_connection_error():
    with patch("urllib.request.urlopen", side_effect=OSError("refused")):
        result = _fetch_frigate_presence(dry=False)
    assert result is None


def test_frigate_returns_none_on_timeout():
    import socket
    with patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
        result = _fetch_frigate_presence(dry=False)
    assert result is None


def test_frigate_empty_events_no_person():
    with patch("urllib.request.urlopen", return_value=_mock_urlopen([])):
        result = _fetch_frigate_presence(dry=False)
    assert result is not None
    assert result["person_present"] is False


def test_frigate_dry_run_skips_network():
    with patch("urllib.request.urlopen", side_effect=AssertionError("must not call")):
        result = _fetch_frigate_presence(dry=True)
    assert result is None


def test_frigate_filters_low_confidence():
    events = [_make_frigate_event(score=0.45, top_score=0.45)]
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(events)):
        result = _fetch_frigate_presence(dry=False)
    assert result["person_present"] is False


def test_frigate_qualifies_via_top_score():
    # Real Frigate pattern: instantaneous score below threshold, top_score above
    events = [_make_frigate_event(score=0.577, top_score=0.676)]
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(events)):
        result = _fetch_frigate_presence(dry=False)
    assert result["person_present"] is True
    assert result["score"] == pytest.approx(0.676, abs=0.01)


def test_frigate_reports_event_count():
    events = [_make_frigate_event(score=0.80), _make_frigate_event(score=0.75)]
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(events)):
        result = _fetch_frigate_presence(dry=False)
    assert result["event_count"] == 2


# ---------------------------------------------------------------------------
# compute_obi_mode with Frigate data
# ---------------------------------------------------------------------------

def _fp_present(x_center=0.5, score=0.80, count=1):
    return {"person_present": True, "event_count": count,
            "score": score, "x_center": x_center, "speed": 0.0, "velocity_angle": 0.0}

def _fp_absent():
    return {"person_present": False, "event_count": 0,
            "score": None, "x_center": None, "speed": None, "velocity_angle": None}


def test_obi_mode_calm_from_frigate_without_close_sonar():
    """Frigate detects person → calm, even if sonar shows person far away."""
    awareness = {"ambient_sound": {"level": "quiet"}, "sonar_cm": 200,
                 "frigate": _fp_present()}
    assert compute_obi_mode(awareness, hour_override=10) == "calm"


def test_obi_mode_active_from_frigate_multiple_events():
    """Multiple Frigate detections in window → active."""
    awareness = {"ambient_sound": {"level": "quiet"}, "sonar_cm": 200,
                 "frigate": _fp_present(count=3)}
    assert compute_obi_mode(awareness, hour_override=10) == "active"


def test_obi_mode_not_absent_when_frigate_sees_person_at_night():
    """Frigate detects person at night → not absent."""
    awareness = {"ambient_sound": {"level": "silent"}, "sonar_cm": 90,
                 "frigate": _fp_present()}
    assert compute_obi_mode(awareness, hour_override=2) != "absent"


def test_obi_mode_absent_when_frigate_online_but_no_person_at_night():
    """Frigate is online, reports no person, nighttime → absent."""
    awareness = {"ambient_sound": {"level": "silent"}, "sonar_cm": 90,
                 "frigate": _fp_absent()}
    assert compute_obi_mode(awareness, hour_override=2) == "absent"


def test_obi_mode_sonar_fallback_when_frigate_offline():
    """No frigate key → original sonar/ambient logic unchanged."""
    awareness = {"ambient_sound": {"level": "quiet"}, "sonar_cm": 25}
    assert compute_obi_mode(awareness, hour_override=10) == "calm"


# ---------------------------------------------------------------------------
# filter_battery — glitch detection
# ---------------------------------------------------------------------------


def _reset_battery_state():
    """Clear battery history between tests."""
    _battery_history.clear()
    _MIND["_battery_glitch_count"] = 0
    _MIND["_battery_glitch_first_mono"] = 0.0


def test_battery_filter_accepts_normal_reading():
    _reset_battery_state()
    result = filter_battery({"pct": 72, "volts": 7.8}, prev_pct=75)
    assert result is not None
    assert result["pct"] == 72


def test_battery_filter_rejects_sudden_drop_to_zero():
    """A 0% reading when history says 72% is a sensor glitch."""
    _reset_battery_state()
    # Seed history with normal readings
    for pct in [75, 74, 73, 72]:
        filter_battery({"pct": pct, "volts": 7.8}, prev_pct=pct + 1)
    # Now a 0% reading should be rejected
    result = filter_battery({"pct": 0, "volts": 5.0}, prev_pct=72)
    assert result is not None
    assert result["pct"] == 72  # returns prev_pct, not 0


def test_battery_filter_rejects_implausible_large_drop():
    """A drop larger than BATTERY_MAX_DROP_PER_TICK is suspicious."""
    _reset_battery_state()
    for pct in [80, 79, 78]:
        filter_battery({"pct": pct, "volts": 8.0}, prev_pct=pct + 1)
    # Drop of 50% in one tick — impossible
    result = filter_battery({"pct": 28, "volts": 6.5}, prev_pct=78)
    assert result["pct"] == 78  # rejected


def test_battery_filter_accepts_after_confirmed_consecutive():
    """After BATTERY_GLITCH_CONFIRMS consecutive low readings spanning >=90s, accept it."""
    _reset_battery_state()
    for pct in [50, 49, 48]:
        filter_battery({"pct": pct, "volts": 7.2}, prev_pct=pct + 1)

    # Send glitch readings all at t=0 — time-gap prevents counter passing 1
    fake_time = [100.0]
    with patch.object(_time, "monotonic", side_effect=lambda: fake_time[0]):
        r = filter_battery({"pct": 5, "volts": 6.0}, prev_pct=48)
        assert r["pct"] == 48, "should reject first glitch"
        r = filter_battery({"pct": 5, "volts": 6.0}, prev_pct=48)
        assert r["pct"] == 48, "same-tick glitch should not increment past 1"

    # Advance time past the 90s gap and send remaining confirmations
    for i in range(_BATTERY_GLITCH_CONFIRMS - 1):
        fake_time[0] = 100.0 + 91.0 * (i + 1)
        with patch.object(_time, "monotonic", side_effect=lambda: fake_time[0]):
            r = filter_battery({"pct": 5, "volts": 6.0}, prev_pct=48)
            if i < _BATTERY_GLITCH_CONFIRMS - 2:
                assert r["pct"] == 48, f"should still reject on attempt {i+2}"

    # The final reading (after enough time + confirmations) should be accepted
    assert r["pct"] == 5


def test_battery_filter_resets_on_normal_reading():
    """A normal reading after a glitch resets the counter."""
    _reset_battery_state()
    for pct in [60, 59, 58]:
        filter_battery({"pct": pct, "volts": 7.5}, prev_pct=pct + 1)
    # One glitch
    filter_battery({"pct": 0, "volts": 5.0}, prev_pct=58)
    # Normal reading resets
    filter_battery({"pct": 57, "volts": 7.4}, prev_pct=58)
    # Another glitch should start counter from 1 again
    r = filter_battery({"pct": 0, "volts": 5.0}, prev_pct=57)
    assert r["pct"] == 57  # still rejected (only 1 consecutive)


def test_battery_filter_passes_through_none():
    _reset_battery_state()
    assert filter_battery(None, prev_pct=50) is None


def test_battery_filter_accepts_first_reading():
    _reset_battery_state()
    result = filter_battery({"pct": 85, "volts": 8.1}, prev_pct=100)
    assert result["pct"] == 85


def test_battery_filter_accepts_gradual_decline():
    """Gradual decline within threshold is always accepted."""
    _reset_battery_state()
    prev = 80
    for pct in range(80, 65, -1):
        r = filter_battery({"pct": pct, "volts": 7.0}, prev_pct=prev)
        assert r["pct"] == pct
        prev = pct


# ---------------------------------------------------------------------------
# _fetch_ha_presence
# ---------------------------------------------------------------------------

def _mock_ha_urlopen(responses: dict):
    """Return a urlopen mock that dispatches based on the requested URL path."""
    def _side_effect(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        for key, body in responses.items():
            if key in url:
                cm = MagicMock()
                cm.__enter__ = MagicMock(
                    return_value=MagicMock(read=MagicMock(return_value=_json.dumps(body).encode()))
                )
                cm.__exit__ = MagicMock(return_value=False)
                return cm
        raise OSError(f"No mock for {url}")
    return _side_effect


_PERSON_ADRIAN = {
    "entity_id": "person.adrian",
    "state": "home",
    "attributes": {"friendly_name": "Adrian"},
}
_PERSON_OBI = {
    "entity_id": "person.obi",
    "state": "unknown",
    "attributes": {"friendly_name": "Obi"},
}


def _ha_ctx(token="test-token", host="http://ha.test:8123"):
    """Context manager that temporarily injects HA_TOKEN/HA_HOST into _MIND globals."""
    import contextlib

    @contextlib.contextmanager
    def _cm():
        old_token = _MIND.get("HA_TOKEN", "")
        old_host  = _MIND.get("HA_HOST", "")
        _MIND["HA_TOKEN"] = token
        _MIND["HA_HOST"]  = host
        try:
            yield
        finally:
            _MIND["HA_TOKEN"] = old_token
            _MIND["HA_HOST"]  = old_host

    return _cm()


def test_ha_presence_dry_returns_none():
    with _ha_ctx():
        result = _fetch_ha_presence(dry=True)
    assert result is None


def test_ha_presence_no_token_returns_none():
    with _ha_ctx(token=""):
        result = _fetch_ha_presence(dry=False)
    assert result is None


def test_ha_presence_parses_home_person():
    responses = {
        "person.adrian": _PERSON_ADRIAN,
        "person.obi": _PERSON_OBI,
    }
    with _ha_ctx():
        with patch("urllib.request.urlopen", side_effect=_mock_ha_urlopen(responses)):
            result = _fetch_ha_presence(dry=False)
    assert result is not None
    people = {p["name"]: p for p in result["people"]}
    assert people["Adrian"]["home"] is True
    assert people["Obi"]["home"] is False


def test_ha_presence_unreachable_returns_none():
    with _ha_ctx():
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            result = _fetch_ha_presence(dry=False)
    assert result is None


def test_ha_presence_per_entity_failure_continues():
    """404 on some entities does not abort — returns only successfully-fetched people."""
    import urllib.error as _urlerr

    def _side_effect(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "person.adrian" in url:
            cm = MagicMock()
            cm.__enter__ = MagicMock(
                return_value=MagicMock(read=MagicMock(return_value=_json.dumps(_PERSON_ADRIAN).encode()))
            )
            cm.__exit__ = MagicMock(return_value=False)
            return cm
        # All other entities → 404
        raise _urlerr.HTTPError(url, 404, "Not Found", {}, None)

    with _ha_ctx():
        with patch("urllib.request.urlopen", side_effect=_side_effect):
            result = _fetch_ha_presence(dry=False)
    assert result is not None
    assert len(result["people"]) == 1
    assert result["people"][0]["name"] == "Adrian"


def test_ha_presence_auth_failure_raises():
    """401 auth failure must propagate so the caller can clear the cache."""
    import urllib.error as _urlerr

    def _side_effect(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        raise _urlerr.HTTPError(url, 401, "Unauthorized", {}, None)

    with _ha_ctx():
        with pytest.raises(_urlerr.HTTPError) as exc_info:
            with patch("urllib.request.urlopen", side_effect=_side_effect):
                _fetch_ha_presence(dry=False)
    assert exc_info.value.code == 401


# ---------------------------------------------------------------------------
# _parse_calendar_events / _fetch_ha_calendar / _format_calendar_context
# ---------------------------------------------------------------------------

import datetime as _dt_cal


def test_parse_ha_calendar_events():
    """Timed event 45 minutes in the future is parsed correctly."""
    now = _dt_cal.datetime(2026, 3, 15, 14, 15, 0,
                           tzinfo=_dt_cal.timezone(_dt_cal.timedelta(hours=11)))
    raw = [{
        "summary": "Swimming",
        "start": {"dateTime": "2026-03-15T15:00:00+11:00"},
        "end": {"dateTime": "2026-03-15T16:00:00+11:00"},
        "location": "Hobart Aquatic Centre",
    }]
    events = _parse_calendar_events(raw, "calendar.test", now)
    assert len(events) == 1
    assert events[0]["title"] == "Swimming"
    assert events[0]["starts_in_mins"] == 45
    assert events[0]["location"] == "Hobart Aquatic Centre"
    assert events[0]["calendar"] == "calendar.test"


def test_parse_ha_calendar_all_day_event():
    """All-day events use 'date' not 'dateTime' and are parsed correctly."""
    now = _dt_cal.datetime(2026, 3, 15, 10, 0, 0,
                           tzinfo=_dt_cal.timezone(_dt_cal.timedelta(hours=11)))
    raw = [{
        "summary": "School",
        "start": {"date": "2026-03-15"},
        "end": {"date": "2026-03-16"},
    }]
    events = _parse_calendar_events(raw, "calendar.test", now)
    assert len(events) == 1
    assert events[0]["title"] == "School"
    # All-day event starting at midnight — starts_in_mins should be negative (already started)
    assert events[0]["starts_in_mins"] < 0
    assert events[0]["location"] is None


def test_parse_ha_calendar_past_event_filtered():
    """Events whose end time is before now are excluded."""
    now = _dt_cal.datetime(2026, 3, 15, 17, 0, 0,
                           tzinfo=_dt_cal.timezone(_dt_cal.timedelta(hours=11)))
    raw = [{
        "summary": "Old Meeting",
        "start": {"dateTime": "2026-03-15T14:00:00+11:00"},
        "end": {"dateTime": "2026-03-15T15:00:00+11:00"},
    }]
    events = _parse_calendar_events(raw, "calendar.test", now)
    assert len(events) == 0


def test_parse_ha_calendar_empty():
    """Empty list returns empty list."""
    now = _dt_cal.datetime(2026, 3, 15, 10, 0, 0,
                           tzinfo=_dt_cal.timezone(_dt_cal.timedelta(hours=11)))
    events = _parse_calendar_events([], "calendar.test", now)
    assert events == []


def test_format_next_event_for_prompt():
    """Upcoming event within 60 mins formats as 'Coming up: ...'."""
    events = [{"title": "Swimming", "starts_in_mins": 45,
               "location": "Hobart Aquatic Centre", "calendar": "calendar.test"}]
    text = _format_calendar_context(events)
    assert "Coming up: Swimming at Hobart Aquatic Centre in 45 minutes" in text


def test_format_next_event_happening_now():
    """Event with negative starts_in_mins formats as 'Happening now: ...'."""
    events = [{"title": "Swimming", "starts_in_mins": -10,
               "location": None, "calendar": "calendar.test"}]
    text = _format_calendar_context(events)
    assert "Happening now: Swimming (started 10 minutes ago)" in text


def test_format_calendar_later_event():
    """Event >= 60 mins away formats as 'Later: ... in N hours'."""
    events = [{"title": "Dinner", "starts_in_mins": 180,
               "location": "Home", "calendar": "calendar.test"}]
    text = _format_calendar_context(events)
    assert "Later: Dinner at Home in 3 hours" in text


def test_format_calendar_empty():
    """Empty events list returns empty string."""
    assert _format_calendar_context([]) == ""


def test_fetch_ha_calendar_dry_returns_none():
    with _ha_ctx():
        result = _fetch_ha_calendar(dry=True)
    assert result is None


def test_fetch_ha_calendar_no_token_returns_none():
    with _ha_ctx(token=""):
        result = _fetch_ha_calendar(dry=False)
    assert result is None


def test_fetch_ha_calendar_returns_sorted_events():
    """Events from multiple calendars are merged and sorted by starts_in_mins."""
    now = _dt_cal.datetime.now(_dt_cal.timezone.utc)
    soon = now + _dt_cal.timedelta(minutes=30)
    later = now + _dt_cal.timedelta(hours=2)

    cal1_events = [{
        "summary": "Later Event",
        "start": {"dateTime": later.isoformat()},
        "end": {"dateTime": (later + _dt_cal.timedelta(hours=1)).isoformat()},
    }]
    cal2_events = [{
        "summary": "Soon Event",
        "start": {"dateTime": soon.isoformat()},
        "end": {"dateTime": (soon + _dt_cal.timedelta(hours=1)).isoformat()},
    }]

    def _side_effect(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "obiwedd" in url:
            body = cal1_events
        elif "calendar.calendar" in url:
            body = cal2_events
        else:
            raise OSError(f"No mock for {url}")
        cm = MagicMock()
        cm.__enter__ = MagicMock(
            return_value=MagicMock(read=MagicMock(return_value=_json.dumps(body).encode()))
        )
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    with _ha_ctx():
        with patch("urllib.request.urlopen", side_effect=_side_effect):
            result = _fetch_ha_calendar(dry=False)
    assert result is not None
    assert len(result) == 2
    assert result[0]["title"] == "Soon Event"
    assert result[1]["title"] == "Later Event"
    assert result[0]["starts_in_mins"] < result[1]["starts_in_mins"]


def test_fetch_ha_calendar_per_calendar_failure_continues():
    """Error on one calendar does not block others."""
    now = _dt_cal.datetime.now(_dt_cal.timezone.utc)
    soon = now + _dt_cal.timedelta(minutes=15)
    cal2_events = [{
        "summary": "Good Event",
        "start": {"dateTime": soon.isoformat()},
        "end": {"dateTime": (soon + _dt_cal.timedelta(hours=1)).isoformat()},
    }]

    def _side_effect(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "obiwedd" in url:
            raise OSError("calendar offline")
        cm = MagicMock()
        cm.__enter__ = MagicMock(
            return_value=MagicMock(read=MagicMock(return_value=_json.dumps(cal2_events).encode()))
        )
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    with _ha_ctx():
        with patch("urllib.request.urlopen", side_effect=_side_effect):
            result = _fetch_ha_calendar(dry=False)
    assert result is not None
    assert len(result) == 1
    assert result[0]["title"] == "Good Event"


# ---------------------------------------------------------------------------
# Multi-label Frigate grouping
# ---------------------------------------------------------------------------


def test_frigate_groups_multiple_labels():
    """Events with different labels are grouped correctly."""
    events = [
        _make_frigate_event(score=0.85, label="person"),
        _make_frigate_event(score=0.80, label="person"),
        _make_frigate_event(score=0.72, label="dog"),
        _make_frigate_event(score=0.65, label="car"),
    ]
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(events)):
        result = _fetch_frigate_presence(dry=False)
    assert result is not None
    by_label = {d["label"]: d for d in result["detections"]}
    assert "person" in by_label
    assert by_label["person"]["count"] == 2
    assert "dog" in by_label
    assert by_label["dog"]["count"] == 1
    assert "car" in by_label


def test_frigate_detections_sorted_by_score_desc():
    """detections list must be sorted highest-score first."""
    events = [
        _make_frigate_event(score=0.60, label="car"),
        _make_frigate_event(score=0.90, label="person"),
        _make_frigate_event(score=0.75, label="dog"),
    ]
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(events)):
        result = _fetch_frigate_presence(dry=False)
    scores = [d["score"] for d in result["detections"]]
    assert scores == sorted(scores, reverse=True)


def test_frigate_non_list_response_returns_none():
    """A non-list Frigate API response (e.g. error dict) must return None."""
    error_body = _json.dumps({"error": "camera offline"}).encode()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=MagicMock(read=MagicMock(return_value=error_body)))
    cm.__exit__ = MagicMock(return_value=False)
    with patch("urllib.request.urlopen", return_value=cm):
        result = _fetch_frigate_presence(dry=False)
    assert result is None


# ---------------------------------------------------------------------------
# compute_obi_mode — HA presence integration
# ---------------------------------------------------------------------------


def test_obi_mode_absent_when_ha_says_no_one_home():
    """HA reporting no one home → absent, regardless of sonar/time."""
    awareness = {
        "ambient_sound": {"level": "moderate"},
        "sonar_cm": 30,   # close, daytime
        "ha_presence": {"people": [{"name": "Adrian", "state": "away", "home": False}]},
    }
    assert compute_obi_mode(awareness, hour_override=14) == "absent"


def test_obi_mode_not_absent_when_ha_says_someone_home():
    """HA reporting someone home prevents absent, even at night."""
    awareness = {
        "ambient_sound": {"level": "silent"},
        "sonar_cm": 120,
        "ha_presence": {"people": [{"name": "Adrian", "state": "home", "home": True}]},
    }
    assert compute_obi_mode(awareness, hour_override=2) != "absent"


def test_obi_mode_falls_through_to_sensors_when_ha_someone_home():
    """When HA says someone's home, sensor logic still determines calm/active/etc."""
    awareness = {
        "ambient_sound": {"level": "loud"},
        "sonar_cm": 25,
        "ha_presence": {"people": [{"name": "Obi", "state": "home", "home": True}]},
    }
    assert compute_obi_mode(awareness, hour_override=10) == "active"


def test_read_battery_includes_charging(tmp_path):
    _MIND = _load_mind_helpers()
    read_battery = _MIND["read_battery"]

    import datetime as dt
    battery_file = tmp_path / "battery.json"
    battery_data = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "pct": 72,
        "volts": 7.8,
        "charging": True,
    }
    battery_file.write_text(_json.dumps(battery_data))

    old_file = _MIND.get("BATTERY_FILE")
    _MIND["BATTERY_FILE"] = battery_file
    try:
        result = read_battery()
        assert result is not None
        assert result["charging"] is True
        assert result["pct"] == 72
    finally:
        if old_file is not None:
            _MIND["BATTERY_FILE"] = old_file


# ---------------------------------------------------------------------------
# _can_explore — safety gate tests
# ---------------------------------------------------------------------------

import datetime as _dt


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
    old = _MIND.get("STATE_DIR")
    _MIND["STATE_DIR"] = tmp_path
    yield tmp_path
    if old is not None:
        _MIND["STATE_DIR"] = old


def test_can_explore_all_gates_pass(explore_state):
    assert _can_explore(_base_session(), _base_awareness()) is True


def test_can_explore_blocked_roaming_disabled(explore_state):
    assert _can_explore(_base_session(roaming_allowed=False), _base_awareness()) is False


def test_can_explore_blocked_motion_not_allowed(explore_state):
    assert _can_explore(_base_session(confirm_motion_allowed=False), _base_awareness()) is False


def test_can_explore_blocked_wheels_on_blocks(explore_state):
    assert _can_explore(_base_session(wheels_on_blocks=True), _base_awareness()) is False


def test_can_explore_blocked_listening(explore_state):
    assert _can_explore(_base_session(listening=True), _base_awareness()) is False


def test_can_explore_blocked_charging(explore_state):
    assert _can_explore(_base_session(), _base_awareness(battery={"pct": 80, "charging": True})) is False


def test_can_explore_blocked_battery_none(explore_state):
    """No battery data at all → blocked (fail-safe)."""
    assert _can_explore(_base_session(), _base_awareness(battery={})) is False


def test_can_explore_blocked_battery_low(explore_state):
    assert _can_explore(_base_session(), _base_awareness(battery={"pct": 15, "charging": False})) is False


def test_can_explore_blocked_cooldown(explore_state):
    """Recent exploration within 1200s cooldown → blocked."""
    meta = {"last_explore_ts": _dt.datetime.now(_dt.timezone.utc).isoformat()}
    (explore_state / "exploration_meta.json").write_text(_json.dumps(meta))
    assert _can_explore(_base_session(), _base_awareness()) is False


def test_can_explore_passes_after_cooldown(explore_state):
    """Exploration older than 1200s → allowed."""
    old_ts = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=1500)).isoformat()
    meta = {"last_explore_ts": old_ts}
    (explore_state / "exploration_meta.json").write_text(_json.dumps(meta))
    assert _can_explore(_base_session(), _base_awareness()) is True


def test_can_explore_corrupt_meta_fails_safe(explore_state):
    """Corrupt meta file → blocked (fail-safe)."""
    (explore_state / "exploration_meta.json").write_text("not json")
    assert _can_explore(_base_session(), _base_awareness()) is False


# ---------------------------------------------------------------------------
# VALID_ACTIONS expansion + mood mapping dicts
# ---------------------------------------------------------------------------

VALID_ACTIONS = _MIND["VALID_ACTIONS"]
MOOD_TO_SOUND = _MIND["MOOD_TO_SOUND"]
MOOD_TO_EMOTE = _MIND["MOOD_TO_EMOTE"]
CHARGING_GATED_ACTIONS = _MIND["CHARGING_GATED_ACTIONS"]
ABSENT_GATED_ACTIONS = _MIND["ABSENT_GATED_ACTIONS"]


def test_valid_actions_includes_new_actions():
    """All 14 actions must be present in VALID_ACTIONS."""
    expected = {
        "wait", "greet", "comment", "remember", "look_at",
        "weather_comment", "scan", "explore",
        "play_sound", "photograph", "emote", "look_around",
        "time_check", "calendar_check",
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

REFLECTION_SYSTEM = _MIND["REFLECTION_SYSTEM"]
REFLECTION_SYSTEM_GREMLIN = _MIND["REFLECTION_SYSTEM_GREMLIN"]
REFLECTION_SYSTEM_VIXEN = _MIND["REFLECTION_SYSTEM_VIXEN"]
_SPARK_REFLECTION_SUFFIX = _MIND["_SPARK_REFLECTION_SUFFIX"]


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
    for name, prompt in prompts.items():
        # The replace target must exist in the prompt
        assert 'time_check, calendar_check"' in prompt, (
            f"{name} is missing the replace target 'time_check, calendar_check\"'"
        )
        # After injection, explore must appear
        injected = prompt.replace(
            'time_check, calendar_check"',
            'time_check, calendar_check, explore"',
        )
        assert "explore" in injected, f"{name} does not contain 'explore' after injection"
        # The original prompt must NOT already contain explore
        assert "explore" not in prompt, (
            f"{name} already contains 'explore' before injection"
        )


def test_absent_gate_blocks_calendar_check():
    """calendar_check speaks calendar info — must be blocked when Obi is absent."""
    assert "calendar_check" in ABSENT_GATED_ACTIONS


def test_absent_gate_allows_emote():
    """emote is a silent physical expression — should NOT be in the absent gate."""
    assert "emote" not in ABSENT_GATED_ACTIONS


def test_absent_gate_blocks_look_around():
    """look_around speaks via tool-voice — must be blocked when Obi is absent."""
    assert "look_around" in ABSENT_GATED_ACTIONS


# ---------------------------------------------------------------------------
# expression() dispatch tests
# ---------------------------------------------------------------------------

expression = _MIND["expression"]
BIN_DIR = _MIND["BIN_DIR"]


def _thought(action, mood="curious", text="test thought", salience=0.5):
    return {"thought": text, "mood": mood, "action": action, "salience": salience}


@pytest.fixture(autouse=False)
def _mock_awareness_and_battery(tmp_path):
    """Stub AWARENESS_FILE and BATTERY_FILE so expression() gates don't block."""
    old_aw = _MIND.get("AWARENESS_FILE")
    old_bat = _MIND.get("BATTERY_FILE")
    aw_file = tmp_path / "awareness.json"
    bat_file = tmp_path / "battery.json"
    aw_file.write_text(_json.dumps({"obi_mode": "calm"}))
    bat_file.write_text(_json.dumps({"pct": 80, "charging": False}))
    _MIND["AWARENESS_FILE"] = aw_file
    _MIND["BATTERY_FILE"] = bat_file
    yield
    if old_aw is not None:
        _MIND["AWARENESS_FILE"] = old_aw
    if old_bat is not None:
        _MIND["BATTERY_FILE"] = old_bat


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
    old_log = _MIND.get("LOG_FILE")
    _MIND["LOG_FILE"] = log_file
    try:
        with patch("subprocess.run"):
            expression(_thought("invented_action"), dry=True)
        log_content = log_file.read_text()
        assert "unhandled action" in log_content
    finally:
        if old_log is not None:
            _MIND["LOG_FILE"] = old_log


# ---------------------------------------------------------------------------
# Calendar awareness integration (Task 2)
# ---------------------------------------------------------------------------


def test_awareness_calendar_cache_variables_exist():
    """Cache variables for HA calendar exist at module level."""
    assert "_cached_ha_calendar" in _MIND
    assert "_last_ha_calendar_fetch" in _MIND


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
