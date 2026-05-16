"""Tests for px-alive directional gaze toward Frigate-detected person."""
from __future__ import annotations
import os
import sys
import types
from pathlib import Path


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

    env_patch = {
        "PROJECT_ROOT": str(PROJECT_ROOT),
        "LOG_DIR": str(PROJECT_ROOT / "logs"),
        "PX_STATE_DIR": str(PROJECT_ROOT / "state"),
    }
    old_env = {k: os.environ.get(k) for k in env_patch}
    for k, v in env_patch.items():
        os.environ[k] = v

    globs: dict = {"__file__": str(PROJECT_ROOT / "bin" / "px-alive")}
    try:
        exec(compile(py_src, "bin/px-alive", "exec"), globs)  # noqa: S102
    finally:
        for k, old_mod in saved.items():
            sys.modules.pop(k, None) if old_mod is None else sys.modules.update({k: old_mod})
        for k, old_v in old_env.items():
            os.environ.pop(k, None) if old_v is None else os.environ.update({k: old_v})
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
