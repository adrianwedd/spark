"""Unit tests for pxh.wander (extracted from bin/px-wander heredoc)."""
import datetime as dt
import json
from pxh import wander


class FakePx:
    """Minimal picarx stand-in. Scripted grayscale; records calls."""
    def __init__(self, grayscale=None):
        self._gs = list(grayscale or [])
        self.cliff_reference = [500.0, 500.0, 500.0]
        self.calls = []
    def get_grayscale_data(self):
        if not self._gs:
            raise OSError("I2C read failed")
        v = self._gs.pop(0)
        if v is None:
            raise OSError("I2C read failed")
        return list(v)
    def set_cliff_reference(self, value):
        self.cliff_reference = list(value)
    def get_cliff_status(self, gm):  # mirrors picarx.py:240 semantics
        return any(gm[i] <= self.cliff_reference[i] for i in range(3))
    def stop(self): self.calls.append("stop")
    def forward(self, s): self.calls.append(("forward", s))
    def backward(self, s): self.calls.append(("backward", s))
    def set_dir_servo_angle(self, a): self.calls.append(("dir", a))
    def set_cam_pan_angle(self, a): self.calls.append(("pan", a))
    def get_distance(self): return 100.0


def test_wander_module_importable_without_picarx():
    assert callable(wander.main)


def test_best_direction_ignores_none():
    assert wander.best_direction({0: None, 25: 40.0, -25: 10.0}) == (25, 40.0)


def test_calibrate_cliff_writes_reference(tmp_path):
    px = FakePx(grayscale=[[1000.0, 1100.0, 900.0]])
    cal = wander.calibrate_cliff(px, tmp_path)
    assert cal["floor_ref"] == [1000.0, 1100.0, 900.0]
    assert cal["cliff_ref"] == [650.0, 715.0, 585.0]
    on_disk = json.loads((tmp_path / "wander_calibration.json").read_text())
    assert on_disk == cal


def test_calibrate_cliff_read_failure_raises(tmp_path):
    import pytest
    with pytest.raises(RuntimeError):
        wander.calibrate_cliff(FakePx(grayscale=[None, None]), tmp_path)


def test_calibrate_cliff_write_failure_cleans_up_tmp(tmp_path, monkeypatch):
    import pytest

    def _raise_replace(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(wander.os, "replace", _raise_replace)
    px = FakePx(grayscale=[[1000.0, 1100.0, 900.0]])
    with pytest.raises(OSError):
        wander.calibrate_cliff(px, tmp_path)
    assert list(tmp_path.glob("*.tmp")) == []
    assert not (tmp_path / "wander_calibration.json").exists()


def test_load_calibration_missing_or_corrupt_is_none(tmp_path):
    assert wander.load_cliff_calibration(tmp_path) is None
    (tmp_path / "wander_calibration.json").write_text("{nope")
    assert wander.load_cliff_calibration(tmp_path) is None


def test_load_calibration_stale_warns_but_loads(tmp_path, monkeypatch):
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=45)).isoformat()
    (tmp_path / "wander_calibration.json").write_text(json.dumps(
        {"floor_ref": [1000, 1000, 1000], "cliff_ref": [650, 650, 650], "ts": old}))
    warnings = []
    monkeypatch.setattr(wander, "log", lambda m: warnings.append(m))
    cal = wander.load_cliff_calibration(tmp_path)
    assert cal is not None
    assert any("stale" in w.lower() for w in warnings)
