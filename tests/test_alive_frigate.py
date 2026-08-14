"""Tests for px-alive directional gaze toward Frigate-detected person."""
from __future__ import annotations
import json
import os
import sys
import types
from pathlib import Path

import pytest


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


def test_alive_heartbeat_records_loop_mode_atomically(tmp_path, monkeypatch):
    heartbeat = tmp_path / "alive_heartbeat.json"
    monkeypatch.setitem(_ALIVE, "ALIVE_HEARTBEAT_FILE", heartbeat)
    notifications = []
    monkeypatch.setitem(_ALIVE, "_sd_notify_watchdog", notifications.append)

    assert _ALIVE["write_alive_heartbeat"]("charging", now=123.5) is True

    assert json.loads(heartbeat.read_text()) == {"ts": 123.5, "mode": "charging"}
    assert notifications == ["WATCHDOG=1"]
    assert not list(tmp_path.glob("*.tmp"))


def test_charging_loop_advances_heartbeat_without_reading_sonar(monkeypatch):
    modes = []
    monkeypatch.setitem(_ALIVE, "write_alive_heartbeat", lambda mode, now=None: modes.append(mode))
    monkeypatch.setitem(_ALIVE, "_is_charging", lambda: True)
    monkeypatch.setitem(_ALIVE, "read_mood", lambda: {})
    monkeypatch.setitem(_ALIVE, "stop_face_detection", lambda: None)

    class StopLoop(Exception):
        pass

    monkeypatch.setattr(_ALIVE["time"], "sleep", lambda _seconds: (_ for _ in ()).throw(StopLoop()))
    args = types.SimpleNamespace(no_face=True)

    with pytest.raises(StopLoop):
        _ALIVE["idle_loop"](args)

    assert modes == ["starting", "charging"]


def test_running_loop_advances_heartbeat(monkeypatch):
    modes = []
    monkeypatch.setitem(_ALIVE, "write_alive_heartbeat", lambda mode, now=None: modes.append(mode))
    monkeypatch.setitem(_ALIVE, "_is_charging", lambda: False)
    monkeypatch.setitem(_ALIVE, "read_mood", lambda: {})
    monkeypatch.setitem(_ALIVE, "stop_face_detection", lambda: None)

    class StopLoop(Exception):
        pass

    monkeypatch.setattr(_ALIVE["time"], "sleep", lambda _seconds: (_ for _ in ()).throw(StopLoop()))
    args = types.SimpleNamespace(no_face=True)

    with pytest.raises(StopLoop):
        _ALIVE["idle_loop"](args)

    assert modes == ["starting", "running"]


def test_failed_heartbeat_write_does_not_notify_watchdog(tmp_path, monkeypatch):
    notifications = []
    monkeypatch.setitem(_ALIVE, "LOG_FILE", tmp_path / "px-alive.log")
    monkeypatch.setitem(_ALIVE, "_heartbeat_err_logged", False)
    monkeypatch.setitem(_ALIVE, "_sd_notify_watchdog", notifications.append)

    def fail_mkstemp(*_args, **_kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(_ALIVE["tempfile"], "mkstemp", fail_mkstemp)

    assert _ALIVE["write_alive_heartbeat"]("running", now=123.5) is False
    assert notifications == []
