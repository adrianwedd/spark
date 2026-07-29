"""Unit tests for pxh.wander (extracted from bin/px-wander heredoc)."""
import datetime as dt
import json
from pathlib import Path
from pxh import wander

# NOTE: wander.PROJECT_ROOT is computed at import time from a fallback
# (Path(__file__).resolve().parent.parent) that resolves to src/, not the
# repo root, whenever the PROJECT_ROOT env var isn't already set in the
# process — which is exactly the case for a bare `python -m pytest` (only
# subprocess envs get PROJECT_ROOT via the isolated_project fixture). That's
# a pre-existing bug unrelated to this task (bin/px-env always sets the env
# var in real usage, so it's never hit outside tests) — use our own
# known-good repo root for subprocess cwd instead of relying on the module
# attribute.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


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


def test_explore_step_forward_when_clear(monkeypatch):
    monkeypatch.setattr(wander.time, "sleep", lambda s: None)
    px = FakePx(grayscale=[[1000]*3]*10)
    px.get_distance = lambda: 120.0
    state = {"forced_turn": None, "stuck_count": 0, "sensor_fail_streak": 0,
             "steps_completed": 1, "explore_id": "e-test"}
    entry = wander.run_explore_step(px, _guard(), state)
    assert entry["action"] == "forward"
    assert entry["sonar_cm"] == 120.0
    assert "heading_estimate" not in entry


def test_explore_step_probes_when_blocked(monkeypatch):
    monkeypatch.setattr(wander.time, "sleep", lambda s: None)
    px = FakePx(grayscale=[[1000]*3]*20)
    px._dist = iter([15.0, 80.0])            # blocked ahead; left probe clear
    px.get_distance = lambda: next(px._dist, 80.0)
    state = {"forced_turn": None, "stuck_count": 0, "sensor_fail_streak": 0,
             "steps_completed": 1, "explore_id": "e-test"}
    entry = wander.run_explore_step(px, _guard(), state)
    assert entry["action"] == "turned_left"
    assert state["stuck_count"] == 0


def test_append_jsonl_capped_trims(tmp_path):
    p = tmp_path / "observations.jsonl"
    for batch in range(3):
        wander.append_jsonl_capped(p, [{"n": batch * 10 + i} for i in range(10)], cap=15)
    lines = [json.loads(l) for l in p.read_text().strip().splitlines()]
    assert len(lines) == 15
    assert lines[-1] == {"n": 29}


def test_observation_goes_to_observations_file(tmp_path, monkeypatch):
    monkeypatch.setattr(wander, "STATE_DIR", tmp_path)
    wander._write_observation({"type": "observation", "landmark": "a red chair"})
    assert (tmp_path / "observations.jsonl").exists()
    assert not (tmp_path / "exploration.jsonl").exists()


def test_explore_live_requires_calibration(isolated_project):
    """Live explore (bypass-sudo, no calibration file) is blocked, rc 2."""
    from pxh.state import default_state
    state = default_state()
    state["confirm_motion_allowed"] = True
    state["roaming_allowed"] = True
    isolated_project["session_path"].write_text(json.dumps(state))
    env = isolated_project["env"].copy()
    env["PX_DRY"] = "0"
    import subprocess
    r = subprocess.run(["bin/px-wander", "--mode", "explore", "--duration", "30"],
                       capture_output=True, text=True, env=env, cwd=str(PROJECT_ROOT))
    payload = json.loads(r.stdout.strip().splitlines()[-1])
    assert payload["status"] == "blocked"
    assert "calibrat" in payload["reason"]
