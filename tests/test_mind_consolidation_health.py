"""px-mind's consolidation tick reports its outcome to health.

Three outcomes, three different reports, and the distinction is the point:
"attempted and failed" must not read the same as "not due tonight", and
"not due" must not refresh the record every 60s forever.

Since #291 the pass runs on a background daemon thread, so `_consolidation_tick`
only *starts* it; `run_tick` below waits for the worker so these tests keep
asserting on the finished outcome rather than on a race.

Inert: conftest's autouse `_isolate_health_writes` redirects health_dir() into
tmp, `PX_STATE_DIR` is redirected per-test, and maybe_consolidate is stubbed —
no LLM call, no live state.
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


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Keep the job marker and the due-gate out of the live state dir."""
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    # The gate is exercised on its own in test_memory.py; here it is always
    # "due" so each test is about the outcome reporting.
    monkeypatch.setattr(mind.spark_memory, "consolidation_due",
                        lambda *a, **kw: None)
    mind._consolidation_worker = None
    yield
    worker = mind._consolidation_worker
    if worker is not None:
        worker.join(timeout=JOIN_TIMEOUT_S)
    mind._consolidation_worker = None


# Generous on purpose: this Pi routinely sits at a load average above 10, and a
# join margin sized to an idle machine turns a passing test into a flake.
JOIN_TIMEOUT_S = 60


def run_tick(session=SPARK, dry=False, timeout=JOIN_TIMEOUT_S):
    """Start the tick and wait for whatever worker it launched."""
    mind._consolidation_tick(session, dry)
    worker = mind._consolidation_worker
    if worker is not None:
        worker.join(timeout=timeout)
        assert not worker.is_alive(), "consolidation worker did not finish"
        mind._consolidation_worker = None


@pytest.fixture
def stub(monkeypatch):
    """Replace maybe_consolidate with a canned result."""
    def _set(result):
        monkeypatch.setattr(mind.spark_memory, "maybe_consolidate",
                            lambda **kw: result)
    return _set


def test_success_is_recorded(stub):
    stub({"status": "ok", "written": 6})
    run_tick()
    rec = _record()
    assert rec["success_count"] == 1
    assert rec["last_success_detail"]["written"] == 6


def test_skipped_counts_as_a_healthy_night(stub):
    # Too few thoughts in 24h is a correct outcome, not a failure —
    # maybe_consolidate marks the date done for it. Recording a success keeps a
    # quiet-but-working night from ageing into "stale".
    stub({"status": "skipped", "reason": "only 3 thoughts in 24h"})
    run_tick()
    rec = _record()
    assert rec["success_count"] == 1
    assert rec["consecutive_failures"] == 0


def test_failure_is_recorded_with_its_error(stub):
    stub({"status": "failed", "error": "claude exit 1"})
    run_tick()
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
    run_tick()
    assert not health._component_path(CONS).exists()


def test_dry_run_records_nothing(stub):
    # A dry run forms no memory, so it must not refresh the record. Since #291
    # the tick refuses to even launch a worker under dry, so nothing runs at
    # all — but the worker still branches on the status, so pin both.
    stub({"status": "dry"})
    run_tick(dry=True)
    assert not health._component_path(CONS).exists()
    mind._record_consolidation_outcome({"status": "dry"})
    assert not health._component_path(CONS).exists()


def test_a_raising_consolidation_is_recorded_not_swallowed(stub, monkeypatch):
    def _boom(**kw):
        raise RuntimeError("mailbox gone")
    monkeypatch.setattr(mind.spark_memory, "maybe_consolidate", _boom)
    run_tick()  # must not raise, and must not lose the error to the thread
    rec = _record()
    assert rec["consecutive_failures"] == 1
    assert "mailbox gone" in rec["last_error"]


def test_non_spark_persona_reports_nothing(stub):
    # Consolidation only runs for SPARK; GREMLIN's night is not a missed one.
    stub({"status": "ok", "written": 3})
    run_tick({"persona": "gremlin"})
    assert not health._component_path(CONS).exists()


def test_three_failed_nights_read_failing(stub):
    stub({"status": "failed", "error": "claude exit 1"})
    for _ in range(3):
        run_tick()
    assert health.read_health()["components"][CONS]["status"] == "failing"
