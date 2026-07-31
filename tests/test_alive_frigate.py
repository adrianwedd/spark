"""Tests for px-alive directional gaze toward Frigate-detected person."""
from __future__ import annotations
import datetime as dt
import sys
import types
from pathlib import Path

from _harness import daemon_load_env

PROJECT_ROOT = Path(__file__).parent.parent


def _load_alive_helpers():
    src = (PROJECT_ROOT / "bin" / "px-alive").read_text()
    start = src.index("<<'PY'\n") + len("<<'PY'\n")
    end = src.rindex("\nPY\n")
    py_src = src[start:end]

    stub_keys = ("pxh", "pxh.logging", "pxh.time", "picarx", "robot_hat", "vilib")
    saved = {k: sys.modules.get(k) for k in stub_keys + ("pxh.state",)}
    for k in stub_keys:
        sys.modules[k] = types.ModuleType(k)
    stubs_state = types.ModuleType("pxh.state")
    stubs_state.load_session = lambda: {}
    sys.modules["pxh.state"] = stubs_state  # explicit, not overwritten by loop

    globs: dict = {"__file__": str(PROJECT_ROOT / "bin" / "px-alive")}
    try:
        # STATE_DIR is frozen here for the session — see tests/_harness.py.
        with daemon_load_env():
            exec(compile(py_src, "bin/px-alive", "exec"), globs)  # noqa: S102
    finally:
        for k, old_mod in saved.items():
            sys.modules.pop(k, None) if old_mod is None else sys.modules.update({k: old_mod})
    return globs


_ALIVE = _load_alive_helpers()
_pan_from_frigate = _ALIVE["_pan_from_frigate"]
FRIGATE_STALE_S = _ALIVE["FRIGATE_STALE_S"]


def test_pan_center():
    assert _pan_from_frigate({"person_present": True, "x_center": 0.5}) == 0


def test_pan_right():
    # Person right of frame (x=0.8) → -24 (picarx: positive=left, negative=right)
    assert _pan_from_frigate({"person_present": True, "x_center": 0.8}) == -24


def test_pan_left():
    # Person left of frame (x=0.2) → +24
    assert _pan_from_frigate({"person_present": True, "x_center": 0.2}) == 24


def test_pan_clamped_max():
    # Extreme left (x=0.0) → exactly +40
    assert _pan_from_frigate({"person_present": True, "x_center": 0.0}) == 40


def test_pan_clamped_min():
    # Extreme right (x=1.0) → exactly -40
    assert _pan_from_frigate({"person_present": True, "x_center": 1.0}) == -40


def test_pan_no_detection():
    assert _pan_from_frigate({"person_present": False, "x_center": None}) == 0


def test_pan_none_input():
    assert _pan_from_frigate(None) == 0


def test_pan_non_dict_input():
    # JSON array or other non-dict values must not crash the daemon
    assert _pan_from_frigate([1, 2, 3]) == 0
    assert _pan_from_frigate("person") == 0


def test_pan_non_numeric_x_center():
    # Non-numeric x_center must not crash the daemon
    assert _pan_from_frigate({"person_present": True, "x_center": "left"}) == 0
    assert _pan_from_frigate({"person_present": True, "x_center": None}) == 0


def test_frigate_stale_s_constant():
    # Staleness threshold should be defined
    assert FRIGATE_STALE_S > 0


# ---------------------------------------------------------------------------
# _person_confirmed — the veto that stops SPARK greeting furniture
# ---------------------------------------------------------------------------

_person_confirmed = _ALIVE["_person_confirmed"]
GREET_FRIGATE_STALE_S = _ALIVE["GREET_FRIGATE_STALE_S"]
GREET_CONFIRM_CAMERAS = _ALIVE["GREET_CONFIRM_CAMERAS"]


def _presence(cameras, age_s=0.0):
    ts = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=age_s)
    return {"ts": ts.isoformat(), "cameras": cameras}


def test_greet_confirmed_by_robot_camera():
    assert _person_confirmed(_presence({"picar_x": {"person": True}})) is True


def test_greet_confirmed_by_indoor_camera():
    """Robot's own head may be turned away — the indoor camera still confirms."""
    assert _person_confirmed(
        _presence({"picar_x": {"person": False}, "picamera": {"person": True}})
    ) is True


def test_greet_not_confirmed_by_outdoor_camera_only():
    """Someone in the driveway says nothing about what's in front of the robot."""
    assert _person_confirmed(
        _presence({"driveway_camera": {"person": True}, "garden_camera": {"person": True}})
    ) is False


def test_greet_not_confirmed_without_person():
    """The chair-leg case: sonar fired, no camera saw anyone."""
    assert _person_confirmed(
        _presence({"picar_x": {"person": False}, "picamera": {"person": False}})
    ) is False


def test_greet_not_confirmed_when_stale():
    """Past the window, a person sighting means a room someone already left."""
    stale = _presence({"picar_x": {"person": True}}, age_s=GREET_FRIGATE_STALE_S + 30)
    assert _person_confirmed(stale) is False


def test_greet_stale_bound_is_tighter_than_pan_bound():
    """Aiming the head tolerates stale data; speaking does not."""
    assert GREET_FRIGATE_STALE_S < FRIGATE_STALE_S


def test_greet_confirmation_survives_malformed_input():
    """px-alive must never die on a bad state file — every branch returns False."""
    for bad in (None, [1, 2, 3], "person", {}, {"ts": "not-a-timestamp"},
                {"ts": None, "cameras": {}},
                _presence("cameras-not-a-dict"),
                _presence({"picar_x": "not-a-dict"})):
        assert _person_confirmed(bad) is False


def test_greet_confirm_cameras_are_indoor_only():
    assert "driveway_camera" not in GREET_CONFIRM_CAMERAS
    assert "garden_camera" not in GREET_CONFIRM_CAMERAS


def test_alive_fallbacks_match_spark_config():
    """_load_alive_helpers() stubs out pxh, so the constants under test above
    are px-alive's ImportError fallbacks. Pin them to spark_config — otherwise
    SPARK evolves the config and the values px-alive actually uses on a stripped
    PYTHONPATH drift away silently."""
    from pxh import spark_config

    assert _ALIVE["PROXIMITY_GREETINGS"] == spark_config.PROXIMITY_GREETINGS
    assert GREET_CONFIRM_CAMERAS == spark_config.GREET_CONFIRM_CAMERAS
    assert GREET_FRIGATE_STALE_S == spark_config.GREET_FRIGATE_STALE_S
