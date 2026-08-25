"""Nightly consolidation must be able to succeed (#291).

Three defects made success structurally impossible, and each gets its own
section here:

1. **The pass ran inline on px-mind's ~60s awareness tick.** Its declared
   budget is 600s — twice px-mind's own 300s health-staleness window — so
   honouring the budget and keeping the mind loop alive were mutually
   exclusive. Now it runs on a daemon thread with a pid-keyed job marker.
2. **An ad-hoc `timeout=180` overrode the declared 600s deadline.** The tighter
   number always won, so the declared budget was never once reachable.
3. **Attempt 2 could not be spent.** `MAX_ATTEMPTS_PER_DAY` promised two tries
   a night while the `consolidate` quota was 1 and its type cooldown 20 hours.

Inert by construction: no service is touched, no tmux session is reached, no
Claude call is made, and every duration in here is synthetic — a "600s"
deadline is asserted as a *plumbed number*, never waited for. The one real
thread that runs blocks on an Event the test controls.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import time

import pytest

import pxh.mind as mind
from pxh import brain, claude_session, memory

SPARK = {"persona": "spark"}
UTC = dt.timezone.utc

# This Pi routinely sits above a load average of 10; margins are sized to that
# rather than to an idle machine (a join that finished in 0.06s idle has been
# observed taking 12s here under load).
JOIN_TIMEOUT_S = 60
# Used only where a *fresh* write has to be distinguishable from a backdated
# one — never as a "the tick was fast enough" budget. See the note in
# test_tick_stays_responsive_while_consolidation_exceeds_300s.
FRESH_WITHIN_S = 120.0


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Keep the job marker, the meta file and the worker global out of live state."""
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    mind._consolidation_worker = None
    yield
    worker = mind._consolidation_worker
    if worker is not None:
        worker.join(timeout=JOIN_TIMEOUT_S)
    mind._consolidation_worker = None


@pytest.fixture
def due(monkeypatch):
    """Report consolidation as due, without depending on the wall clock."""
    monkeypatch.setattr(mind.spark_memory, "consolidation_due",
                        lambda *a, **kw: None)


def _backdate_marker(started_ago_s: float, beat_ago_s: float = 0.0) -> None:
    """Age the in-flight marker synthetically. No test ever waits real time."""
    now = dt.datetime.now(UTC)
    job = memory.read_consolidation_job()
    job["started_ts"] = (now - dt.timedelta(seconds=started_ago_s)).isoformat()
    job["heartbeat_ts"] = (now - dt.timedelta(seconds=beat_ago_s)).isoformat()
    memory._write_consolidation_job(job)


# ---------------------------------------------------------------------------
# 1. The tick cannot be stalled by a long consolidation
# ---------------------------------------------------------------------------

def test_tick_stays_responsive_while_consolidation_exceeds_300s(due, monkeypatch):
    """px-mind's loop keeps running while a >300s consolidation is in flight.

    300s is not an arbitrary number: it is `health.STALE_AFTER_S["px-mind"]`,
    the point at which px-mind itself reads stale. Under the old inline call a
    consolidation allowed to use its real 600s budget guaranteed that reading —
    no awareness snapshot, no reflection, no battery check, for ten minutes.

    The "600 seconds" here is synthetic: the worker blocks on an Event the test
    owns, and the marker is backdated. Nothing sleeps.

    The proof is structural rather than a stopwatch, deliberately. The gate is
    held shut for the whole loop below, so a tick that waited on the worker
    could not return **at all** — reaching the end of the loop is itself the
    assertion. A wall-clock budget would prove nothing extra and would fail on
    this host for the wrong reason: it is the live robot, routinely above a load
    average of 10, where a single fsync has been measured taking tens of
    seconds. Slow is not the property under test; blocked is.
    """
    gate = threading.Event()
    calls = []

    def _slow(**kw):
        calls.append(kw)
        gate.wait(JOIN_TIMEOUT_S)
        return {"status": "ok", "written": 1}

    monkeypatch.setattr(mind.spark_memory, "maybe_consolidate", _slow)
    try:
        mind._consolidation_tick(SPARK, dry=False)
        worker = mind._consolidation_worker
        assert worker is not None and worker.is_alive()

        # Five further ticks, each standing in for one 60s awareness cycle,
        # with the run backdated well past px-mind's own 300s staleness window
        # and past the 600s deadline.
        for i in range(5):
            _backdate_marker(started_ago_s=60 * (i + 1) + 600)
            mind._consolidation_tick(SPARK, dry=False)
            assert not gate.is_set()
            assert worker.is_alive(), (
                f"tick {i} returned only because the worker had finished — "
                "it must return while the worker is still running")

        assert len(calls) == 1, "the tick started more than one consolidation"
    finally:
        gate.set()
        if mind._consolidation_worker is not None:
            mind._consolidation_worker.join(timeout=JOIN_TIMEOUT_S)


def test_no_duplicate_concurrent_consolidation(due, monkeypatch):
    """A second tick must not start a second run — nor a second px-mind."""
    gate = threading.Event()
    calls = []

    def _slow(**kw):
        calls.append(kw)
        gate.wait(JOIN_TIMEOUT_S)
        return {"status": "ok", "written": 1}

    monkeypatch.setattr(mind.spark_memory, "maybe_consolidate", _slow)
    try:
        mind._consolidation_tick(SPARK, dry=False)
        first = mind._consolidation_worker
        for _ in range(3):
            mind._consolidation_tick(SPARK, dry=False)
        assert mind._consolidation_worker is first
        assert len(calls) == 1
        # And the marker itself refuses a second claimant, so the guard does
        # not rest solely on a process-local variable.
        assert memory.claim_consolidation_job(attempt=2) is False
    finally:
        gate.set()
        if mind._consolidation_worker is not None:
            mind._consolidation_worker.join(timeout=JOIN_TIMEOUT_S)


def test_the_tick_heartbeats_the_marker_while_the_worker_runs(due, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(mind.spark_memory, "maybe_consolidate",
                        lambda **kw: (gate.wait(JOIN_TIMEOUT_S), {"status": "ok"})[1])
    try:
        mind._consolidation_tick(SPARK, dry=False)
        _backdate_marker(started_ago_s=400, beat_ago_s=400)
        mind._consolidation_tick(SPARK, dry=False)
        job = memory.read_consolidation_job()
        beat = memory._parse_ts(job["heartbeat_ts"])
        age = (dt.datetime.now(UTC) - beat).total_seconds()
        assert age < FRESH_WITHIN_S, "the tick did not refresh the heartbeat"
        assert not memory.consolidation_job_is_stale(job)
    finally:
        gate.set()
        if mind._consolidation_worker is not None:
            mind._consolidation_worker.join(timeout=JOIN_TIMEOUT_S)


def test_an_overrunning_worker_is_reported_exactly_once(due, monkeypatch):
    """A stuck worker must be visible, and must not spam a failure every 60s."""
    gate = threading.Event()
    monkeypatch.setattr(mind.spark_memory, "maybe_consolidate",
                        lambda **kw: (gate.wait(JOIN_TIMEOUT_S), {"status": "ok"})[1])
    from pxh import health
    try:
        mind._consolidation_tick(SPARK, dry=False)
        _backdate_marker(started_ago_s=memory.JOB_OVERRUN_AFTER_S + 60)
        for _ in range(4):
            mind._consolidation_tick(SPARK, dry=False)
        rec = json.loads(
            health._component_path(health.CONSOLIDATION_COMPONENT).read_text())
        assert rec["consecutive_failures"] == 1
        assert "overran" in rec["last_error"]
    finally:
        gate.set()
        if mind._consolidation_worker is not None:
            mind._consolidation_worker.join(timeout=JOIN_TIMEOUT_S)


# ---------------------------------------------------------------------------
# 1b. A marker cannot survive a restart as a false "in progress" claim
# ---------------------------------------------------------------------------

def test_a_marker_from_a_dead_owner_is_not_a_running_claim(due, monkeypatch):
    """px-mind restarted mid-run: the thread went with it, the file did not.

    The marker is keyed on pid, so a marker whose owner is gone is detectably a
    lie. It is cleared and recorded as a *failure* — no memory formed that
    night — rather than silently reset.
    """
    from pxh import health
    memory._write_consolidation_job({
        "status": "running", "pid": 999999, "attempt": 1,
        "started_ts": dt.datetime.now(UTC).isoformat(),
        "heartbeat_ts": dt.datetime.now(UTC).isoformat(),
    })
    ran = []
    monkeypatch.setattr(mind.spark_memory, "maybe_consolidate",
                        lambda **kw: ran.append(1) or {"status": "ok"})

    mind._consolidation_tick(SPARK, dry=False)
    assert not memory.read_consolidation_job(), "the stale marker was not cleared"
    rec = json.loads(
        health._component_path(health.CONSOLIDATION_COMPONENT).read_text())
    assert "abandoned" in rec["last_error"]
    # Cleaning up is one tick's work; the next tick is free to try again.
    mind._consolidation_tick(SPARK, dry=False)
    worker = mind._consolidation_worker
    if worker is not None:
        worker.join(timeout=JOIN_TIMEOUT_S)
    assert ran == [1]


def test_a_marker_with_a_silent_heartbeat_is_stale():
    """A live pid is not enough: the process may exist and no longer be ticking."""
    import os
    fresh = dt.datetime.now(UTC)
    old = fresh - dt.timedelta(seconds=memory.JOB_HEARTBEAT_STALE_S + 60)
    live_and_beating = {"pid": os.getpid(), "started_ts": old.isoformat(),
                        "heartbeat_ts": fresh.isoformat()}
    live_but_quiet = {"pid": os.getpid(), "started_ts": old.isoformat(),
                      "heartbeat_ts": old.isoformat()}
    no_beat_at_all = {"pid": os.getpid(), "started_ts": fresh.isoformat()}
    assert not memory.consolidation_job_is_stale(live_and_beating)
    assert memory.consolidation_job_is_stale(live_but_quiet)
    assert memory.consolidation_job_is_stale(no_beat_at_all)
    assert not memory.consolidation_job_is_stale({})  # absent is not stale


def test_a_stale_marker_does_not_block_a_fresh_claim():
    memory._write_consolidation_job({
        "status": "running", "pid": 999999, "attempt": 1,
        "started_ts": dt.datetime.now(UTC).isoformat(),
        "heartbeat_ts": dt.datetime.now(UTC).isoformat(),
    })
    assert memory.claim_consolidation_job(attempt=1) is True
    assert memory.read_consolidation_job()["pid"] == __import__("os").getpid()


# ---------------------------------------------------------------------------
# 2. One deadline source: the kind's declared 600s
# ---------------------------------------------------------------------------

def test_consolidate_passes_no_ad_hoc_timeout(monkeypatch, tmp_path):
    """memory.consolidate() must let the declared deadline stand.

    The ad-hoc 180 that used to sit here was tighter than the declared 600, and
    the tighter number always wins — so the budget the kind declares was never
    once reachable.
    """
    (tmp_path / "thoughts-spark.jsonl").write_text("".join(
        json.dumps({"ts": dt.datetime.now(UTC).isoformat(),
                    "thought": f"thought {i}"}) + "\n" for i in range(8)),
        encoding="utf-8")
    seen = {}

    def _fake(session_type, prompt, **kw):
        seen["type"] = session_type
        seen["kw"] = kw
        return claude_session.RunResult(
            stdout="[]", stderr="", returncode=0, duration_s=0.0,
            model_used="haiku")

    monkeypatch.setattr(claude_session, "run_claude_session", _fake)
    memory.consolidate()
    assert seen["type"] == "consolidate"
    assert "timeout" not in seen["kw"], (
        f"consolidate() still overrides the declared deadline: {seen['kw']}")


def test_run_claude_session_defaults_to_the_declared_deadline(monkeypatch):
    """`timeout=None` is the default and reaches ask_brain as None.

    None is what makes brain.py's per-kind table authoritative — ask_brain
    substitutes `deadline_for_kind(kind)` only when the caller passed nothing.
    """
    seen = {}

    def _fake_ask(kind, payload, timeout_s=None, model=None):
        seen["kind"] = kind
        seen["timeout_s"] = timeout_s
        return {"reply": "ok"}

    monkeypatch.setattr(brain, "ask_brain", _fake_ask)
    monkeypatch.setattr(claude_session, "BUDGET_DISABLED", True)
    monkeypatch.setattr(claude_session, "SESSION_LOG",
                        claude_session.PROJECT_ROOT / "state" / "nonexistent.jsonl")
    monkeypatch.setattr(claude_session, "_log_session", lambda *a, **kw: None)
    claude_session.run_claude_session("consolidate", "prompt", allowed_tools="")
    assert seen["kind"] == "consolidate"
    assert seen["timeout_s"] is None
    assert brain.deadline_for_kind("consolidate") == 600


def test_the_declared_600s_is_what_reaches_the_request(monkeypatch):
    """End of the plumbing: the request the brain would answer carries 600s.

    Fully inert — the mailbox is conftest's tmp dir, the session state is
    stubbed validated, and `inject` is stubbed to fail so ask_brain returns
    immediately after writing the request. Nothing waits; the assertion is on
    the number in the file.
    """
    session = brain.session_for_kind("consolidate")
    captured = []

    def _capture_then_fail(*a, **kw):
        # ask_brain cleans the inbox up in its `finally`, so read the request
        # here — at the one moment it exists — and then refuse the injection so
        # the call returns without waiting for any reply.
        for f in brain.inbox_dir(session).glob("*.json"):
            captured.append(json.loads(f.read_text()))
        return False

    monkeypatch.setattr(brain, "session_state", lambda *a, **kw: brain.VALIDATED)
    monkeypatch.setattr(brain.tmux_claude, "inject", _capture_then_fail)
    before = time.time()
    assert brain.ask_brain("consolidate", {"prompt": "x"}) is None

    assert len(captured) == 1
    request = captured[0]
    assert request["kind"] == "consolidate"
    budget = request["deadline"] - before
    # A window, not an equality: ask_brain deducts the time already spent
    # waiting for validation and the lock from the caller's budget.
    assert 580 <= budget <= 601, f"deadline carried {budget:.0f}s, expected ~600"


# ---------------------------------------------------------------------------
# 3. Attempt 2 is actually reachable
# ---------------------------------------------------------------------------

def test_quota_matches_the_attempts_memory_promises():
    """A quota of 1 against MAX_ATTEMPTS_PER_DAY=2 made the retry unspendable."""
    assert (claude_session._TYPE_QUOTAS["consolidate"]
            == memory.MAX_ATTEMPTS_PER_DAY == 2)


def test_retry_spacing_clears_the_global_cooldown():
    """Attempt 2 is spaced past the 30-min global cooldown, not exempted from it.

    Exempting `consolidate` (as `self_debug` and `blog` are) would buy nothing
    here — nobody is waiting on a 3am retry — and every exemption is one more
    way for the nightly job to crowd a session someone *is* waiting on. Spacing
    is the cheaper answer, but it only works if the gap really is larger.
    """
    assert memory.RETRY_SPACING_S > claude_session.COOLDOWN_S
    assert memory.RETRY_SPACING_S >= claude_session._TYPE_COOLDOWNS["consolidate"]
    assert "consolidate" not in claude_session._GLOBAL_COOLDOWN_EXEMPT
    # Two spaced attempts still fit inside the 02:00-06:00 window.
    window_s = (memory.CONSOLIDATION_WINDOW[1]
                - memory.CONSOLIDATION_WINDOW[0]) * 3600
    assert memory.RETRY_SPACING_S * (memory.MAX_ATTEMPTS_PER_DAY - 1) < window_s


def test_a_failed_attempt_one_leaves_attempt_two_reachable(monkeypatch):
    """The end-to-end gate: fail at 02:00, and 02:40 is a real second attempt."""
    at2 = dt.datetime(2026, 7, 11, 2, 0, tzinfo=memory.HOBART_TZ)
    calls = []

    def _fail(**kw):
        calls.append(kw)
        return {"status": "failed", "error": "brain unavailable"}

    monkeypatch.setattr(memory, "consolidate", _fail)
    assert memory.maybe_consolidate(now=at2)["status"] == "failed"
    # Immediately after, the spacing gate holds it back...
    assert memory.consolidation_due(now=at2 + dt.timedelta(minutes=5)) is not None
    # ...and once the gap has passed, attempt 2 really runs.
    later = at2 + dt.timedelta(seconds=memory.RETRY_SPACING_S)
    assert memory.consolidation_due(now=later) is None
    assert memory.maybe_consolidate(now=later)["status"] == "failed"
    assert len(calls) == 2
    meta = json.loads(memory.consolidation_meta_file().read_text())
    assert meta["attempts"] == 2 and meta["done"] is False


def test_the_budget_gate_admits_attempt_two(monkeypatch, tmp_path):
    """claude_session's own quota/cooldown must not be what blocks the retry."""
    log = tmp_path / "claude_sessions.jsonl"
    then = dt.datetime.now(UTC) - dt.timedelta(seconds=memory.RETRY_SPACING_S)
    log.write_text(json.dumps({
        "ts": then.isoformat().replace("+00:00", "Z"),
        "session_type": "consolidate", "model": "haiku",
        "duration_s": 1.0, "returncode": 1, "status": "brain_unavailable",
    }) + "\n", encoding="utf-8")
    monkeypatch.setattr(claude_session, "SESSION_LOG", log)
    monkeypatch.setattr(claude_session, "BUDGET_DISABLED", False)
    assert claude_session.check_budget("consolidate") is None


def test_success_marks_the_day_done(monkeypatch):
    at2 = dt.datetime(2026, 7, 11, 2, 0, tzinfo=memory.HOBART_TZ)
    monkeypatch.setattr(memory, "consolidate",
                        lambda **kw: {"status": "ok", "written": 3})
    assert memory.maybe_consolidate(now=at2)["status"] == "ok"
    later = at2 + dt.timedelta(seconds=memory.RETRY_SPACING_S)
    assert memory.consolidation_due(now=later) == "already done for this date"


def test_a_correct_skip_also_marks_the_day_done(monkeypatch):
    # Too few thoughts in 24h is a correct outcome; retrying it at 03:40 would
    # spend a session to reach the same answer.
    at2 = dt.datetime(2026, 7, 11, 2, 0, tzinfo=memory.HOBART_TZ)
    monkeypatch.setattr(memory, "consolidate",
                        lambda **kw: {"status": "skipped", "reason": "3 thoughts"})
    assert memory.maybe_consolidate(now=at2)["status"] == "skipped"
    assert memory.consolidation_due(
        now=at2 + dt.timedelta(hours=1)) == "already done for this date"


def test_the_window_and_meta_shape_are_unchanged():
    """The existing contract other code and the operator read."""
    assert memory.CONSOLIDATION_WINDOW == (2, 6)
    assert memory.consolidation_due(
        now=dt.datetime(2026, 7, 11, 12, 0, tzinfo=memory.HOBART_TZ)
    ) == "outside the 02:00-06:00 window"
    assert memory.consolidation_meta_file().name == "consolidation_meta.json"
    at2 = dt.datetime(2026, 7, 11, 2, 30, tzinfo=memory.HOBART_TZ)
    assert memory.consolidation_due(now=at2) is None
