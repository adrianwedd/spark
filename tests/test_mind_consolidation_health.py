"""px-mind's consolidation tick reports its outcome to health.

Three outcomes, three different reports, and the distinction is the point:
"attempted and failed" must not read the same as "not due tonight", and
"not due" must not refresh the record every 60s forever.

Inert: conftest's autouse `_isolate_health_writes` redirects health_dir() into
tmp, and maybe_consolidate is stubbed — no LLM call, no live state.
"""
from __future__ import annotations

import json

import pytest

import pxh.mind as mind
from pxh import health

CONS = health.CONSOLIDATION_COMPONENT
SPARK = {"persona": "spark"}


def _record() -> dict:
    path = health._component_path(CONS)
    return json.loads(path.read_text()) if path.exists() else {}


@pytest.fixture
def stub(monkeypatch):
    """Replace maybe_consolidate with a canned result."""
    def _set(result):
        monkeypatch.setattr(mind.spark_memory, "maybe_consolidate",
                            lambda **kw: result)
    return _set


def test_success_is_recorded(stub):
    stub({"status": "ok", "written": 6})
    mind._consolidation_tick(SPARK, dry=False)
    rec = _record()
    assert rec["success_count"] == 1
    assert rec["last_success_detail"]["written"] == 6


def test_skipped_counts_as_a_healthy_night(stub):
    # Too few thoughts in 24h is a correct outcome, not a failure —
    # maybe_consolidate marks the date done for it. Recording a success keeps a
    # quiet-but-working night from ageing into "stale".
    stub({"status": "skipped", "reason": "only 3 thoughts in 24h"})
    mind._consolidation_tick(SPARK, dry=False)
    rec = _record()
    assert rec["success_count"] == 1
    assert rec["consecutive_failures"] == 0


def test_failure_is_recorded_with_its_error(stub):
    stub({"status": "failed", "error": "claude exit 1"})
    mind._consolidation_tick(SPARK, dry=False)
    rec = _record()
    assert rec["consecutive_failures"] == 1
    assert rec["last_error"] == "claude exit 1"
    assert "success_count" not in rec


def test_not_due_records_nothing(stub):
    """None means outside the window, already done today, or dry.

    Writing a success here would mean the component reported healthy on every
    60s tick whether or not memory ever formed — the same blindness this whole
    change exists to remove. Staleness already covers true silence.
    """
    stub(None)
    mind._consolidation_tick(SPARK, dry=False)
    assert not health._component_path(CONS).exists()


def test_dry_run_records_nothing(stub):
    # A dry run forms no memory, so it must not refresh the record. (Live
    # maybe_consolidate returns None under dry; this pins the belt as well as
    # the braces, since the status is what the tick actually branches on.)
    stub({"status": "dry"})
    mind._consolidation_tick(SPARK, dry=True)
    assert not health._component_path(CONS).exists()


def test_a_raising_consolidation_is_recorded_not_swallowed(stub, monkeypatch):
    def _boom(**kw):
        raise RuntimeError("mailbox gone")
    monkeypatch.setattr(mind.spark_memory, "maybe_consolidate", _boom)
    mind._consolidation_tick(SPARK, dry=False)  # must not raise
    rec = _record()
    assert rec["consecutive_failures"] == 1
    assert "mailbox gone" in rec["last_error"]


def test_non_spark_persona_reports_nothing(stub):
    # Consolidation only runs for SPARK; GREMLIN's night is not a missed one.
    stub({"status": "ok", "written": 3})
    mind._consolidation_tick({"persona": "gremlin"}, dry=False)
    assert not health._component_path(CONS).exists()


def test_three_failed_nights_read_failing(stub):
    stub({"status": "failed", "error": "claude exit 1"})
    for _ in range(3):
        mind._consolidation_tick(SPARK, dry=False)
    assert health.read_health()["components"][CONS]["status"] == "failing"
