"""Tests for px-wander explore mode helpers and exploration log."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent


def _load_wander_helpers():
    """Load a private copy of pxh.wander (not registered in sys.modules) so its
    module-level env-var reads pick up the patched env without mutating the
    real, cached pxh.wander module used elsewhere in the process."""
    import importlib.util

    wander_path = PROJECT_ROOT / "src" / "pxh" / "wander.py"
    spec = importlib.util.spec_from_file_location("pxh_wander_test_copy", wander_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return vars(module)


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
    write_obs(entry)
    path = tmp_path / "observations.jsonl"
    assert path.exists()
    assert not (tmp_path / "exploration.jsonl").exists()
    lines = path.read_text().strip().splitlines()
    parsed = json.loads(lines[0])
    assert parsed["type"] == "observation"
    assert parsed["landmark"] == "ginger cat on shelf"


def test_exploration_log_trim_atomic(wander, tmp_path):
    flush = wander["_flush_nav_entries"]
    path = tmp_path / "exploration.jsonl"
    existing = [json.dumps({"type": "nav", "i": i}) for i in range(95)]
    path.write_text("\n".join(existing) + "\n")
    new_entries = [{"type": "nav", "i": 95 + i} for i in range(10)]
    flush(new_entries, "e-test")
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 100


# -- Vision escalation: off by default, novelty-only, tightly budgeted --

def test_wander_vision_disabled_by_default(wander):
    # Fixture never sets PX_WANDER_VISION_ENABLED — the off-by-default case.
    assert wander["WANDER_VISION_ENABLED"] is False


def test_curiosity_trigger_vision_failure_no_rate_limit(wander):
    assert wander["VISION_FAIL_MAX"] == 3


def test_novelty_vision_budget_is_much_tighter_than_the_old_eager_one(wander):
    # Old routine-perception budget was 30s / 50-a-day; novelty escalation
    # must be rarer, not just relabeled.
    assert wander["NOVELTY_VISION_COOLDOWN_S"] == 300
    assert wander["NOVELTY_VISION_DAILY_CAP"] == 5


def test_daily_vision_cap(wander, tmp_path):
    check = wander["_check_daily_vision_cap"]
    inc = wander["_increment_vision_count"]
    meta = {"daily_vision_date": dt.date.today().isoformat(), "daily_vision_calls": 4}
    assert check(meta) is True
    meta = inc(meta)
    assert meta["daily_vision_calls"] == 5
    assert check(meta) is False


def test_vision_trigger_never_fires_when_disabled(wander):
    # PX_WANDER_VISION_ENABLED is unset in this fixture, so no combination of
    # local evidence — however close, however unexplained — may escalate.
    # This is the "autonomous wander can run indefinitely without invoking
    # Claude vision" guarantee, exercised at the decision-function level
    # rather than by driving the real hardware loop.
    trigger = wander["_vision_trigger"]
    sonar_readings = [0.0, 5.0, 10.0, 39.9, 40.0, 41.0, 100.0, 500.0, None]
    label_sets = [[], ["person"], ["dog", "cat"], ["chair"]]
    for _ in range(500):
        for sonar_cm in sonar_readings:
            for labels in label_sets:
                should_escalate, reason = trigger(labels, sonar_cm)
                assert should_escalate is False
                assert reason == ""


@pytest.fixture
def wander_vision_enabled(tmp_path):
    old_env = {}
    patch = {
        "PX_STATE_DIR": str(tmp_path),
        "PROJECT_ROOT": str(PROJECT_ROOT),
        "LOG_DIR": str(tmp_path / "logs"),
        "PX_DRY": "1",
        "PX_WANDER_VISION_ENABLED": "1",
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


def test_vision_trigger_fires_only_on_unexplained_proximity_when_enabled(wander_vision_enabled):
    trigger = wander_vision_enabled["_vision_trigger"]

    # The one genuine ambiguity: close AND Frigate has nothing to say.
    should_escalate, reason = trigger([], 39.0)
    assert should_escalate is True
    assert "39" in reason

    # Proximity Frigate already explains is not ambiguous — no escalation.
    should_escalate, _ = trigger(["chair"], 20.0)
    assert should_escalate is False

    # A new/any Frigate label alone, sonar far away, is not escalated either
    # — Frigate's own label is already the information.
    should_escalate, _ = trigger(["person"], 200.0)
    assert should_escalate is False

    # Right at/over the threshold does not escalate (strict less-than).
    should_escalate, _ = trigger([], 40.0)
    assert should_escalate is False

    # No sonar reading at all — nothing to be ambiguous about.
    should_escalate, _ = trigger([], None)
    assert should_escalate is False


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
    remember("Found a cat on the shelf to my right")
    notes = tmp_path / "notes.jsonl"
    assert notes.exists()
    entry = json.loads(notes.read_text().strip())
    assert "cat" in entry["note"]
    assert entry["source"] == "exploration"


def test_promoted_landmark_is_typed_as_something_spark_saw(wander, tmp_path):
    """A scene description comes from SPARK's own camera — the one durable
    writer that genuinely produces an `observation` (#170). It is capped below
    certainty all the same: a vision model can be wrong about what it sees."""
    from pxh import provenance
    wander["_auto_remember"]("Found a cat on the shelf to my right")
    entry = json.loads((tmp_path / "notes.jsonl").read_text().strip())
    p = provenance.read_provenance(entry)
    assert p["kind"] == "observation"
    assert p["confidence"] < 1.0


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
