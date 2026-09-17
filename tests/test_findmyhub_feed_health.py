"""A frozen upstream feed must not read the same as a working arrival detector.

Measured on the robot 2026-09-18: `state/findmyhub.json` had a file age of 47 s
(the cron rewrites it every ~5 min) while adrian's *fix* inside it was 15.6 h old,
obi_chipolo's 4.4 days, and laura was erroring. The file-level gate passes, the
per-fix gate correctly holds every state, and the outcome is **zero arrivals** —
which is indistinguishable from "the flap is fixed" unless the feed's own health
is recorded (#305).
"""
from __future__ import annotations

import json
import time

import pxh.mind as mind

STALE_H = mind.FINDMYHUB_FIX_STALE_S / 3600.0


def _tracker(*, age_s: float, accuracy_m: float = 30.0) -> dict:
    return {
        "lat": mind.HOME_LAT,
        "lon": mind.HOME_LON,
        "accuracy_m": accuracy_m,
        "ts": time.time() - age_s,
    }


def _feed(tmp_path, trackers: dict, *, file_age_s: float = 0.0):
    path = tmp_path / "findmyhub.json"
    path.write_text(
        json.dumps({"ts": time.time() - file_age_s, "trackers": trackers}),
        encoding="utf-8",
    )
    return path


def _capture_health(monkeypatch):
    calls: dict[str, list] = {"success": [], "failure": []}
    monkeypatch.setattr(
        mind.health_mod,
        "record_success",
        lambda component, **kwargs: calls["success"].append((component, kwargs)),
    )
    monkeypatch.setattr(
        mind.health_mod,
        "record_failure",
        lambda component, error, **kwargs: calls["failure"].append((component, error)),
    )
    return calls


def setup_function(_fn):
    mind._findmyhub_feed_reason = None


def test_every_fix_stale_is_a_failure_naming_the_ages(tmp_path, monkeypatch):
    """The production shape: a current file full of ancient fixes."""
    monkeypatch.setattr(
        mind,
        "FINDMYHUB_FILE",
        _feed(
            tmp_path,
            {"adrian": _tracker(age_s=15.6 * 3600), "obi_chipolo": _tracker(age_s=103.7 * 3600)},
        ),
    )
    calls = _capture_health(monkeypatch)

    assert mind._read_findmyhub() != {}  # the read still returns them; the latch holds
    assert calls["success"] == []
    assert len(calls["failure"]) == 1
    component, reason = calls["failure"][0]
    assert component == "findmyhub"
    assert "every tracked fix is stale" in reason
    assert "adrian 15.6 h" in reason and "obi_chipolo 103.7 h" in reason
    assert "holding, not working" in reason
    assert mind._findmyhub_feed_reason == reason


def test_one_fresh_fix_is_enough_for_success(tmp_path, monkeypatch):
    """A tracker that *is* being seen keeps detection live for the others."""
    monkeypatch.setattr(
        mind,
        "FINDMYHUB_FILE",
        _feed(
            tmp_path,
            {"adrian": _tracker(age_s=15.6 * 3600), "obi_chipolo": _tracker(age_s=120)},
        ),
    )
    calls = _capture_health(monkeypatch)

    mind._read_findmyhub()
    assert calls["failure"] == []
    assert calls["success"] == [
        ("findmyhub", {"min_interval_s": mind.FINDMYHUB_STALE_S})
    ], "the health record is an fsync'd write; it must not happen every cycle"
    assert mind._findmyhub_feed_reason is None


def test_a_stale_file_blames_the_fetcher_not_the_trackers(tmp_path, monkeypatch):
    monkeypatch.setattr(
        mind,
        "FINDMYHUB_FILE",
        _feed(tmp_path, {"adrian": _tracker(age_s=60)}, file_age_s=3 * 3600),
    )
    calls = _capture_health(monkeypatch)

    assert mind._read_findmyhub() == {}
    assert len(calls["failure"]) == 1
    assert "the fetcher has stopped" in calls["failure"][0][1]


def test_an_absent_feed_records_nothing_at_all(tmp_path, monkeypatch):
    """Not configured is not broken — and a false alarm is worse than silence."""
    monkeypatch.setattr(mind, "FINDMYHUB_FILE", tmp_path / "nope.json")
    calls = _capture_health(monkeypatch)

    assert mind._read_findmyhub() == {}
    assert calls == {"success": [], "failure": []}
    assert mind._findmyhub_feed_reason is None


def test_a_feed_of_errors_is_not_reported_as_staleness(tmp_path, monkeypatch):
    monkeypatch.setattr(
        mind,
        "FINDMYHUB_FILE",
        _feed(tmp_path, {"laura": {"error": "timeout after 45s"}}),
    )
    calls = _capture_health(monkeypatch)

    assert mind._read_findmyhub() == {}
    assert len(calls["failure"]) == 1
    reason = calls["failure"][0][1]
    assert "no tracker returned a usable fix" in reason and "laura" in reason
    assert "stale" not in reason, "an error is not a stale fix; do not blur them"


def test_recording_health_never_breaks_the_read(tmp_path, monkeypatch):
    """Reporting is never load-bearing: a health failure must not lose the data."""
    monkeypatch.setattr(
        mind, "FINDMYHUB_FILE", _feed(tmp_path, {"adrian": _tracker(age_s=10)})
    )

    def boom(*_args, **_kwargs):
        raise RuntimeError("health store exploded")

    monkeypatch.setattr(mind.health_mod, "record_success", boom)
    result = mind._read_findmyhub()
    assert list(result) == ["adrian"]
