"""Tests for px-mind utility functions."""
from __future__ import annotations

import datetime as _dt
import json as _json
import time as _time
import urllib.error
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


def test_frigate_unreachable_sets_backoff_and_skips_next_call():
    """An unreachable host is skipped for the backoff window without a second
    connect attempt. FRIGATE_TIMEOUT_S bounds the socket but not DNS, so a
    retry every tick costs a 15-20 s resolver hang on a 60 s loop."""
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("fail")):
        assert _fetch_frigate_presence(dry=False) is None
    assert pxh.mind._host_failure_until.get(pxh.mind.FRIGATE_HOST, 0) > _time.monotonic()

    # Host is now "up", but the backoff must suppress the call entirely.
    opener = MagicMock(side_effect=_mock_urlopen_fn([_make_frigate_event()]))
    with patch("urllib.request.urlopen", opener):
        assert _fetch_frigate_presence(dry=False) is None
    assert opener.call_count == 0

    # Once the window lapses, the next call goes out normally.
    pxh.mind._host_failure_until[pxh.mind.FRIGATE_HOST] = _time.monotonic() - 1
    with patch("urllib.request.urlopen", side_effect=_mock_urlopen_fn([_make_frigate_event()])):
        assert _fetch_frigate_presence(dry=False).get("person_present") is True


def test_frigate_malformed_response_does_not_set_backoff():
    """A host that answers with garbage is reachable — it must not be backed
    off, or one bad payload blinds SPARK's cameras for the whole window."""
    def _bad_body(*args, **kwargs):
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=MagicMock(return_value=None, read=MagicMock(return_value=b"not json")))
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    with patch("urllib.request.urlopen", side_effect=_bad_body):
        assert _fetch_frigate_presence(dry=False) is None
    assert pxh.mind.FRIGATE_HOST not in pxh.mind._host_failure_until


def test_frigate_http_error_does_not_set_backoff():
    """HTTPError subclasses URLError but means the host replied — no backoff."""
    err = urllib.error.HTTPError("http://x/api/events", 500, "boom", {}, None)
    with patch("urllib.request.urlopen", side_effect=err):
        assert _fetch_frigate_presence(dry=False) is None
    assert pxh.mind.FRIGATE_HOST not in pxh.mind._host_failure_until


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
    """A low-score event (0.3 < FRIGATE_MIN_SCORE=0.6) → person_present is False."""
    events = [_make_frigate_event(score=0.3)]
    with patch("urllib.request.urlopen", side_effect=_mock_urlopen_fn(events)):
        result = _fetch_frigate_presence(dry=False)
    assert result is not None
    assert result.get("person_present") is False


def test_frigate_carries_sub_label_through():
    """Frigate's recognised name rides on sub_label. Nothing consumes it yet;
    this pins the data path so named greetings stay a phrasing change."""
    ev = _make_frigate_event(camera="picar_x")
    ev["sub_label"] = "Obi"
    with patch("urllib.request.urlopen", side_effect=_mock_urlopen_fn([ev])):
        result = _fetch_frigate_presence(dry=False)
    cam = result["cameras"]["picar_x"]
    assert cam["people"] == ["Obi"]
    assert cam["detections"][0]["sub_labels"] == ["Obi"]


def test_frigate_sub_label_list_form_and_absence():
    """Frigate has sent sub_label as [name, score] as well as a bare string,
    and sends nothing at all when recognition is off."""
    named = _make_frigate_event(camera="picar_x")
    named["sub_label"] = ["Adrian", 0.91]
    anon = _make_frigate_event(camera="picamera")   # no sub_label key
    with patch("urllib.request.urlopen", side_effect=_mock_urlopen_fn([named, anon])):
        result = _fetch_frigate_presence(dry=False)
    assert result["cameras"]["picar_x"]["people"] == ["Adrian"]
    assert result["cameras"]["picamera"]["people"] == []
    assert result["cameras"]["picamera"]["person"] is True


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


class _FakeClock:
    """Monotonic clock the test advances explicitly."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def fake_mono(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(pxh.mind.time, "monotonic", clock)
    return clock


def _volts_for(pct: int) -> float:
    """Inverse of px-battery-poll's volts_to_pct, for realistic readings."""
    return round(6.0 + (pct / 100) * (8.4 - 6.0), 2)


def test_sustained_decline_to_flat_is_reported_not_held(fake_mono):
    """A real battery going flat must surface a critical pct, not be pinned at prev.

    Replays the 2026-08-06 brownout at its real cadence. px-mind's ticks were
    starved by slow reflection calls (86.9s and 99.4s), so the pack fell
    34% -> 0% across only three evaluations spanning 222s while filter_battery
    kept returning the stale 34%. The <=10% emergency shutdown never saw a
    critical value and the Pi lost power mid-write.
    """
    for pct in (40, 38, 36, 34):
        filter_battery({"pct": pct, "volts": _volts_for(pct)}, prev_pct=pct + 2)
        fake_mono.advance(60)

    prev = 34
    reported = []
    # (pct, seconds until next evaluation) — gaps taken from the journal
    for pct, gap in ((17, 60), (15, 162), (0, 0)):
        out = filter_battery({"pct": pct, "volts": _volts_for(pct)}, prev_pct=prev)
        prev = out["pct"]
        reported.append(out["pct"])
        fake_mono.advance(gap)

    assert min(reported) <= 10, (
        f"filter never reported a critical level during a real decline: {reported}"
    )


def test_transient_zero_spike_is_still_rejected(fake_mono):
    """A single 0% spike surrounded by healthy readings must never be accepted."""
    for pct in (80, 79, 78):
        filter_battery({"pct": pct, "volts": _volts_for(pct)}, prev_pct=pct + 1)
        fake_mono.advance(60)

    spike = filter_battery({"pct": 0, "volts": 5.2}, prev_pct=78)
    assert spike["pct"] == 78

    fake_mono.advance(60)
    recovered = filter_battery({"pct": 77, "volts": _volts_for(77)}, prev_pct=78)
    assert recovered["pct"] == 77


def test_scattered_garbage_readings_never_confirm(fake_mono):
    """Wildly inconsistent low readings are a dead ADC, not a discharge curve."""
    for pct in (80, 79, 78):
        filter_battery({"pct": pct, "volts": _volts_for(pct)}, prev_pct=pct + 1)
        fake_mono.advance(60)

    prev = 78
    for pct in (0, 55, 2, 61, 0, 48):
        out = filter_battery({"pct": pct, "volts": _volts_for(pct)}, prev_pct=prev)
        prev = out["pct"]
        fake_mono.advance(60)
        assert out["pct"] >= 40, f"garbage reading {pct}% was accepted as {out['pct']}%"


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
    """Temporarily redirect STATE_DIR so _can_explore reads meta from tmp_path.

    Also seeds a valid cliff calibration so the gate tests below exercise the
    condition they name rather than short-circuiting on the calibration gate.
    """
    old = getattr(pxh.mind, "STATE_DIR", None)
    pxh.mind.STATE_DIR = tmp_path
    (tmp_path / "wander_calibration.json").write_text(_json.dumps({
        "floor_ref": [1000, 1000, 1000], "cliff_ref": [650, 650, 650],
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }))
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


def test_can_explore_requires_cliff_calibration(tmp_path, monkeypatch):
    """Autonomous explore must not arm without a calibrated cliff reference."""
    monkeypatch.setattr(pxh.mind, "STATE_DIR", tmp_path)
    session = _base_session()
    awareness = _base_awareness()
    assert _can_explore(session, awareness) is False  # no calibration file
    (tmp_path / "wander_calibration.json").write_text(_json.dumps({
        "floor_ref": [1000, 1000, 1000], "cliff_ref": [650, 650, 650],
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }))
    assert _can_explore(session, awareness) is True  # no cooldown file yet


# ---------------------------------------------------------------------------
# VALID_ACTIONS expansion + mood mapping dicts
# ---------------------------------------------------------------------------


def test_valid_actions_includes_new_actions():
    """All known actions must be present in VALID_ACTIONS."""
    expected = {
        "wait", "greet", "greet_arrival", "comment", "remember", "look_at",
        "weather_comment", "scan", "explore",
        "play_sound", "photograph", "emote", "look_around",
        "time_check", "calendar_check", "morning_fact",
        "introspect", "evolve",
        "research", "compose", "self_debug", "blog_essay",
        "message_obi", "announce",
        "set_goal", "update_goal", "complete_goal",
    }
    assert VALID_ACTIONS == expected


# Actions deliberately absent from the static enum because reflection injects
# them per-tick only when their preconditions hold (_inject_explore, gated on
# _can_explore). Offering these unconditionally would spend a reflection on an
# action that gets refused at re-check.
DYNAMIC_ACTIONS = {"explore"}


def test_prompt_action_enum_matches_valid_actions():
    """The static enum must be exactly VALID_ACTIONS minus the injected ones.

    Drift here is silent and one-directional: an action missing from both the
    enum and the injection path is unreachable no matter how complete its
    dispatch handler is, and nothing logs the omission.
    """
    from pxh.spark_config import _SPARK_REFLECTION_SUFFIX

    line = [ln for ln in _SPARK_REFLECTION_SUFFIX.splitlines()
            if '"action": "one of:' in ln]
    assert len(line) == 1, "action enum line not found (or duplicated)"
    listed = {a.strip() for a in
              line[0].split("one of:", 1)[1].rstrip('",').split(",")}
    assert listed == VALID_ACTIONS - DYNAMIC_ACTIONS


def test_dynamic_actions_are_valid_and_injectable():
    """Every dynamic action is a real action and does reach the prompt."""
    from pxh import mind, spark_config

    assert DYNAMIC_ACTIONS <= VALID_ACTIONS
    injected = mind._inject_explore(spark_config._SPARK_REFLECTION_SUFFIX)
    line = [ln for ln in injected.splitlines() if '"action": "one of:' in ln][0]
    listed = {a.strip() for a in line.split("one of:", 1)[1].rstrip('",').split(",")}
    assert listed == VALID_ACTIONS


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
def _mock_awareness_and_battery(tmp_path, monkeypatch):
    """Stub AWARENESS_FILE, BATTERY_FILE, LOG_FILE, and datetime so expression() gates
    don't block and log output stays isolated from the production px-mind.log.
    Time is fixed to 12:00 Hobart to stay clear of the 19:00–07:00 night gate."""
    import datetime as _dt
    from unittest.mock import patch as _patch

    old_aw = getattr(pxh.mind, "AWARENESS_FILE", None)
    old_bat = getattr(pxh.mind, "BATTERY_FILE", None)
    old_log = getattr(pxh.mind, "LOG_FILE", None)
    aw_file = tmp_path / "awareness.json"
    bat_file = tmp_path / "battery.json"
    aw_file.write_text(_json.dumps({"obi_mode": "calm"}))
    bat_file.write_text(_json.dumps({"pct": 80, "charging": False}))
    pxh.mind.AWARENESS_FILE = aw_file
    pxh.mind.BATTERY_FILE = bat_file
    pxh.mind.LOG_FILE = tmp_path / "px-mind.log"

    # The policy layer (#174) reads the session to decide quiet mode. Without a
    # stub these tests read the live robot session, so the verdict depends on
    # whether SPARK happens to be in quiet mode right now.
    monkeypatch.setattr(pxh.mind, "load_session", lambda: {"persona": ""})

    # Fix the clock to midday Hobart so the night gate (19:00–07:00) never fires.
    _noon = _dt.datetime(2025, 1, 1, 12, 0, 0, tzinfo=pxh.mind.HOBART_TZ)
    with _patch("pxh.mind.dt") as mock_dt:
        mock_dt.datetime.now.return_value = _noon
        mock_dt.datetime.fromisoformat = _dt.datetime.fromisoformat
        mock_dt.timezone = _dt.timezone
        mock_dt.timedelta = _dt.timedelta
        yield

    if old_aw is not None:
        pxh.mind.AWARENESS_FILE = old_aw
    if old_bat is not None:
        pxh.mind.BATTERY_FILE = old_bat
    if old_log is not None:
        pxh.mind.LOG_FILE = old_log


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


def test_expression_suppressed_during_night_window(tmp_path):
    """expression() suppresses all non-silent actions between 19:00 and 07:00 Hobart."""
    import datetime as _dt
    from unittest.mock import patch as _patch

    aw_file = tmp_path / "awareness.json"
    bat_file = tmp_path / "battery.json"
    log_file = tmp_path / "px-mind.log"
    aw_file.write_text(_json.dumps({"obi_mode": "calm"}))
    bat_file.write_text(_json.dumps({"pct": 80, "charging": False}))

    old_aw = pxh.mind.AWARENESS_FILE
    old_bat = pxh.mind.BATTERY_FILE
    old_log = pxh.mind.LOG_FILE
    pxh.mind.AWARENESS_FILE = aw_file
    pxh.mind.BATTERY_FILE = bat_file
    pxh.mind.LOG_FILE = log_file

    try:
        _midnight = _dt.datetime(2025, 6, 15, 23, 0, 0, tzinfo=pxh.mind.HOBART_TZ)
        with _patch("pxh.mind.dt") as mock_dt:
            mock_dt.datetime.now.return_value = _midnight
            mock_dt.datetime.fromisoformat = _dt.datetime.fromisoformat
            mock_dt.timezone = _dt.timezone
            mock_dt.timedelta = _dt.timedelta
            with patch("subprocess.run") as mock_run:
                for action in ("comment", "greet", "play_sound", "emote", "greet_arrival"):
                    expression(_thought(action), dry=True)
        assert mock_run.call_count == 0, "No tools should be called during night window"
        log_text = log_file.read_text()
        assert "night silence" in log_text
    finally:
        pxh.mind.AWARENESS_FILE = old_aw
        pxh.mind.BATTERY_FILE = old_bat
        pxh.mind.LOG_FILE = old_log


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


def test_sleep_404_disables_future_requests():
    """A 404 from HA sets the entity-missing flag and suppresses all future fetches."""
    def _raise_404(*args, **kwargs):
        raise urllib.error.HTTPError("http://ha.test/sensor.sleep", 404, "Not Found", {}, None)

    with _ha_ctx():
        with patch("urllib.request.urlopen", side_effect=_raise_404):
            result = _fetch_ha_sleep(dry=False)
    assert result is None
    assert pxh.mind._ha_sleep_entity_missing is True

    # Subsequent call is short-circuited — no network access needed
    result2 = _fetch_ha_sleep(dry=False)
    assert result2 is None


def test_sleep_404_reset_between_tests():
    """_reset_state() clears the entity-missing flag so tests are isolated."""
    pxh.mind._ha_sleep_entity_missing = True
    _reset_state()
    assert pxh.mind._ha_sleep_entity_missing is False


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


# ---------------------------------------------------------------------------
# Find Hub arrival detection (issue #156)
# ---------------------------------------------------------------------------


def test_findmyhub_arrival_first_seen_no_transition():
    """First-ever read of a tracker — even at_home — fires no arrival.
    Preserves the daemon-restart guard: cache starts empty."""
    from pxh.mind import _detect_findmyhub_arrivals
    transitions = _detect_findmyhub_arrivals({"obi": {"at_home": True}})
    assert transitions == []


def test_findmyhub_arrival_away_then_home_fires():
    """Classic transition: tracker seen away, then later at_home → arrival."""
    from pxh.mind import _detect_findmyhub_arrivals
    _detect_findmyhub_arrivals({"obi": {"at_home": False}})
    transitions = _detect_findmyhub_arrivals({"obi": {"at_home": True}})
    assert transitions == ["person_arrived_home:obi"]


def test_findmyhub_arrival_already_home_no_transition():
    """Tracker stays at_home across reads — no spurious arrival."""
    from pxh.mind import _detect_findmyhub_arrivals
    _detect_findmyhub_arrivals({"obi": {"at_home": True}})
    transitions = _detect_findmyhub_arrivals({"obi": {"at_home": True}})
    assert transitions == []


def test_findmyhub_arrival_survives_stale_gap():
    """Issue #156 regression: a stale-file window between the away read and
    the at_home read must not drop the arrival. Stale ticks pass {} into the
    helper; the in-memory cache should retain the prior away state."""
    from pxh.mind import _detect_findmyhub_arrivals
    # Tick 1: fresh away read populates the cache.
    _detect_findmyhub_arrivals({"obi": {"at_home": False}})
    # Ticks 2-5: file stale → helper called with empty dict, cache unchanged.
    for _ in range(4):
        assert _detect_findmyhub_arrivals({}) == []
    # Tick 6: fresh at_home read → arrival fires against the cached away state.
    transitions = _detect_findmyhub_arrivals({"obi": {"at_home": True}})
    assert transitions == ["person_arrived_home:obi"]


def test_findmyhub_arrival_independent_trackers():
    """Each tracker's arrival is independent."""
    from pxh.mind import _detect_findmyhub_arrivals
    _detect_findmyhub_arrivals({
        "obi": {"at_home": False},
        "adrian": {"at_home": True},
    })
    # obi arrives; adrian was already home and stays home → no transition.
    transitions = _detect_findmyhub_arrivals({
        "obi": {"at_home": True},
        "adrian": {"at_home": True},
    })
    assert transitions == ["person_arrived_home:obi"]


def test_findmyhub_arrival_empty_input_returns_empty():
    """Empty findmyhub yields no transitions and doesn't crash."""
    from pxh.mind import _detect_findmyhub_arrivals
    assert _detect_findmyhub_arrivals({}) == []
    assert _detect_findmyhub_arrivals(None) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Close-the-loops sprint: action-outcome feedback, night cognition, notes schema
# ---------------------------------------------------------------------------


def test_expression_research_records_failed_outcome(_mock_awareness_and_battery):
    """A budget-blocked research action records a 'failed:' outcome in session history."""
    mock_result = MagicMock(
        returncode=0,
        stdout=_json.dumps({"status": "error",
                            "error": "budget exhausted: research quota reached (3/3)"}),
        stderr="")
    with patch("subprocess.run", return_value=mock_result), \
         patch.object(pxh.mind, "update_session") as mock_us:
        expression(_thought("research"), dry=True)
    entry = mock_us.call_args.kwargs["history_entry"]
    assert entry["outcome"].startswith("failed:")
    assert "budget exhausted" in entry["outcome"]


def test_expression_research_records_ok_outcome(_mock_awareness_and_battery):
    """A successful research action records outcome='ok' in session history."""
    mock_result = MagicMock(
        returncode=0,
        stdout=_json.dumps({"status": "ok", "query": "q", "length": 512}),
        stderr="")
    with patch("subprocess.run", return_value=mock_result), \
         patch.object(pxh.mind, "update_session") as mock_us:
        expression(_thought("research"), dry=True)
    entry = mock_us.call_args.kwargs["history_entry"]
    assert entry["outcome"] == "ok"


def test_expression_compose_records_failed_outcome(_mock_awareness_and_battery):
    """compose parses tool JSON stdout the same way research does."""
    mock_result = MagicMock(
        returncode=0,
        stdout=_json.dumps({"status": "error", "error": "budget exhausted: compose quota reached (2/2)"}),
        stderr="")
    with patch("subprocess.run", return_value=mock_result), \
         patch.object(pxh.mind, "update_session") as mock_us:
        expression(_thought("compose"), dry=True)
    entry = mock_us.call_args.kwargs["history_entry"]
    assert "budget exhausted" in entry.get("outcome", "")


def test_expression_speech_actions_have_no_outcome_key(_mock_awareness_and_battery):
    """Actions without structured tool output must not grow a bogus outcome key."""
    with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")), \
         patch.object(pxh.mind, "update_session") as mock_us:
        expression(_thought("look_around"), dry=True)
    entry = mock_us.call_args.kwargs["history_entry"]
    assert "outcome" not in entry


def test_night_silence_allows_silent_cognitive_actions(tmp_path):
    """research/compose/introspect run during night silence — they make no sound."""
    import datetime as _dt2
    aw_file = tmp_path / "awareness.json"
    bat_file = tmp_path / "battery.json"
    log_file = tmp_path / "px-mind.log"
    aw_file.write_text(_json.dumps({"obi_mode": "calm"}))
    bat_file.write_text(_json.dumps({"pct": 80, "charging": False}))
    old = (pxh.mind.AWARENESS_FILE, pxh.mind.BATTERY_FILE, pxh.mind.LOG_FILE)
    pxh.mind.AWARENESS_FILE, pxh.mind.BATTERY_FILE, pxh.mind.LOG_FILE = aw_file, bat_file, log_file
    try:
        _late = _dt2.datetime(2025, 6, 15, 23, 0, 0, tzinfo=pxh.mind.HOBART_TZ)
        with patch("pxh.mind.dt") as mock_dt:
            mock_dt.datetime.now.return_value = _late
            mock_dt.datetime.fromisoformat = _dt2.datetime.fromisoformat
            mock_dt.timezone = _dt2.timezone
            mock_dt.timedelta = _dt2.timedelta
            with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="{}", stderr="")) as mock_run, \
                 patch.object(pxh.mind, "update_session"):
                expression(_thought("research"), dry=True)
                expression(_thought("compose"), dry=True)
                expression(_thought("introspect"), dry=True)
                expression(_thought("comment"), dry=True)  # spoken — must stay suppressed
        called = str(mock_run.call_args_list)
        assert "tool-research" in called
        assert "tool-compose" in called
        assert "tool-introspect" in called
        assert "tool-voice" not in called
        assert "night silence" in log_file.read_text()  # comment was suppressed
    finally:
        pxh.mind.AWARENESS_FILE, pxh.mind.BATTERY_FILE, pxh.mind.LOG_FILE = old


def test_absent_mode_no_longer_gates_research_and_compose():
    """Silent cognitive work is exactly what absent/idle time is for."""
    assert "research" not in pxh.mind.ABSENT_GATED_ACTIONS
    assert "compose" not in pxh.mind.ABSENT_GATED_ACTIONS
    # Spoken/visible actions stay gated
    assert "comment" in pxh.mind.ABSENT_GATED_ACTIONS
    assert "greet" in pxh.mind.ABSENT_GATED_ACTIONS


def test_load_notes_skips_records_without_note(tmp_path, monkeypatch):
    """Legacy research/compose records (no 'note' key) must not blank the memory window."""
    nf = tmp_path / "notes-spark.jsonl"
    nf.write_text("\n".join([
        _json.dumps({"ts": "t1", "type": "research", "query": "q", "response": "r"}),
        _json.dumps({"ts": "t2", "note": "older real memory"}),
        _json.dumps({"ts": "t3", "note": ""}),
        _json.dumps({"ts": "t4", "note": "newest real memory"}),
    ]) + "\n")
    monkeypatch.setattr(pxh.mind, "notes_file_for_persona", lambda p: nf)
    notes = pxh.mind.load_notes(2, "spark")
    assert notes == ["older real memory", "newest real memory"]


def test_load_notes_with_provenance_labels_where_each_note_came_from(tmp_path, monkeypatch):
    from pxh import provenance
    nf = tmp_path / "notes-spark.jsonl"
    nf.write_text("\n".join([
        _json.dumps(provenance.stamp({"ts": "t1", "note": "Obi likes lego"},
                                     "report", "voice:obi")),
        _json.dumps(provenance.stamp({"ts": "t2", "note": "I felt quiet today"},
                                     "narrative", "mind:auto_remember")),
    ]) + "\n")
    monkeypatch.setattr(pxh.mind, "notes_file_for_persona", lambda p: nf)

    notes = pxh.mind.load_notes(2, "spark", with_provenance=True)

    assert "Obi likes lego" in notes[0] and "told me" in notes[0]
    assert "I felt quiet today" in notes[1] and "own reflection" in notes[1]


def test_load_notes_with_provenance_marks_a_legacy_note_unknown(tmp_path, monkeypatch):
    nf = tmp_path / "notes-spark.jsonl"
    nf.write_text(_json.dumps({"ts": "t1", "note": "an old note"}) + "\n")
    monkeypatch.setattr(pxh.mind, "notes_file_for_persona", lambda p: nf)

    notes = pxh.mind.load_notes(1, "spark", with_provenance=True)

    assert "an old note" in notes[0]
    assert "unknown" in notes[0].lower()


def test_auto_remember_stamps_the_note_as_generated_narrative(tmp_path, monkeypatch):
    """A high-salience thought saved to notes is SPARK's own prose, and must
    not later be readable as something it observed."""
    from pxh import provenance
    nf = tmp_path / "notes-spark.jsonl"
    monkeypatch.setattr(pxh.mind, "notes_file_for_persona", lambda p: nf)

    pxh.mind.auto_remember({"thought": "the house feels empty tonight",
                            "ts": "2026-08-14T09:00:00Z"}, persona="spark")

    record = _json.loads(nf.read_text().strip())
    p = provenance.read_provenance(record)
    assert p["kind"] == "narrative"
    assert p["confidence"] <= provenance.CONFIDENCE_CEILING["narrative"]
    assert "the house feels empty tonight" in record["note"]


# ---------------------------------------------------------------------------
# Continuity sprint: goal actions
# ---------------------------------------------------------------------------


def test_goal_actions_are_valid_and_night_allowed():
    from pxh.mind import VALID_ACTIONS, NIGHT_ALLOWED_ACTIONS, ABSENT_GATED_ACTIONS
    for a in ("set_goal", "update_goal", "complete_goal"):
        assert a in VALID_ACTIONS
        assert a in NIGHT_ALLOWED_ACTIONS
        assert a not in ABSENT_GATED_ACTIONS


def test_expression_set_goal_writes_intention_and_records_ok(
        _mock_awareness_and_battery, tmp_path, monkeypatch):
    from pxh import intention
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    with patch.object(pxh.mind, "update_session") as mock_us:
        expression(_thought("set_goal", text="map the hallway this week"), dry=True)
    assert intention.get_active_goal() == "map the hallway this week"
    entry = mock_us.call_args.kwargs["history_entry"]
    assert entry["outcome"] == "ok"


def test_expression_update_goal_without_active_records_failed(
        _mock_awareness_and_battery, tmp_path, monkeypatch):
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    with patch.object(pxh.mind, "update_session") as mock_us:
        expression(_thought("update_goal", text="progress on nothing"), dry=True)
    entry = mock_us.call_args.kwargs["history_entry"]
    assert entry["outcome"].startswith("failed:")
    assert "no active intention" in entry["outcome"]


def test_expression_complete_goal_archives_and_records_ok(
        _mock_awareness_and_battery, tmp_path, monkeypatch):
    from pxh import intention
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    intention.set_goal("finish the map")
    with patch.object(pxh.mind, "update_session") as mock_us:
        expression(_thought("complete_goal", text="mapped every corner"), dry=True)
    assert intention.get_active_goal() == ""
    entry = mock_us.call_args.kwargs["history_entry"]
    assert entry["outcome"] == "ok"


def _capture_reflection_context(monkeypatch, awareness):
    """Run reflection() with a fake LLM; return the context string it was sent."""
    captured = {}

    def fake_llm(context, system_prompt, persona=""):
        captured["context"] = context
        return {"response": _json.dumps(
            {"thought": "t", "mood": "curious", "action": "wait", "salience": 0.4})}

    monkeypatch.setattr(pxh.mind, "call_llm", fake_llm)
    pxh.mind.reflection(awareness, dry=False)
    return captured.get("context", "")


def test_reflection_injects_relevant_memories(tmp_path, monkeypatch):
    from pxh import memory
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    memory.append_memories([{
        "ts": "2026-07-10T12:00:00Z", "date": "2026-07-10",
        "text": "Obi and I built a lego tower on the kitchen floor",
        "tags": ["obi", "lego"], "importance": 0.8, "source": "consolidation"}])
    awareness = {"persona": "spark", "time_period": "afternoon",
                 "recent_conversations": [
                     {"who": "Obi", "text": "can we do lego again", "minutes_ago": 5}]}
    ctx = _capture_reflection_context(monkeypatch, awareness)
    assert "Memories that feel relevant right now" in ctx
    assert "lego tower" in ctx


def test_reflection_labels_each_memory_with_where_it_came_from(tmp_path, monkeypatch):
    """#170's acceptance: a retrieved claim reaching cognition must carry its
    own answer to "where did this come from?"."""
    from pxh import memory, provenance
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    memory.append_memories([provenance.stamp(
        {"ts": "2026-07-10T12:00:00Z", "date": "2026-07-10",
         "text": "Obi and I built a lego tower", "tags": ["obi", "lego"],
         "importance": 0.8, "source": "consolidation"},
        "narrative", "consolidation")])
    awareness = {"persona": "spark", "time_period": "afternoon",
                 "recent_conversations": [
                     {"who": "Obi", "text": "can we do lego again", "minutes_ago": 5}]}

    ctx = _capture_reflection_context(monkeypatch, awareness)

    assert "lego tower" in ctx
    assert "own reflection" in ctx


def test_reflection_labels_a_legacy_memory_as_unknown_provenance(tmp_path, monkeypatch):
    from pxh import memory
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    memory.append_memories([{
        "ts": "2026-07-10T12:00:00Z", "date": "2026-07-10",
        "text": "Obi and I built a lego tower", "tags": ["obi", "lego"],
        "importance": 0.8, "source": "consolidation"}])   # pre-#170 record
    awareness = {"persona": "spark", "time_period": "afternoon",
                 "recent_conversations": [
                     {"who": "Obi", "text": "can we do lego again", "minutes_ago": 5}]}

    ctx = _capture_reflection_context(monkeypatch, awareness)

    assert "lego tower" in ctx
    assert "unknown" in ctx.lower()


def test_reflection_omits_memories_when_nothing_is_topically_relevant(tmp_path, monkeypatch):
    """A populated store with no relevant record must inject no memories at all.

    The raw-notes fallback exists for a persona that has no consolidated store
    yet. Letting it fire on a zero-match search would re-open the hole #171
    closes: unrelated recent material entering cognition because the relevant
    slot came back empty.
    """
    from pxh import memory
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    memory.append_memories([{
        "ts": "2026-07-10T12:00:00Z", "date": "2026-07-10",
        "text": "Obi and I built a lego tower on the kitchen floor",
        "tags": ["obi", "lego"], "importance": 0.8, "source": "consolidation"}])
    monkeypatch.setattr(pxh.mind, "load_notes",
                        lambda *a, **k: ["an old raw note"])
    ctx = _capture_reflection_context(
        monkeypatch, {"persona": "spark", "time_period": "afternoon",
                      "recent_conversations": [
                          {"who": "Adrian", "text": "xylophone quartz",
                           "minutes_ago": 5}]})
    assert "lego tower" not in ctx
    assert "an old raw note" not in ctx
    assert "Your long-term memories" not in ctx


def test_reflection_falls_back_to_notes_when_no_memories(tmp_path, monkeypatch):
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(pxh.mind, "load_notes",
                        lambda *a, **k: ["an old raw note"])
    ctx = _capture_reflection_context(
        monkeypatch, {"persona": "spark", "time_period": "afternoon"})
    assert "Your long-term memories" in ctx
    assert "an old raw note" in ctx
    assert "Memories that feel relevant" not in ctx


def test_reflection_injects_active_intention(tmp_path, monkeypatch):
    from pxh import intention
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    intention.set_goal("map the hallway this week")
    ctx = _capture_reflection_context(
        monkeypatch, {"persona": "spark", "time_period": "afternoon"})
    assert "map the hallway this week" in ctx
    assert "current intention" in ctx.lower()


def test_consolidation_tick_spark_logs_result(monkeypatch):
    calls = []
    monkeypatch.setattr(pxh.mind.spark_memory, "maybe_consolidate",
                        lambda dry: {"status": "ok", "written": 2})
    monkeypatch.setattr(pxh.mind, "log", lambda msg: calls.append(msg))
    pxh.mind._consolidation_tick({"persona": "spark"}, dry=False)
    assert any("consolidation: ok" in c and "wrote 2" in c for c in calls)


def test_consolidation_tick_none_is_silent(monkeypatch):
    calls = []
    monkeypatch.setattr(pxh.mind.spark_memory, "maybe_consolidate", lambda dry: None)
    monkeypatch.setattr(pxh.mind, "log", lambda msg: calls.append(msg))
    pxh.mind._consolidation_tick({"persona": "spark"}, dry=False)
    assert calls == []


def test_consolidation_tick_never_raises(monkeypatch):
    def boom(dry):
        raise RuntimeError("disk exploded")
    calls = []
    monkeypatch.setattr(pxh.mind.spark_memory, "maybe_consolidate", boom)
    monkeypatch.setattr(pxh.mind, "log", lambda msg: calls.append(msg))
    pxh.mind._consolidation_tick({"persona": "spark"}, dry=False)  # must not raise
    assert any("consolidation error" in c for c in calls)


def test_consolidation_tick_skips_other_personas(monkeypatch):
    called = []
    monkeypatch.setattr(pxh.mind.spark_memory, "maybe_consolidate",
                        lambda dry: called.append(1))
    pxh.mind._consolidation_tick({"persona": "gremlin"}, dry=False)
    pxh.mind._consolidation_tick({}, dry=False)
    assert called == []


def test_reflection_survives_memory_retrieval_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    def boom(*a, **k):
        raise RuntimeError("retrieval broke")
    monkeypatch.setattr(pxh.mind.spark_memory, "retrieve_memories", boom)
    monkeypatch.setattr(pxh.mind, "load_notes", lambda *a, **k: ["a raw note"])
    ctx = _capture_reflection_context(
        monkeypatch, {"persona": "spark", "time_period": "afternoon"})
    assert "Your long-term memories" in ctx  # fallback still fires
    assert "a raw note" in ctx


def test_reflection_survives_intention_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    def boom(*a, **k):
        raise RuntimeError("intention broke")
    monkeypatch.setattr(pxh.mind.intention_mod, "format_for_context", boom)
    ctx = _capture_reflection_context(
        monkeypatch, {"persona": "spark", "time_period": "afternoon"})
    assert ctx  # reflection completed and produced a context
    assert "current intention" not in ctx.lower()


def test_spark_prompt_offers_goal_actions_and_explore_injection_still_works():
    from pxh.spark_config import _SPARK_REFLECTION_SUFFIX
    from pxh.mind import _inject_explore
    assert "set_goal, update_goal, complete_goal" in _SPARK_REFLECTION_SUFFIX
    assert '- "set_goal"' in _SPARK_REFLECTION_SUFFIX
    patched = _inject_explore(_SPARK_REFLECTION_SUFFIX)
    assert ", explore" in patched  # regex injection survives the longer enum


def test_awareness_reads_observations_file(tmp_path, monkeypatch):
    from pxh import mind
    monkeypatch.setattr(mind, "STATE_DIR", tmp_path)
    obs = {"type": "observation", "landmark": "bookshelf corner",
           "heading_estimate": "", "interesting": True, "vision_failed": False}
    (tmp_path / "observations.jsonl").write_text(_json.dumps(obs) + "\n")
    # nav spam in the OLD file must not shadow observations anymore
    (tmp_path / "exploration.jsonl").write_text(
        "\n".join(_json.dumps({"type": "nav", "action": "forward"}) for _ in range(100)) + "\n")
    recent = mind._recent_exploration_observations()   # extracted helper, see Step 3
    assert recent and recent[0]["landmark"] == "bookshelf corner"


# ---------------------------------------------------------------------------
# Find Hub feed health (surfaces silent auth breakage — 2026-08-01)
# ---------------------------------------------------------------------------


def _write_findmyhub(tmp_path, monkeypatch, trackers, ts=None):
    from pxh import mind
    path = tmp_path / "findmyhub.json"
    path.write_text(_json.dumps({"ts": ts or _time.time(), "trackers": trackers}))
    monkeypatch.setattr(mind, "FINDMYHUB_FILE", path)
    return path


def test_findmyhub_health_ok_on_valid_trackers(tmp_path, monkeypatch):
    from pxh import health, mind
    now = _time.time()
    _write_findmyhub(tmp_path, monkeypatch, {
        "obi": {"lat": -43.13567, "lon": 147.11840, "accuracy_m": 10, "ts": now},
    })
    result = mind._read_findmyhub()
    assert "obi" in result
    rec = health.read_health(("findmyhub",))["components"]["findmyhub"]
    assert rec["status"] == "ok"


def test_findmyhub_health_fails_when_all_trackers_error(tmp_path, monkeypatch):
    """The BadAuthentication signature: fresh file, valid JSON, every tracker
    carries an error key. Must surface as a health failure, not silence."""
    from pxh import health, mind
    _write_findmyhub(tmp_path, monkeypatch, {
        "obi": {"error": "'Auth'"},
        "adrian": {"error": "'Auth'"},
    })
    assert mind._read_findmyhub() == {}
    rec = health.read_health(("findmyhub",))["components"]["findmyhub"]
    assert rec["status"] == "degraded"
    assert "Auth" in rec["last_error"]
    # Three consecutive bad reads → failing.
    mind._read_findmyhub()
    mind._read_findmyhub()
    rec = health.read_health(("findmyhub",))["components"]["findmyhub"]
    assert rec["status"] == "failing"


def test_findmyhub_health_recovers_after_auth_fixed(tmp_path, monkeypatch):
    from pxh import health, mind
    _write_findmyhub(tmp_path, monkeypatch, {"obi": {"error": "'Auth'"}})
    mind._read_findmyhub()
    now = _time.time()
    _write_findmyhub(tmp_path, monkeypatch, {
        "obi": {"lat": -43.13567, "lon": 147.11840, "ts": now},
    })
    mind._read_findmyhub()
    rec = health.read_health(("findmyhub",))["components"]["findmyhub"]
    assert rec["status"] == "ok"


def test_findmyhub_health_semantic_only_is_ok(tmp_path, monkeypatch):
    """Semantic (address-only) locations are a healthy feed, not a failure."""
    from pxh import health, mind
    now = _time.time()
    _write_findmyhub(tmp_path, monkeypatch, {
        "obi": {"semantic": True, "address": "school", "ts": now},
    })
    mind._read_findmyhub()
    rec = health.read_health(("findmyhub",))["components"]["findmyhub"]
    assert rec["status"] == "ok"


def test_findmyhub_health_empty_trackers_is_failure(tmp_path, monkeypatch):
    from pxh import health, mind
    _write_findmyhub(tmp_path, monkeypatch, {})
    assert mind._read_findmyhub() == {}
    rec = health.read_health(("findmyhub",))["components"]["findmyhub"]
    assert rec["status"] == "degraded"


def test_findmyhub_health_stale_file_writes_nothing(tmp_path, monkeypatch):
    """A stale file means the *push* stopped — the record must be allowed to
    age into 'stale' on its own, not be refreshed as success or failure."""
    from pxh import health, mind
    _write_findmyhub(tmp_path, monkeypatch, {"obi": {"error": "x"}},
                     ts=_time.time() - 10_000)
    assert mind._read_findmyhub() == {}
    rec = health.read_health(("findmyhub",))["components"]["findmyhub"]
    assert rec["status"] == "missing"  # nothing ever recorded


def test_findmyhub_health_missing_file_writes_nothing(tmp_path, monkeypatch):
    from pxh import health, mind
    monkeypatch.setattr(mind, "FINDMYHUB_FILE", tmp_path / "nope.json")
    assert mind._read_findmyhub() == {}
    rec = health.read_health(("findmyhub",))["components"]["findmyhub"]
    assert rec["status"] == "missing"


def test_findmyhub_health_corrupt_json_is_failure(tmp_path, monkeypatch):
    from pxh import health, mind
    path = tmp_path / "findmyhub.json"
    path.write_text("{not json")
    monkeypatch.setattr(mind, "FINDMYHUB_FILE", path)
    assert mind._read_findmyhub() == {}
    rec = health.read_health(("findmyhub",))["components"]["findmyhub"]
    assert rec["status"] == "degraded"
    assert "read error" in rec["last_error"]


# ---------------------------------------------------------------------------
# Frigate last_person_ts carry-through (greet recency, 2026-08-01)
# ---------------------------------------------------------------------------


def test_frigate_snapshot_carries_last_person_ts(monkeypatch, tmp_path):
    from pxh import mind
    monkeypatch.setattr(mind, "FRIGATE_FILE", tmp_path / "frigate_presence.json")
    mind._last_person_seen.clear()
    mind._host_failure_until.clear()
    events = [_make_frigate_event(score=0.8, camera="picar_x")]
    with patch("urllib.request.urlopen", side_effect=_mock_urlopen_fn(events)):
        r1 = _fetch_frigate_presence(dry=False)
    ts1 = r1["cameras"]["picar_x"]["last_person_ts"]
    assert ts1
    # Person goes still → no events — the sighting timestamp is carried forward.
    with patch("urllib.request.urlopen", side_effect=_mock_urlopen_fn([])):
        r2 = _fetch_frigate_presence(dry=False)
    assert r2["cameras"]["picar_x"]["person"] is False
    assert r2["cameras"]["picar_x"]["last_person_ts"] == ts1


def test_frigate_last_person_ts_absent_when_never_seen(monkeypatch, tmp_path):
    from pxh import mind
    monkeypatch.setattr(mind, "FRIGATE_FILE", tmp_path / "frigate_presence.json")
    mind._last_person_seen.clear()
    mind._host_failure_until.clear()
    with patch("urllib.request.urlopen", side_effect=_mock_urlopen_fn([])):
        r = _fetch_frigate_presence(dry=False)
    assert "last_person_ts" not in r["cameras"]["picar_x"]


def test_frigate_last_person_ts_seeds_from_snapshot_file(monkeypatch, tmp_path):
    """px-mind restarts must not forget a recent sighting: the in-memory cache
    seeds from the previous snapshot file on first fetch."""
    from pxh import mind
    snap = tmp_path / "frigate_presence.json"
    snap.write_text(_json.dumps({
        "cameras": {"picar_x": {"person": True, "last_person_ts": "2026-08-01T03:00:00+00:00"}},
    }))
    monkeypatch.setattr(mind, "FRIGATE_FILE", snap)
    mind._last_person_seen.clear()
    monkeypatch.setattr(mind, "_last_person_seen_seeded", False)
    mind._host_failure_until.clear()
    with patch("urllib.request.urlopen", side_effect=_mock_urlopen_fn([])):
        r = _fetch_frigate_presence(dry=False)
    assert r["cameras"]["picar_x"]["last_person_ts"] == "2026-08-01T03:00:00+00:00"
