"""Unit tests for pxh.wander (extracted from bin/px-wander heredoc)."""
import datetime as dt
import json
import re
import time
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


# The robot_hat ADC returns a fixed power-on latch for ~0.75s after Picarx() is
# constructed. Observed on this hardware as [2571, 3085, 3599] — an exact
# arithmetic progression (gaps of 514, 514), which three independent physical
# sensors never produce. Tests use the real observed values deliberately.
ADC_LATCH = [2571.0, 3085.0, 3599.0]
REAL_FLOOR = [245.0, 493.0, 473.0]


def test_wait_for_grayscale_returns_first_live_reading():
    """The power-on latch is discarded; the first CHANGED reading is returned."""
    px = FakePx(grayscale=[ADC_LATCH] * 4 + [REAL_FLOOR] * 5)
    assert wander.wait_for_grayscale(px, settle_s=1.0, poll_s=0.0) == REAL_FLOOR


def test_wait_for_grayscale_stuck_adc_fails_closed():
    """An ADC that never updates yields None — callers must refuse to move."""
    px = FakePx(grayscale=[ADC_LATCH] * 500)
    assert wander.wait_for_grayscale(px, settle_s=0.2, poll_s=0.0) is None


def test_wait_for_grayscale_unreadable_sensor_fails_closed():
    px = FakePx(grayscale=[None] * 500)
    assert wander.wait_for_grayscale(px, settle_s=0.2, poll_s=0.0) is None


def test_wait_for_grayscale_failed_baseline_does_not_accept_the_latch():
    """Regression: if the BASELINE read fails, the next successful read is the
    latch — and with nothing to compare it against it was returned as live.
    An I2C error is most likely right after Picarx(), i.e. precisely when the
    latch is up, so this is the reachable path to a fabricated 'live' reading.

    Two Nones, not one: safe_grayscale(retries=1) absorbs a single OSError and
    returns the following reading, so only a both-attempts failure yields the
    None baseline this regression needs."""
    px = FakePx(grayscale=[None, None] + [ADC_LATCH] * 500)
    assert wander.wait_for_grayscale(px, settle_s=0.2, poll_s=0.0) is None


def test_wait_for_grayscale_failed_baseline_still_returns_a_real_change():
    """The rebaselining must not cost us a genuine reading: once a real floor
    value appears after the latch, it is still returned."""
    px = FakePx(grayscale=[None, None] + [ADC_LATCH] * 3 + [REAL_FLOOR] * 5)
    assert wander.wait_for_grayscale(px, settle_s=1.0, poll_s=0.0) == REAL_FLOOR


def test_calibrate_cliff_writes_reference(tmp_path):
    # Must survive the latch: calibration reference comes from the live reading,
    # never from the power-on latch that precedes it.
    px = FakePx(grayscale=[ADC_LATCH] * 2 + [[1000.0, 1100.0, 900.0]] * 3)
    cal = wander.calibrate_cliff(px, tmp_path, settle_s=1.0, poll_s=0.0)
    assert cal["floor_ref"] == [1000.0, 1100.0, 900.0]
    assert cal["cliff_ref"] == [650.0, 715.0, 585.0]
    on_disk = json.loads((tmp_path / "wander_calibration.json").read_text())
    assert on_disk == cal


def test_calibrate_cliff_refuses_to_persist_the_adc_latch(tmp_path):
    """Regression: calibrating on the latch stores a reference ~5x too high,
    which grounds the robot — or, with a plausible floor, silently defeats the
    guard. Refuse to write anything rather than persist fabricated data."""
    import pytest
    px = FakePx(grayscale=[ADC_LATCH] * 500)
    with pytest.raises(RuntimeError):
        wander.calibrate_cliff(px, tmp_path, settle_s=0.2, poll_s=0.0)
    assert not (tmp_path / "wander_calibration.json").exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_calibrate_cliff_read_failure_raises(tmp_path):
    import pytest
    with pytest.raises(RuntimeError):
        wander.calibrate_cliff(FakePx(grayscale=[None, None]), tmp_path,
                               settle_s=0.2, poll_s=0.0)


def test_calibrate_cliff_write_failure_cleans_up_tmp(tmp_path, monkeypatch):
    import pytest

    def _raise_replace(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(wander.os, "replace", _raise_replace)
    px = FakePx(grayscale=[ADC_LATCH] * 2 + [[1000.0, 1100.0, 900.0]] * 3)
    with pytest.raises(OSError):
        wander.calibrate_cliff(px, tmp_path, settle_s=1.0, poll_s=0.0)
    assert list(tmp_path.glob("*.tmp")) == []
    assert not (tmp_path / "wander_calibration.json").exists()


def test_load_calibration_missing_or_corrupt_is_none(tmp_path):
    assert wander.load_cliff_calibration(tmp_path) is None
    (tmp_path / "wander_calibration.json").write_text("{nope")
    assert wander.load_cliff_calibration(tmp_path) is None


def test_load_calibration_rejects_nan_and_nonpositive(tmp_path):
    """Python's json parser accepts NaN, and every `reading <= nan` is False —
    a NaN cliff_ref would load fine and silently disarm CliffGuard (fail-open).
    Same for a non-numeric or non-positive reference."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    p = tmp_path / "wander_calibration.json"
    for bad_ref in ('[NaN, NaN, NaN]', '[Infinity, 650, 650]',
                    '["650", "650", "650"]', '[0, 650, 650]', '[-1, 650, 650]',
                    '[true, true, true]'):
        p.write_text('{"floor_ref": [1000, 1000, 1000], "cliff_ref": %s, "ts": "%s"}'
                     % (bad_ref, now))
        assert wander.load_cliff_calibration(tmp_path) is None, bad_ref


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


def test_probe_turn_arc_back_reverses_with_same_steer(monkeypatch):
    """Retracing an arc in reverse requires the SAME steer angle (bicycle
    model: heading rate = v/L*tan(steer); negating v alone undoes the
    rotation). The old mirrored-steer recovery doubled the heading change,
    leaving probe labels pointing at the wrong real-world direction."""
    monkeypatch.setattr(wander.time, "sleep", lambda s: None)
    px = FakePx(grayscale=[[1000]*3]*20)
    # left probe 20cm, arc-back 40->55 (moved), right probe 90cm -> commit right
    px._dist = iter([20.0, 40.0, 55.0, 90.0])
    px.get_distance = lambda: next(px._dist, 90.0)
    wander.probe_turn(px, _guard(), prefer="left")
    bk = px.calls.index(("backward", wander.REVERSE_SPEED))
    steers_before_reverse = [c for c in px.calls[:bk]
                             if isinstance(c, tuple) and c[0] == "dir" and c[1] != 0]
    # the left probe steered -30; the arc-back must ALSO be -30, not +30
    assert steers_before_reverse[-1] == ("dir", -30)


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


def test_describe_scene_timeout_has_margin_over_claude():
    """The outer wander timeout must outlive tool-describe-scene's whole run:
    Claude call + photo capture (incl. an 8s stream pause) + bounded speech.
    Pin the RELATIONSHIP against the tool's actual constant, not a literal —
    a literal check stays green when someone raises CLAUDE_TIMEOUT."""
    src = (PROJECT_ROOT / "bin" / "tool-describe-scene").read_text(encoding="utf-8")
    m = re.search(r"^CLAUDE_TIMEOUT = (\d+)", src, re.M)
    assert m, "CLAUDE_TIMEOUT not found in bin/tool-describe-scene"
    claude_timeout = int(m.group(1))
    # 60s voice bound + ~20s photo/stream headroom
    assert wander.DESCRIBE_SCENE_TIMEOUT >= claude_timeout + 80
    # and the tool's voice call must itself be bounded
    assert re.search(r"TOOL_VOICE.*\n.*\n\s*check=False, env=env, timeout=\d+", src) or \
           re.search(r"timeout=60\)", src), "tool-voice call in describe-scene is unbounded"


def test_px_dry_env_forces_dry(monkeypatch, capsys, tmp_path):
    """PX_DRY=1 alone (no --dry-run flag) must produce a dry run."""
    monkeypatch.setenv("PX_DRY", "1")
    monkeypatch.setattr(wander, "STATE_DIR", tmp_path)
    rc = wander.main(["--steps", "1", "--quiet"])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 0
    assert out["dry"] is True


def test_live_avoid_requires_motion_confirmed(monkeypatch, capsys, tmp_path):
    """A live avoid run must respect confirm_motion_allowed even when invoked
    directly (tool-wander gates it, px-wander itself previously did not)."""
    monkeypatch.delenv("PX_DRY", raising=False)
    monkeypatch.setattr(wander, "STATE_DIR", tmp_path)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    (tmp_path / "wander_calibration.json").write_text(json.dumps(
        {"floor_ref": [1000, 1000, 1000], "cliff_ref": [650, 650, 650], "ts": now}))
    monkeypatch.setenv("PX_SESSION_PATH", str(tmp_path / "session.json"))
    (tmp_path / "session.json").write_text(json.dumps({"confirm_motion_allowed": False}))
    rc = wander.main(["--steps", "1"])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 2
    assert out["status"] == "blocked"
    assert "motion" in out["reason"]


def test_dry_explore_is_silent_and_stateless(monkeypatch, capsys, tmp_path):
    """PX_DRY / --dry-run must skip ALL audio and must not consume the live
    explore cooldown, lifetime counters, or the px-alive guard file."""
    spoken = []
    monkeypatch.setattr(wander, "speak", lambda t: spoken.append(t))
    monkeypatch.setattr(wander.time, "sleep", lambda s: None)
    monkeypatch.setattr(wander, "STATE_DIR", tmp_path)
    monkeypatch.setenv("PX_SESSION_PATH", str(tmp_path / "session.json"))
    rc = wander.main(["--mode", "explore", "--duration", "30", "--dry-run"])
    assert rc == 0
    assert spoken == []
    assert not (tmp_path / "exploring.json").exists()
    assert not (tmp_path / "exploration_meta.json").exists()


def test_exploring_refresher_rewrites_guard_file(monkeypatch, tmp_path):
    """px-alive ignores exploring.json once its mtime is >60s old; the
    refresher must keep rewriting it (fresh mtime, same pid) while live."""
    monkeypatch.setattr(wander, "STATE_DIR", tmp_path)
    monkeypatch.setattr(wander, "EXPLORING_REFRESH_S", 0.05)
    r = wander._ExploringRefresher(pid=1234, started="2026-01-01T00:00:00+00:00")
    r.start()
    try:
        deadline = time.monotonic() + 2.0
        path = tmp_path / "exploring.json"
        while time.monotonic() < deadline and not path.exists():
            time.sleep(0.02)
        data = json.loads(path.read_text())
        assert data == {"active": True, "pid": 1234,
                        "started": "2026-01-01T00:00:00+00:00"}
    finally:
        r.stop()
        r.join(timeout=2)
        assert not r.is_alive()


# ---------------------------------------------------------------------------
# Task 8: Frigate freshness + non-blocking speech
# ---------------------------------------------------------------------------


class _FakeFrigateResp:
    def __init__(self, payload=b"[]"):
        self._payload = payload
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._payload


def _capture_frigate_url(monkeypatch, **kwargs):
    seen = {}
    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        return _FakeFrigateResp()
    monkeypatch.setattr(wander.urllib.request, "urlopen", fake_urlopen)
    wander._query_frigate(**kwargs)
    return seen["url"]


def test_query_frigate_passes_after(monkeypatch):
    """A run-start epoch is forwarded so only events from *this* run come back."""
    url = _capture_frigate_url(monkeypatch, after_epoch=1753747200.9)
    assert "after=1753747200" in url


def test_query_frigate_after_url_is_well_formed(monkeypatch):
    """The substring check above would pass on a malformed URL — pin the structure."""
    import urllib.parse as _up
    url = _capture_frigate_url(monkeypatch, after_epoch=1753747200.9)
    assert url.count("?") == 1, f"malformed query string: {url}"
    parsed = _up.urlparse(url)
    assert parsed.scheme in ("http", "https")
    assert parsed.netloc
    assert parsed.path == "/api/events"
    q = _up.parse_qs(parsed.query, strict_parsing=True)
    # truncation, not rounding: 1753747200.9 -> ...200 (never ...201)
    assert q["after"] == ["1753747200"]
    # pre-existing params survive
    assert "cameras" in q and "limit" in q and "min_score" in q


def test_query_frigate_omits_after_when_not_given(monkeypatch):
    """Default stays backwards-compatible: no after= bound at all."""
    url = _capture_frigate_url(monkeypatch)
    assert "after" not in url


def test_explore_loop_bounds_frigate_by_run_start():
    """Every _query_frigate call in the explore loop must carry the run start."""
    import inspect as _inspect
    import re as _re
    src = _inspect.getsource(wander.main)
    calls = _re.findall(r"_query_frigate\(([^)]*)\)", src)
    assert calls, "no _query_frigate calls found in wander.main"
    for args in calls:
        assert "after_epoch=start_time" in args, f"unbounded frigate query: _query_frigate({args})"


def test_speak_never_blocks_on_audio(monkeypatch):
    """speak() is fire-and-forget: the drive loop must not wait on aplay/espeak."""
    waits = []

    class _FakeStdout:
        def close(self): pass

    class _FakeProc:
        def __init__(self, *a, **k):
            self.stdout = _FakeStdout()
        def wait(self, *a, **k):
            waits.append(1)
            return 0

    monkeypatch.setattr(wander.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(wander.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(wander.subprocess, "Popen", _FakeProc)
    wander.speak("hello")
    assert waits == [], "speak() waited on a child process — it must not block the drive loop"


def test_speak_spawns_nothing_without_aplay(monkeypatch):
    """No aplay means no reader for espeak's pipe.

    speak() never waits on its children, so an undrained pipe fills at ~64KB
    and blocks espeak forever — one stuck process per call. With no sink,
    nothing may be spawned at all.
    """
    spawned = []
    monkeypatch.setattr(wander.shutil, "which",
                        lambda name: None if name == "aplay" else f"/usr/bin/{name}")
    monkeypatch.setattr(wander.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(wander.subprocess, "Popen",
                        lambda *a, **k: spawned.append(a) or (_ for _ in ()).throw(
                            AssertionError("speak() spawned espeak with no aplay to drain it")))
    wander.speak("hello")
    assert spawned == []
