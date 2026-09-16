"""Nightly consolidation must be able to succeed (#291).

Three defects made success structurally impossible, and each gets its own
section here:

1. **The pass ran inline on px-mind's ~60s awareness tick.** Its declared
   budget then was 600s, twice px-mind's own 300s health-staleness window, so
   honouring the budget and keeping the mind loop alive were mutually
   exclusive. Now it runs on a daemon thread with a pid-keyed job marker.
2. **An ad-hoc `timeout=180` overrode the declared 600s deadline.** The tighter
   number always won, so the declared budget was never once reachable.
3. **Attempt 2 could not be spent.** `MAX_ATTEMPTS_PER_DAY` promised two tries
   a night while the `consolidate` quota was 1 and its type cooldown 20 hours.

Section 4 is #310, where three more defects kept the pass failing for nine
nights — attempt 1 landed inside the resident session supervisor's nightly
recycle, the retry spacing was measured against the wrong end of attempt 1, and
a `research` failure that spent nothing armed the global cooldown against
everyone — plus the attempt number, which logged one too high all along. The
recycle is gone (#317 Phase 3 retired the supervisor), so the window's start no
longer has anything to clear; the arithmetic that made it necessary is kept as
the record of why it is 03:00.

Inert by construction: no service is touched, no tmux session is reached, no
Claude call is made, and every duration in here is synthetic — a "600s"
deadline is asserted as a *plumbed number*, never waited for. The one real
thread that runs blocks on an Event the test controls.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time

import pytest

import pxh.mind as mind
from pxh import claude_session, m5, memory

# The deadline a consolidation attempt actually gets, now that the resident
# session's per-kind table is gone (#317 Phase 3). It is the tier's own
# configured timeout — `PX_M5_SPARK_TIMEOUT_S`, 120 on the deployed Pi — and it
# is the number every duration below has to be measured against.
def _attempt_deadline_s() -> float:
    return float(os.environ.get("PX_M5_SPARK_TIMEOUT_S", m5.M5_TIMEOUT_S))

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


def _live_marker(**overrides) -> dict:
    """A marker naming *this* process, by the full identity tuple.

    Built rather than hand-written so the identity fields cannot drift out of a
    test and quietly turn it into an assertion about a malformed marker.
    """
    now = dt.datetime.now(UTC).isoformat()
    job = {
        "status": "running",
        "pid": os.getpid(),
        "boot_id": memory._read_boot_id(),
        "pid_starttime": memory._pid_starttime(os.getpid()),
        "attempt": 1,
        "started_ts": now,
        "heartbeat_ts": now,
    }
    job.update(overrides)
    return job


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
    """A live, correctly-identified pid is not enough on its own.

    The process may exist, be the right process, and simply have stopped
    ticking. The heartbeat is the only check that catches that, which is why
    the identity checks below are additions to it rather than replacements.
    """
    fresh = dt.datetime.now(UTC)
    old = fresh - dt.timedelta(seconds=memory.JOB_HEARTBEAT_STALE_S + 60)
    live_and_beating = _live_marker(started_ts=old.isoformat(),
                                    heartbeat_ts=fresh.isoformat())
    live_but_quiet = _live_marker(started_ts=old.isoformat(),
                                  heartbeat_ts=old.isoformat())
    no_beat_at_all = _live_marker()
    no_beat_at_all.pop("heartbeat_ts")
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
    assert memory.read_consolidation_job()["pid"] == os.getpid()


# ---------------------------------------------------------------------------
# 1c. A pid is a slot the kernel reuses, not a name for a process
# ---------------------------------------------------------------------------

def test_a_freshly_claimed_marker_carries_its_full_identity():
    """Claiming stamps boot_id and start time, and that marker is not stale."""
    assert memory.claim_consolidation_job(attempt=1) is True
    job = memory.read_consolidation_job()
    assert job["boot_id"] == memory._read_boot_id()
    assert job["pid_starttime"] == memory._pid_starttime(os.getpid())
    assert isinstance(job["pid_starttime"], int)
    assert not memory.consolidation_job_is_stale(job)


def test_a_marker_from_a_previous_boot_is_stale():
    """A reboot means the owning thread is certainly gone.

    Nothing survives a reboot, so this marker is reclaimable *immediately* —
    it must not have to wait out a heartbeat that can never arrive again. The
    pid here is this live process's, so only the boot id can make it stale.
    """
    job = _live_marker(boot_id="00000000-0000-4000-8000-000000000000")
    assert memory.consolidation_job_is_stale(job)
    memory._write_consolidation_job(job)
    assert memory.claim_consolidation_job(attempt=1) is True


def test_pid_reuse_does_not_read_as_a_running_consolidation():
    """The pid is live and passes the cmdline check; the start tick is not ours.

    This is the case the marker could not previously tell apart: an unrelated
    process that inherited the number would have read as a consolidation in
    flight for as long as it ran, blocking every attempt that night.
    """
    real = memory._pid_starttime(os.getpid())
    assert isinstance(real, int)
    assert memory.consolidation_job_is_stale(_live_marker(pid_starttime=real + 1))
    assert not memory.consolidation_job_is_stale(_live_marker(pid_starttime=real))


@pytest.mark.parametrize("field", ["boot_id", "pid_starttime"])
def test_a_marker_missing_an_identity_field_is_stale(field):
    """Lenient read, same posture as the rest of the module.

    A marker we cannot identify is not evidence of work in progress, and must
    never be able to block tonight's attempt — including the markers written by
    the pre-hardening version of this code, which carry neither field.
    """
    missing = _live_marker()
    missing.pop(field)
    assert memory.consolidation_job_is_stale(missing)
    assert memory.consolidation_job_is_stale(_live_marker(**{field: None}))
    assert memory.consolidation_job_is_stale(_live_marker(**{field: "junk"}))


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


def test_consolidate_passes_no_timeout_and_the_tier_decides(monkeypatch):
    """`timeout=None` is the default and reaches the tier as None.

    None is what makes the tier's own configured deadline authoritative. The
    per-kind table this used to defer to (`brain._DEADLINE_S`) went with the
    session; what replaces it is not a second table but the one number the
    provider client already reads, which is why the previous test in this file
    asserts the kwarg is absent rather than equal.
    """
    seen = {}

    def _fake_ask(kind, prompt, system, *, timeout_s=None, model=None, **kw):
        seen["kind"] = kind
        seen["timeout_s"] = timeout_s
        return m5.M5Result(status="available", response="[]")

    monkeypatch.setattr(m5, "ask_m5", _fake_ask)
    monkeypatch.setattr(claude_session, "BUDGET_DISABLED", True)
    monkeypatch.setattr(claude_session, "SESSION_LOG",
                        claude_session.PROJECT_ROOT / "state" / "nonexistent.jsonl")
    result = claude_session.run_claude_session("consolidate", "prompt")
    assert result.returncode == 0
    assert seen == {"kind": "consolidate", "timeout_s": None}
    assert _attempt_deadline_s() > 0


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
    # Two spaced attempts still fit inside the 03:00-06:00 window.
    window_s = (memory.CONSOLIDATION_WINDOW[1]
                - memory.CONSOLIDATION_WINDOW[0]) * 3600
    assert memory.RETRY_SPACING_S * (memory.MAX_ATTEMPTS_PER_DAY - 1) < window_s


def test_a_failed_attempt_one_leaves_attempt_two_reachable(monkeypatch):
    """The end-to-end gate: fail at 03:00, and the spaced retry really runs."""
    at3 = dt.datetime(2026, 7, 11, 3, 0, tzinfo=memory.HOBART_TZ)
    calls = []

    def _fail(**kw):
        calls.append(kw)
        return {"status": "failed", "error": "brain unavailable"}

    monkeypatch.setattr(memory, "consolidate", _fail)
    assert memory.maybe_consolidate(now=at3)["status"] == "failed"
    # Immediately after, the spacing gate holds it back...
    assert memory.consolidation_due(now=at3 + dt.timedelta(minutes=5)) is not None
    # ...and once the gap has passed, attempt 2 really runs.
    later = at3 + dt.timedelta(seconds=memory.RETRY_SPACING_S)
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
    at3 = dt.datetime(2026, 7, 11, 3, 0, tzinfo=memory.HOBART_TZ)
    monkeypatch.setattr(memory, "consolidate",
                        lambda **kw: {"status": "ok", "written": 3})
    assert memory.maybe_consolidate(now=at3)["status"] == "ok"
    later = at3 + dt.timedelta(seconds=memory.RETRY_SPACING_S)
    assert memory.consolidation_due(now=later) == "already done for this date"


def test_a_correct_skip_also_marks_the_day_done(monkeypatch):
    # Too few thoughts in 24h is a correct outcome; retrying it at 03:40 would
    # spend a session to reach the same answer.
    at3 = dt.datetime(2026, 7, 11, 3, 0, tzinfo=memory.HOBART_TZ)
    monkeypatch.setattr(memory, "consolidate",
                        lambda **kw: {"status": "skipped", "reason": "3 thoughts"})
    assert memory.maybe_consolidate(now=at3)["status"] == "skipped"
    assert memory.consolidation_due(
        now=at3 + dt.timedelta(hours=1)) == "already done for this date"


def test_the_window_and_meta_shape_are_what_the_operator_reads():
    """The contract other code, the health record and the operator read."""
    assert memory.CONSOLIDATION_WINDOW == (3, 6)
    assert memory.consolidation_due(
        now=dt.datetime(2026, 7, 11, 12, 0, tzinfo=memory.HOBART_TZ)
    ) == "outside the 03:00-06:00 window"
    # Built from the constant, so the refusal an operator reads cannot describe
    # a window the gate no longer applies.
    assert memory.consolidation_due(
        now=dt.datetime(2026, 7, 11, 2, 30, tzinfo=memory.HOBART_TZ)
    ) == "outside the 03:00-06:00 window"
    assert memory.consolidation_meta_file().name == "consolidation_meta.json"
    at3 = dt.datetime(2026, 7, 11, 3, 30, tzinfo=memory.HOBART_TZ)
    assert memory.consolidation_due(now=at3) is None


# ---------------------------------------------------------------------------
# 4. The retry survives a night the model is having (#310)
#
# Nine nights of memory were lost to three defects that all lived in the gap
# between two gates: consolidation's window and the session supervisor's
# nightly recycle, the retry spacing's clock and the type cooldown's clock, and
# the global cooldown's reading of an entry that spent nothing. None of these
# tests calls a model: every duration is a number the code declares, and the
# session log is written by hand.
# ---------------------------------------------------------------------------

def test_the_window_still_admits_both_spaced_attempts():
    """#310.1: the first attempt raced the supervisor's own 02:00 recycle.

    A recycle cleared the session's validation marker on purpose — that is what
    made a caller fall back instead of typing into a session that had just
    forgotten its identity prompt — so everyone arriving during the boot that
    followed was refused *immediately* (`brain_unavailable`, dur=0.0s) rather
    than timing out. The first attempt landed in that boot on every one of the
    nine nights measured, and failed on arrival on every one of them.

    The supervisor and its recycle are gone (#317 Phase 3), so there is no
    longer a `NIGHTLY_RECYCLE_HOUR` to pin against — the coupling that test
    guarded cannot drift because one of its terms no longer exists. What
    survives, and is what the window's start actually has to satisfy, is the
    arithmetic: both attempts, spaced, inside the window.
    """
    start_h, end_h = memory.CONSOLIDATION_WINDOW
    span_s = (end_h - start_h) * 3600
    assert memory.RETRY_SPACING_S * (memory.MAX_ATTEMPTS_PER_DAY - 1) + \
        _attempt_deadline_s() <= span_s, (
            "two spaced attempts no longer fit between "
            f"{start_h:02d}:00 and {end_h:02d}:00")


def test_retry_spacing_covers_the_cooldown_and_the_attempt_before_it():
    """#310.2: the spacing gate and the type cooldown measure different things.

    This gate reads attempt 1's *start*; the `consolidate` type cooldown reads
    its *finish*. So the spacing has to clear the cooldown plus the attempt
    that sits between the two clocks. "40 minutes clears the global cooldown
    with ten minutes of margin" assumed attempt 1 ended when it started.
    """
    assert memory.RETRY_SPACING_S >= (
        claude_session._TYPE_COOLDOWNS["consolidate"] + _attempt_deadline_s())


def test_a_slow_first_attempt_still_leaves_the_retry_admissible(monkeypatch, tmp_path):
    """#310.2, end to end: attempt 1 fails *after* using its whole deadline.

    The entry is built the way the real attempt leaves the log — written at
    start + the deadline, which is exactly the disagreement between the two
    clocks — and then asked of claude_session's own gate rather than of a
    restatement of it. Put RETRY_SPACING_S back to 2400 and this returns "consolidate cooldown
    (1800s / 2400s)", which is the refusal that cost the nine nights.
    """
    deadline = _attempt_deadline_s()
    started = dt.datetime.now(UTC) - dt.timedelta(seconds=memory.RETRY_SPACING_S)
    finished = started + dt.timedelta(seconds=deadline)
    log = tmp_path / "claude_sessions.jsonl"
    log.write_text(json.dumps({
        "ts": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "type": "consolidate", "model": "haiku",
        "duration_s": float(deadline),
        "returncode": 1, "outcome": "brain_unavailable",
    }) + "\n", encoding="utf-8")
    monkeypatch.setattr(claude_session, "SESSION_LOG", log)
    monkeypatch.setattr(claude_session, "BUDGET_DISABLED", False)
    assert claude_session.check_budget("consolidate") is None


def test_a_failure_that_spent_nothing_does_not_lock_out_the_retry(monkeypatch, tmp_path):
    """#310.3, the defect that cost 09-15 on its own.

    At 02:32:10 `research` logged rc=1 — the model was unreachable for every
    caller —
    and that entry, which spent nothing, was read as spend and refused
    `consolidate` the 02:41 retry it had finally been spaced correctly for:
    "global cooldown (554s / 1800s)".
    """
    entry = {
        "ts": (dt.datetime.now(UTC) - dt.timedelta(seconds=554))
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "type": "research", "model": "haiku", "duration_s": 0.0,
        "returncode": 1, "outcome": "brain_unavailable",
    }
    log = tmp_path / "claude_sessions.jsonl"
    log.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    monkeypatch.setattr(claude_session, "SESSION_LOG", log)
    monkeypatch.setattr(claude_session, "BUDGET_DISABLED", False)
    assert claude_session.check_budget("consolidate") is None

    # The same entry *answered* is a real cooldown. This narrows the gate to
    # spend; it does not remove it.
    entry["returncode"], entry["outcome"] = 0, "success"
    log.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    assert "global cooldown" in claude_session.check_budget("consolidate")


def test_the_first_attempt_of_a_night_is_reported_as_the_first(monkeypatch):
    """#310.4: every night logged its first attempt as "attempt 3/2".

    `next_consolidation_attempt` runs before `maybe_consolidate` resets the
    attempts for the new Hobart date, so it read the *previous* night's two
    attempts and added one — 3/2 on the first run of the night, 2/2 on the
    second, nightly. Reporting only; nothing was gated on it.
    """
    night1 = dt.datetime(2026, 7, 11, 3, 0, tzinfo=memory.HOBART_TZ)
    night2 = dt.datetime(2026, 7, 12, 3, 0, tzinfo=memory.HOBART_TZ)
    monkeypatch.setattr(memory, "consolidate",
                        lambda **kw: {"status": "failed", "error": "brain unavailable"})
    assert memory.next_consolidation_attempt(now=night1) == 1
    assert memory.maybe_consolidate(now=night1)["status"] == "failed"
    assert memory.next_consolidation_attempt(now=night1) == 2
    assert memory.maybe_consolidate(
        now=night1 + dt.timedelta(seconds=memory.RETRY_SPACING_S))["status"] == "failed"
    assert memory.next_consolidation_attempt(now=night1) == 3
    # The rollover is what the cap is keyed to: last night's two are spent.
    assert memory.next_consolidation_attempt(now=night2) == 1
    assert memory.maybe_consolidate(now=night2)["status"] == "failed"
