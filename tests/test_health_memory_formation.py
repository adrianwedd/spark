"""Long-term memory formation health — pxh.health + bin/px-motd's rendering.

The nightly consolidation pass (px-mind → pxh.memory.maybe_consolidate) had no
health component at all: it was absent from health.STALE_AFTER_S, so
read_health() never named it, and a robot whose memory store had stopped
growing for days still reported `overall: ok`. These tests pin the three things
that made that possible — the component exists, its staleness window fits a
nightly job that fires inside a four-hour window, and "last formed" is measured
from the last *success* rather than the last report.

Inert by construction: conftest's autouse `_isolate_health_writes` redirects
health_dir() into tmp, so nothing here touches the live robot's state/health/.
"""
from __future__ import annotations

import datetime as dt
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

from pxh import health

CONS = health.CONSOLIDATION_COMPONENT

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _shift_updated(seconds: float) -> None:
    """Backdate the record's updated_ts — simulates silence, not failure."""
    path = health._component_path(CONS)
    rec = json.loads(path.read_text())
    ts = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)
    rec["updated_ts"] = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps(rec))


def _backdate_success(days: float) -> None:
    """Backdate only last_success_ts — the record keeps reporting."""
    path = health._component_path(CONS)
    rec = json.loads(path.read_text())
    ts = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    rec["last_success_ts"] = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps(rec))


# --- the component exists at all -------------------------------------------

def test_consolidation_is_a_known_component():
    # Before this, read_health() never mentioned consolidation — not as
    # "missing", not as anything. Silence read as health.
    assert CONS in health.KNOWN_COMPONENTS
    assert health.read_health()["components"][CONS]["status"] == "missing"


# --- status derivation ------------------------------------------------------

def test_fresh_success_reads_ok():
    health.record_success(CONS, detail={"status": "ok", "written": 6})
    assert health.read_health()["components"][CONS]["status"] == "ok"


def test_a_healthy_nightly_run_is_not_stale_at_28_hours():
    # The pass fires anywhere in 02:00-06:00 Hobart, so two good nights can sit
    # ~28h apart. px-blog's daily 86400 would call that stale every afternoon,
    # and an alarm that cries wolf daily is one nobody reads.
    health.record_success(CONS)
    _shift_updated(28 * 3600)
    assert health.read_health()["components"][CONS]["status"] == "ok"


def test_a_night_with_no_attempt_at_all_reads_stale():
    health.record_success(CONS)
    _shift_updated(31 * 3600)
    assert health.read_health()["components"][CONS]["status"] == "stale"


def test_repeated_failed_attempts_read_failing():
    for _ in range(3):
        health.record_failure(CONS, "claude exit 1")
    entry = health.read_health()["components"][CONS]
    assert entry["status"] == "failing"
    assert entry["last_error"] == "claude exit 1"


# --- memory_formation() -----------------------------------------------------

def test_reports_never_when_nothing_ever_consolidated():
    mem = health.memory_formation()
    assert mem["last_formed_ts"] is None
    assert mem["age_human"] == "never"
    assert mem["overdue"] is True


def test_ages_the_last_success():
    health.record_success(CONS, detail={"written": 4})
    _backdate_success(3)
    mem = health.memory_formation()
    assert mem["age_human"] == "3d ago"
    assert mem["overdue"] is True


def test_recent_formation_is_not_overdue():
    health.record_success(CONS)
    mem = health.memory_formation()
    assert mem["overdue"] is False
    assert mem["age_s"] < 5


def test_failed_attempts_do_not_refresh_last_formed():
    """The exact live failure shape, and why `overdue` is its own axis.

    record_failure refreshes updated_ts, so the component stays inside its
    staleness window; two failures keep it under the failing threshold. Status
    reads `degraded` — survivable-looking — while the store has not grown for
    four days. Only last_success_ts tells the truth.
    """
    health.record_success(CONS)
    _backdate_success(4)
    health.record_failure(CONS, "claude exit 1")
    health.record_failure(CONS, "consolidate quota reached (1/1)")

    assert health.read_health()["components"][CONS]["status"] == "degraded"
    mem = health.memory_formation()
    assert mem["overdue"] is True
    assert mem["age_human"] == "4d ago"
    assert mem["last_error"] == "consolidate quota reached (1/1)"


def test_read_health_promotes_memory_formation():
    health.record_success(CONS)
    assert health.read_health()["memory_formation"]["overdue"] is False


# --- summarize() ------------------------------------------------------------

def test_summary_reports_overdue_memory_even_when_nothing_is_unhealthy():
    # Deliberately non-empty on an otherwise clean bill: amnesia is not a
    # healthy state, and summarize() is what reaches SPARK's own reflection.
    out = health.summarize({"unhealthy": [], "components": {},
                            "memory_formation": health.memory_formation()})
    assert "long-term memory: never formed" in out


def test_summary_is_still_empty_when_memory_is_fresh():
    health.record_success(CONS)
    assert health.summarize({"unhealthy": [], "components": {},
                             "memory_formation": health.memory_formation()}) == ""


def test_summary_appends_memory_note_alongside_other_problems():
    out = health.summarize({
        "unhealthy": ["px-post"],
        "components": {"px-post": {"status": "failing",
                                   "consecutive_failures": 3,
                                   "last_error": "bluesky auth"}},
        "memory_formation": health.memory_formation(),
    })
    assert "px-post" in out
    assert "long-term memory" in out


def test_summary_tolerates_a_snapshot_written_before_this_field_existed():
    # px-mind publishes state/health.json and mind.py feeds that snapshot back
    # into summarize(); a daemon started before this change has no such key.
    assert health.summarize({"unhealthy": [], "components": {}}) == ""


# --- bin/px-motd rendering --------------------------------------------------

@pytest.fixture(scope="module")
def motd():
    """Import bin/px-motd as a module.

    Import-time work is constant definition and path clamping only — no
    subprocesses, no systemctl, no I2C. main() is never called.
    """
    path = PROJECT_ROOT / "bin" / "px-motd"
    # px-motd has no .py suffix, so spec_from_file_location cannot infer a
    # loader — name one explicitly rather than getting a None spec.
    spec = importlib.util.spec_from_loader(
        "px_motd_test_copy",
        importlib.machinery.SourceFileLoader("px_motd_test_copy", str(path)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _plain(text: str) -> str:
    import re
    return re.sub(r"\033\[[0-9;]*m", "", text)


def test_motd_line_reports_never_formed(motd):
    line = _plain(motd._memory_formation_line({"last_error": "claude exit 1"}))
    assert "long-term memory" in line
    assert "never formed" in line
    assert "claude exit 1" in line


def test_motd_line_reports_age_of_last_success(motd):
    ts = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)
    line = _plain(motd._memory_formation_line(
        {"last_success_ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ")}))
    assert "last formed 3d ago" in line


def test_motd_line_flags_anything_older_than_two_nights(motd):
    fresh = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=20)
    stale = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=60)
    fresh_line = motd._memory_formation_line(
        {"last_success_ts": fresh.strftime("%Y-%m-%dT%H:%M:%SZ")})
    stale_line = motd._memory_formation_line(
        {"last_success_ts": stale.strftime("%Y-%m-%dT%H:%M:%SZ")})
    assert motd.RED not in fresh_line
    assert motd.RED in stale_line


def test_motd_line_survives_an_unreadable_record(motd):
    # _json() returns {} on any failure; the banner must still render a line.
    assert "never formed" in _plain(motd._memory_formation_line({}))


def test_motd_line_ignores_updated_ts(motd):
    """px-motd must read last_success_ts, not the record's refresh time.

    A record whose only recent field is updated_ts is the failing-every-night
    case; rendering that as "last formed just now" would be the original bug
    reproduced one layer up.
    """
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = _plain(motd._memory_formation_line(
        {"updated_ts": now, "consecutive_failures": 2}))
    assert "never formed" in line


def test_motd_overdue_threshold_matches_health_module(motd):
    # Duplicated constant (px-motd imports no pxh module by design) — pin the
    # two together so they cannot silently drift.
    assert motd.MEMORY_OVERDUE_S == health.MEMORY_FORMATION_OVERDUE_S


# --- the in-flight job marker (#291) ----------------------------------------


def _job(started_ago_s: int, beat_ago_s: int) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    return {
        "status": "running", "pid": 1234, "attempt": 1,
        "started_ts": (now - dt.timedelta(seconds=started_ago_s)).isoformat(),
        "heartbeat_ts": (now - dt.timedelta(seconds=beat_ago_s)).isoformat(),
    }


def test_motd_shows_a_consolidation_in_flight(motd):
    ts = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=20)
    line = _plain(motd._memory_formation_line(
        {"last_success_ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ")},
        _job(started_ago_s=180, beat_ago_s=5)))
    assert "consolidating now for 3m" in line
    # The hint is additive: the honest age of the last real memory stays.
    assert "last formed 20h ago" in line


def test_motd_ignores_a_marker_whose_heartbeat_went_quiet(motd):
    # A marker outliving its owner is a lie; px-motd must not repeat it.
    ts = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=20)
    line = _plain(motd._memory_formation_line(
        {"last_success_ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ")},
        _job(started_ago_s=9000, beat_ago_s=8000)))
    assert "consolidating" not in line


def test_motd_line_renders_without_a_job_argument(motd):
    # The marker is absent almost all the time; the default must stay valid.
    assert "never formed" in _plain(motd._memory_formation_line({}))
    assert "never formed" in _plain(motd._memory_formation_line({}, {}))


def test_motd_job_heartbeat_threshold_matches_memory_module(motd):
    from pxh import memory
    assert motd.JOB_HEARTBEAT_STALE_S == memory.JOB_HEARTBEAT_STALE_S
