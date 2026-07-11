"""Tests for pxh.intention — goal/intention persistence (continuity sprint)."""
import datetime as dt
import json

import pytest

from pxh import intention


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))


def _read(persona="spark"):
    return json.loads(intention.intention_file(persona).read_text(encoding="utf-8"))


def test_set_goal_creates_active_intention():
    res = intention.set_goal("map the hallway over the next week")
    assert res["status"] == "ok"
    data = _read()
    assert data["active"]["goal"] == "map the hallway over the next week"
    assert data["active"]["status"] == "active"
    assert data["active"]["progress"] == []
    assert data["history"] == []


def test_set_goal_empty_text_is_error():
    assert intention.set_goal("   ")["status"] == "error"
    assert not intention.intention_file("spark").exists()


def test_set_goal_supersedes_existing():
    intention.set_goal("goal one")
    intention.set_goal("goal two")
    data = _read()
    assert data["active"]["goal"] == "goal two"
    assert len(data["history"]) == 1
    assert data["history"][0]["goal"] == "goal one"
    assert data["history"][0]["status"] == "superseded"


def test_update_goal_appends_progress():
    intention.set_goal("goal")
    res = intention.update_goal("first progress note")
    assert res["status"] == "ok"
    data = _read()
    assert data["active"]["progress"][0]["note"] == "first progress note"


def test_update_goal_without_active_is_noop_status():
    assert intention.update_goal("note")["status"] == "no_active_intention"


def test_update_goal_progress_capped_at_10():
    intention.set_goal("goal")
    for i in range(13):
        intention.update_goal(f"note {i}")
    progress = _read()["active"]["progress"]
    assert len(progress) == 10
    assert progress[-1]["note"] == "note 12"
    assert progress[0]["note"] == "note 3"


def test_complete_goal_archives_as_done():
    intention.set_goal("goal")
    res = intention.complete_goal("it worked")
    assert res["status"] == "ok"
    data = _read()
    assert data["active"] is None
    assert data["history"][0]["status"] == "done"
    assert data["history"][0]["progress"][-1]["note"] == "it worked"


def test_complete_goal_without_active():
    assert intention.complete_goal()["status"] == "no_active_intention"


def test_history_capped_at_10():
    for i in range(12):
        intention.set_goal(f"goal {i}")
    data = _read()
    assert len(data["history"]) == 10
    assert data["history"][-1]["goal"] == "goal 10"


def test_format_for_context_active_goal():
    intention.set_goal("learn the shape of the kitchen")
    intention.update_goal("scanned the north wall")
    ctx = intention.format_for_context()
    assert "learn the shape of the kitchen" in ctx
    assert "scanned the north wall" in ctx
    assert "set today" in ctx


def test_format_for_context_empty_without_goal():
    assert intention.format_for_context() == ""


def test_stale_goal_expires_with_one_shot_notice():
    intention.set_goal("old goal")
    # Backdate set_at 8 days
    f = intention.intention_file("spark")
    data = json.loads(f.read_text(encoding="utf-8"))
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data["active"]["set_at"] = old
    f.write_text(json.dumps(data), encoding="utf-8")

    first = intention.format_for_context()
    assert "expired" in first
    assert "old goal" in first
    # One-shot: second call returns "" and goal is archived as expired
    assert intention.format_for_context() == ""
    assert _read()["history"][0]["status"] == "expired"


def test_corrupt_file_recovers_to_empty():
    f = intention.intention_file("spark")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("{not json", encoding="utf-8")
    assert intention.format_for_context() == ""
    assert intention.set_goal("fresh start")["status"] == "ok"


def test_get_active_goal():
    assert intention.get_active_goal() == ""
    intention.set_goal("the goal")
    assert intention.get_active_goal() == "the goal"
