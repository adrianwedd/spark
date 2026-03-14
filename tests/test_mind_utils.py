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

    stub_keys = ("pxh", "pxh.state", "pxh.logging", "pxh.time", "pxh.token_log")
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

    sys.modules["pxh"] = stubs_pxh
    sys.modules["pxh.state"] = stubs_state
    sys.modules["pxh.logging"] = stubs_logging
    sys.modules["pxh.time"] = stubs_time
    sys.modules["pxh.token_log"] = stubs_token_log

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
filter_battery = _MIND["filter_battery"]
_battery_history = _MIND["_battery_history"]
_BATTERY_MAX_DROP = _MIND["BATTERY_MAX_DROP_PER_TICK"]
_BATTERY_GLITCH_CONFIRMS = _MIND["BATTERY_GLITCH_CONFIRMS"]


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
    """After BATTERY_GLITCH_CONFIRMS consecutive low readings, accept it."""
    _reset_battery_state()
    for pct in [50, 49, 48]:
        filter_battery({"pct": pct, "volts": 7.2}, prev_pct=pct + 1)
    # Send BATTERY_GLITCH_CONFIRMS consecutive glitch readings
    for i in range(_BATTERY_GLITCH_CONFIRMS - 1):
        r = filter_battery({"pct": 5, "volts": 6.0}, prev_pct=48)
        assert r["pct"] == 48, f"should reject on attempt {i+1}"
    # The Nth consecutive reading should be accepted
    r = filter_battery({"pct": 5, "volts": 6.0}, prev_pct=48)
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
