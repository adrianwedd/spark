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


def _guard():
    return wander.CliffGuard([650.0, 650.0, 650.0])


def test_cliff_guard_detects_drop():
    px = FakePx(grayscale=[[1000, 640, 1000]])   # center ≤ ref → cliff
    assert _guard().check(px) == "cliff"


def test_cliff_guard_clear():
    px = FakePx(grayscale=[[1000, 1000, 1000]])
    assert _guard().check(px) == "clear"


def test_cliff_guard_read_failure_is_fail_closed():
    px = FakePx(grayscale=[None, None, None])    # retries exhausted
    assert _guard().check(px) == "fail"


def test_guarded_forward_stops_and_reverses_on_cliff(monkeypatch):
    monkeypatch.setattr(wander.time, "sleep", lambda s: None)
    px = FakePx(grayscale=[[1000]*3, [600]*3] + [[1000]*3]*5)
    px._dist = iter([50.0, 60.0])                # before/after reverse: moved
    px.get_distance = lambda: next(px._dist, 60.0)
    guard = _guard()
    r = wander.guarded_forward(px, guard, speed=30, duration_s=0.5)
    assert r == "edge"
    assert guard.edge_events == 1
    assert "stop" in px.calls
    assert ("backward", wander.REVERSE_SPEED) in px.calls


def test_bounded_reverse_stall_detection(monkeypatch):
    monkeypatch.setattr(wander.time, "sleep", lambda s: None)
    px = FakePx(grayscale=[[1000]*3]*5)
    px._dist = iter([50.0, 50.5])                # clearance didn't grow → stall
    px.get_distance = lambda: next(px._dist, 50.5)
    assert wander.bounded_reverse(px) is True


def test_guarded_forward_cliff_plus_stall_counts_two_events(monkeypatch):
    """Cornered case: cliff trip AND stalled escape → 2 edge events, which at
    EDGE_ABORT_COUNT=2 aborts the wander on the spot. That instant abort is
    INTENTIONAL — this test pins it so a refactor can't silently change it."""
    monkeypatch.setattr(wander.time, "sleep", lambda s: None)
    px = FakePx(grayscale=[[600]*3] + [[1000]*3]*5)   # first check trips cliff
    px._dist = iter([50.0, 50.5])                # clearance didn't grow → stall
    px.get_distance = lambda: next(px._dist, 50.5)
    guard = _guard()
    assert wander.guarded_forward(px, guard, speed=30, duration_s=0.5) == "edge"
    assert guard.edge_events == 2
    assert guard.edge_events >= wander.EDGE_ABORT_COUNT
    # The guard is checked BEFORE the first slice — a wander that starts
    # already at the desk edge must never move at all.
    assert not any(isinstance(c, tuple) and c[0] == "forward" for c in px.calls)


def test_probe_turn_picks_clearer_side(monkeypatch):
    monkeypatch.setattr(wander.time, "sleep", lambda s: None)
    px = FakePx(grayscale=[[1000]*3]*20)
    # left probe reads 20cm (blocked), arc-back ok, right probe reads 90cm → commit right
    px._dist = iter([20.0, 40.0, 55.0, 90.0])
    px.get_distance = lambda: next(px._dist, 90.0)
    side, clearance = wander.probe_turn(px, _guard(), prefer="left")
    assert side == "right" and clearance == 90.0
    assert ("dir", 30) in px.calls and ("dir", -30) in px.calls


def test_probe_turn_edge_aborts_probe(monkeypatch):
    monkeypatch.setattr(wander.time, "sleep", lambda s: None)
    px = FakePx(grayscale=[[600]*3] * 5)     # cliff on first probe creep
    px._dist = iter([50.0, 55.0])
    px.get_distance = lambda: next(px._dist, 55.0)
    guard = _guard()
    side, _ = wander.probe_turn(px, guard, prefer="left")
    assert side == "edge"
    assert guard.edge_events >= 1


def test_probe_turn_first_side_commit_rearcs(monkeypatch):
    """Both probes < CLEAR_CM, first side best: the chassis must END on the
    first side's arc, not stranded at the end of the second probe arc."""
    monkeypatch.setattr(wander.time, "sleep", lambda s: None)
    px = FakePx(grayscale=[[1000]*3]*30)
    # left probe 40cm (best), arc-back 50→55, right probe 20cm,
    # arc-back 50→55, then re-commit left (guarded creep, no sonar read)
    px._dist = iter([40.0, 50.0, 55.0, 20.0, 50.0, 55.0])
    px.get_distance = lambda: next(px._dist, 55.0)
    side, clearance = wander.probe_turn(px, _guard(), prefer="left")
    assert side == "left" and clearance == 40.0
    # last non-zero steer is the LEFT re-commit, not the right probe
    steers = [c for c in px.calls if c[0] == "dir" and c[1] != 0]
    assert steers[-1] == ("dir", -30)


def test_sweep_helpers_are_gone():
    for name in ("sweep_distances", "_sweep_sonar", "read_dist", "_heading_label", "best_direction"):
        assert not hasattr(wander, name)
