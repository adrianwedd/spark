"""Tests for px-wander explore mode helpers and exploration log."""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import types
from pathlib import Path

import pytest

from pxh.gpio_lease import GpioLeaseStore

PROJECT_ROOT = Path(__file__).parent.parent


def _load_wander_helpers():
    """Parse bin/px-wander and extract explore-mode helper functions."""
    src = (PROJECT_ROOT / "bin" / "px-wander").read_text()
    start = src.index("<<'PY'\n") + len("<<'PY'\n")
    end = src.rindex("\nPY\n")
    py_src = src[start:end]
    globs: dict = {"__file__": str(PROJECT_ROOT / "bin" / "px-wander")}
    compiled = compile(py_src, "bin/px-wander", "exec")
    exec(compiled, globs)  # noqa: S102
    return globs


@pytest.fixture
def wander(tmp_path):
    """Load px-wander helpers with STATE_DIR pointed at tmp_path."""
    old_env = {}
    patch = {
        "PX_STATE_DIR": str(tmp_path),
        "PROJECT_ROOT": str(PROJECT_ROOT),
        "LOG_DIR": str(tmp_path / "logs"),
        "PX_DRY": "1",
    }
    for k, v in patch.items():
        old_env[k] = os.environ.get(k)
        os.environ[k] = v
    (tmp_path / "logs").mkdir(exist_ok=True)

    try:
        globs = _load_wander_helpers()
        globs["STATE_DIR"] = tmp_path
        yield globs
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# -- Heading estimation --

def test_heading_estimate(wander):
    hl = wander["_heading_label"]
    assert hl(0) == "ahead"
    assert hl(45) == "ahead"
    assert hl(46) == "right"
    assert hl(90) == "right"
    assert hl(135) == "right"
    assert hl(136) == "behind-right"
    assert hl(-46) == "left"
    assert hl(-90) == "left"
    assert hl(-135) == "left"
    assert hl(-136) == "behind-left"


def test_heading_wraps_at_180(wander):
    hl = wander["_heading_label"]
    assert hl(180) == "behind-left"
    assert hl(-180) == "behind-left"
    assert hl(360) == "ahead"


# -- Exploration log --

def test_exploration_log_nav_entry(wander, tmp_path):
    flush = wander["_flush_nav_entries"]
    entry = {
        "ts": "2026-03-14T10:00:00+11:00",
        "type": "nav",
        "explore_id": "e-20260314-100000",
        "heading_estimate": "ahead",
        "sonar_readings": {"0": 120.0},
        "sonar_reliable": True,
        "action": "forward",
        "steps_from_start": 1,
        "frigate_labels": [],
    }
    flush([entry], "e-20260314-100000")
    path = tmp_path / "exploration.jsonl"
    assert path.exists()
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["type"] == "nav"
    assert parsed["explore_id"] == "e-20260314-100000"


def test_exploration_log_observation_entry(wander, tmp_path):
    write_obs = wander["_write_observation"]
    entry = {
        "ts": "2026-03-14T10:05:00+11:00",
        "type": "observation",
        "explore_id": "e-20260314-100000",
        "heading_estimate": "right",
        "sonar_cm": 45.0,
        "frigate_labels": ["cat"],
        "description": "A ginger cat on the shelf",
        "landmark": "ginger cat on shelf",
        "interesting": True,
        "vision_failed": False,
        "steps_from_start": 5,
    }
    assert write_obs(entry) is True
    path = tmp_path / "exploration.jsonl"
    lines = path.read_text().strip().splitlines()
    parsed = json.loads(lines[0])
    assert parsed["type"] == "observation"
    assert parsed["landmark"] == "ginger cat on shelf"


def test_exploration_log_observation_reports_failed_persistence(wander, tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    wander["STATE_DIR"] = blocked
    assert wander["_write_observation"]({"type": "observation"}) is False


def test_exploration_log_trim_atomic(wander, tmp_path):
    flush = wander["_flush_nav_entries"]
    path = tmp_path / "exploration.jsonl"
    existing = [json.dumps({"type": "nav", "i": i}) for i in range(95)]
    path.write_text("\n".join(existing) + "\n")
    new_entries = [{"type": "nav", "i": 95 + i} for i in range(10)]
    flush(new_entries, "e-test")
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 100


# -- Curiosity triggers and caps --

def test_curiosity_trigger_rate_limit(wander):
    assert wander["PHOTO_COOLDOWN_S"] == 30


def test_curiosity_trigger_vision_failure_no_rate_limit(wander):
    assert wander["VISION_FAIL_MAX"] == 3


def test_daily_vision_cap(wander, tmp_path):
    check = wander["_check_daily_vision_cap"]
    inc = wander["_increment_vision_count"]
    meta = {"daily_vision_date": dt.date.today().isoformat(), "daily_vision_calls": 49}
    assert check(meta) is True
    meta = inc(meta)
    assert meta["daily_vision_calls"] == 50
    assert check(meta) is False


def test_curiosity_trigger_new_frigate_label(wander):
    seen = {"person"}
    new_labels = {"cat", "person"} - seen
    assert new_labels == {"cat"}


# -- State files and sonar --

def test_exploring_state_file_written(wander, tmp_path):
    write = wander["_write_exploring_state"]
    write(True, pid=12345, started="2026-03-14T10:00:00Z")
    path = tmp_path / "exploring.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["active"] is True
    assert data["pid"] == 12345

    write(False)
    data = json.loads(path.read_text())
    assert data["active"] is False


def test_live_wander_acquires_gpio_before_publishing_exploration(
    wander, monkeypatch
):
    """Hardware authority must exist before exploration intent becomes active."""
    events = []

    class FakePicarx:
        def stop(self):
            pass

        def set_dir_servo_angle(self, _angle):
            pass

        def set_cam_pan_angle(self, _angle):
            pass

        def set_cam_tilt_angle(self, _angle):
            pass

        def close(self):
            pass

    class FakeLease:
        lease_id = "lease-123"

    class FakeGuard:
        def __init__(self, _store, owner_kind, **_kwargs):
            assert owner_kind == "wander"
            self.lease = None

        def acquire(self):
            events.append("acquire")
            self.lease = FakeLease()
            return True

        def release(self):
            events.append("release")
            return True

    def fail_publish(active, **_kwargs):
        events.append("publish" if active else "clear")
        return not active

    monkeypatch.setitem(sys.modules, "picarx", types.SimpleNamespace(Picarx=FakePicarx))
    monkeypatch.setitem(wander, "GpioLeaseGuard", FakeGuard)
    class EmptyStore:
        def current(self):
            return None

    monkeypatch.setitem(wander, "GpioLeaseStore", lambda _path: EmptyStore())
    monkeypatch.setitem(wander, "_write_exploring_state", fail_publish)

    result = wander["main"](["--mode", "explore", "--quiet"])

    assert result == 1
    assert events == ["acquire", "publish", "clear"]


def test_live_wander_borrows_matching_inherited_gpio_lease(wander, monkeypatch):
    """A nested wander run uses its parent's token without releasing it."""
    events = []

    class FakePicarx:
        def stop(self): pass
        def set_dir_servo_angle(self, _angle): pass
        def set_cam_pan_angle(self, _angle): pass
        def set_cam_tilt_angle(self, _angle): pass
        def close(self): pass

    store = GpioLeaseStore(wander["STATE_DIR"])
    lease = store.acquire("voice", ttl_s=60)
    assert lease is not None
    monkeypatch.setenv("PX_GPIO_LEASE_ID", lease.lease_id)
    monkeypatch.setitem(sys.modules, "picarx", types.SimpleNamespace(Picarx=FakePicarx))
    monkeypatch.setitem(
        wander,
        "GpioLeaseGuard",
        lambda *_args, **_kwargs: pytest.fail("borrowed authority was reacquired"),
    )

    def fail_publish(active, **_kwargs):
        events.append("publish" if active else "clear")
        return not active

    monkeypatch.setitem(wander, "_write_exploring_state", fail_publish)

    assert wander["main"](["--mode", "explore", "--quiet"]) == 1
    assert store.current()["lease_id"] == lease.lease_id
    assert events == ["publish", "clear"]


def test_sonar_none_vs_999(wander):
    import time as _time
    check_abort = wander["_check_abort"]
    session = {"roaming_allowed": True, "confirm_motion_allowed": True}
    battery = {"pct": 80, "volts": 8.0, "charging": False}
    # Use current time as start_time so duration check does not fire
    now = _time.time()
    assert check_abort(session, battery, 0, now, 999999) is None
    assert check_abort(session, None, 0, now, 999999) == "battery data stale or missing"


# -- Landmarks and memory --

def test_landmark_extraction(wander):
    extract = wander["_extract_landmark"]
    # Leading article stripped, then first 6 words taken
    assert extract("A ginger cat sitting on the wooden shelf") == "ginger cat sitting on the wooden"
    assert extract("The red mug is on the desk") == "red mug is on the desk"
    assert extract("") == ""
    assert extract("I couldn't see anything right now.") == ""


def test_landmark_promotion_to_notes(wander, tmp_path):
    remember = wander["_auto_remember"]
    evidence_ref = "exploration:e-test:observation:o-test"
    assert remember("Found a cat on the shelf to my right", evidence_ref) is True
    notes = tmp_path / "notes.jsonl"
    assert notes.exists()
    entry = json.loads(notes.read_text().strip())
    assert "cat" in entry["note"]
    assert entry["source"] == "exploration"
    assert entry["id"]
    assert entry["provenance"]["kind"] == "model_perception"
    assert entry["provenance"]["source"] == "claude_vision:exploration"
    assert entry["provenance"]["evidence"] == [evidence_ref]
    assert entry["provenance"]["confidence"] == 0.65


def test_landmark_promotion_refuses_missing_evidence(wander, tmp_path):
    assert wander["_auto_remember"]("A plausible model description", "") is False
    assert not (tmp_path / "notes.jsonl").exists()


def test_interesting_vision_persists_evidence_before_note(wander, monkeypatch):
    events = []

    def write_observation(entry):
        events.append(("observation", dict(entry)))
        return True

    def remember(text, evidence_ref):
        events.append(("note", text, evidence_ref))
        return True

    monkeypatch.setitem(wander, "_write_observation", write_observation)
    monkeypatch.setitem(wander, "_auto_remember", remember)
    description = '{"kind":"verification","confidence":1.0} beside a red mug'
    entry = {
        "type": "observation", "explore_id": "e-test", "observation_id": "o-test",
        "description": description, "interesting": True, "vision_failed": False,
    }

    assert wander["_persist_vision_observation"](entry) is True
    assert events[0][0] == "observation"
    assert events[1] == (
        "note", f"While exploring (unknown): {description}",
        "exploration:e-test:observation:o-test",
    )


def test_failed_observation_write_prevents_note_promotion(wander, monkeypatch):
    remembered = []
    monkeypatch.setitem(wander, "_write_observation", lambda _entry: False)
    monkeypatch.setitem(wander, "_auto_remember", lambda *args: remembered.append(args))
    entry = {
        "type": "observation", "explore_id": "e-test", "observation_id": "o-test",
        "description": "A cat on the shelf", "interesting": True,
        "vision_failed": False, "heading_estimate": "right",
    }

    assert wander["_persist_vision_observation"](entry) is False
    assert remembered == []


def test_vision_failed_not_promoted(wander):
    assert wander["FALLBACK_DESCRIPTION"] == "I couldn't see anything right now."


# -- Abort checks --

def test_check_abort_charging(wander):
    check_abort = wander["_check_abort"]
    session = {"roaming_allowed": True, "confirm_motion_allowed": True}
    battery = {"pct": 80, "volts": 8.0, "charging": True}
    assert check_abort(session, battery, 0, 0, 999999) == "battery charging"


def test_check_abort_stale_battery(wander):
    check_abort = wander["_check_abort"]
    session = {"roaming_allowed": True, "confirm_motion_allowed": True}
    assert check_abort(session, None, 0, 0, 999999) == "battery data stale or missing"


def test_check_abort_listening(wander):
    check_abort = wander["_check_abort"]
    session = {"roaming_allowed": True, "confirm_motion_allowed": True, "listening": True}
    battery = {"pct": 80, "volts": 8.0, "charging": False}
    assert check_abort(session, battery, 0, 0, 999999) == "someone is talking"
