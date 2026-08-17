"""Tests for px-brain, the supervisor over SPARK's resident Claude sessions.

Every behaviour asserted here exists to stop a specific way of destroying work:
killing a session that was about to answer, replaying requests nobody wants,
clearing context mid-request, or — the quiet one — running without an attached
tmux client so that injection fails only when nobody is watching.
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from pxh import brain, brain_daemon, tmux_claude

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _mailbox(tmp_path, monkeypatch):
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(brain, "brain_root", lambda: tmp_path / "brain")


@pytest.fixture(autouse=True)
def _release_supervisor_guard():
    yield
    brain_daemon.release_supervisor_lock()


class _FakeTmux:
    """A tmux that exists in memory, so the supervisor can be driven exactly."""

    def __init__(self, ready=True, exists=False):
        self.ready = ready
        self.sessions = {"spark-brain", "spark-io"} if exists else set()
        self.injected = []
        self.keys = []
        self.killed = []
        self.created = []

    def install(self, monkeypatch):
        monkeypatch.setattr(tmux_claude, "session_exists",
                           lambda spec=None: self._name(spec) in self.sessions)
        monkeypatch.setattr(tmux_claude, "pane_ready", lambda spec=None: self.ready)
        monkeypatch.setattr(tmux_claude, "ensure_session", self._ensure)
        monkeypatch.setattr(tmux_claude, "inject", self._inject)
        monkeypatch.setattr(tmux_claude, "send_key", self._send_key)
        monkeypatch.setattr(tmux_claude, "kill_session", self._kill)
        monkeypatch.setattr(tmux_claude, "HolderClient", _FakeHolder)
        return self

    @staticmethod
    def _name(spec):
        return spec.name if spec is not None else "spark-brain"

    def _ensure(self, timeout_s=None, spec=None):
        name = self._name(spec)
        if name not in self.sessions:
            self.sessions.add(name)
            self.created.append(name)
        return True

    def _inject(self, text, spec=None):
        self.injected.append((self._name(spec), text))
        return True

    def _send_key(self, key, spec=None):
        self.keys.append((self._name(spec), key))
        return True

    def _kill(self, spec=None):
        self.killed.append(self._name(spec))
        self.sessions.discard(self._name(spec))
        return True


class _FakeHolder:
    instances = []

    def __init__(self, spec=None):
        self.spec = spec
        self.started = 0
        self._alive = False
        _FakeHolder.instances.append(self)

    def start(self):
        self.started += 1
        self._alive = True

    def alive(self):
        return self._alive

    def stop(self):
        self._alive = False


@pytest.fixture
def fake_tmux(monkeypatch):
    _FakeHolder.instances = []
    return _FakeTmux().install(monkeypatch)


def _state(name=brain.BRAIN_SESSION, *, real_clock=False):
    """A fresh SessionState. `real_clock=True` stamps `last_recycle_day` to
    today's real Hobart date, for a test that drives `tick()` on the real
    wall clock rather than a fixed `datetime` — otherwise `nightly_due` is
    true for 22 of every 24 hours against the default `last_recycle_day=""`,
    and the recycle it fires is not what that test is about."""
    state = brain_daemon.SessionState(name=name)
    if real_clock:
        state.last_recycle_day = datetime.now(brain_daemon.HOBART).strftime("%Y-%m-%d")
    return state


def _write_current(session, request_id="r1", deadline=None):
    brain.ensure_mailbox(session)
    brain.current_path(session).write_text(json.dumps({
        "id": request_id,
        "deadline": deadline if deadline is not None else time.time() + 300,
    }))


# ---------------------------------------------------------------------------
# The holder — the reason this daemon exists
# ---------------------------------------------------------------------------

def test_a_holder_is_attached_to_every_session(fake_tmux, monkeypatch):
    """tmux 3.3a's send-keys fails with no attached client, and a daemon is
    exactly the case where nothing is watching. No holder, no injection.

    Not about the handshake — stub it out so a fresh, unanswered session
    doesn't fail its (real, slow) validation and drop the holder it just
    attached. The stub reports success without writing a marker; this test
    only reads `state.holder`, never the marker, so that's fine here."""
    monkeypatch.setattr(brain_daemon, "run_handshake", lambda state, reason: True)
    states = {n: _state(n) for n in (brain.BRAIN_SESSION, brain.IO_SESSION)}
    brain_daemon.tick(states)
    assert all(s.holder is not None and s.holder.alive() for s in states.values())


def test_a_dead_holder_is_restarted(fake_tmux):
    state = _state()
    brain_daemon.start_session(state)
    state.holder._alive = False
    brain_daemon.start_session(state)
    assert state.holder.started == 2, "a holder that died must be replaced"


def test_the_holder_is_not_respawned_over_a_live_one(fake_tmux):
    state = _state()
    for _ in range(3):
        brain_daemon.start_session(state)
    assert state.holder.started == 1


# ---------------------------------------------------------------------------
# Sweep on (re)create
# ---------------------------------------------------------------------------

def test_pending_requests_are_swept_when_a_session_is_created(fake_tmux):
    """Callers time out and fall back on their own. A fresh session that
    replayed their requests would spend budget answering nobody."""
    session = brain.BRAIN_SESSION
    brain.ensure_mailbox(session)
    (brain.inbox_dir(session) / "aaaa.json").write_text("{}")
    (brain.inbox_dir(session) / "bbbb.json").write_text("{}")

    brain_daemon.start_session(_state(session))

    assert not list(brain.inbox_dir(session).glob("*.json"))
    assert len(list(brain.dead_dir(session).glob("*.json"))) == 2


def test_nothing_is_swept_when_the_session_was_already_up(fake_tmux):
    """A sweep on every tick would delete the request currently in flight."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    (brain.inbox_dir(session) / "aaaa.json").write_text("{}")

    brain_daemon.start_session(_state(session))
    assert list(brain.inbox_dir(session).glob("*.json")), \
        "an in-flight request must survive a supervisor tick"


def test_validation_is_dropped_on_a_fresh_session(fake_tmux):
    """The marker describes a session that no longer exists. Trusting it would
    send the next request into a session that has never answered anything."""
    session = brain.BRAIN_SESSION
    brain.ensure_mailbox(session)
    brain.write_validation_marker(session, state="validated", request_id="x",
                                  model="claude-haiku-4-5-20251001", attempt=1)
    brain_daemon.start_session(_state(session))
    assert not brain.validation_path(session).exists()


# ---------------------------------------------------------------------------
# Wedge detection
# ---------------------------------------------------------------------------

def test_a_stale_inbox_entry_is_not_a_wedge(fake_tmux):
    """A caller that gave up leaves an inbox file behind. Treating that as a
    wedge would kill a perfectly healthy session."""
    session = brain.BRAIN_SESSION
    brain.ensure_mailbox(session)
    (brain.inbox_dir(session) / "old.json").write_text("{}")
    state = _state(session)

    brain_daemon.check_wedge(state, time.monotonic())
    assert fake_tmux.keys == [] and fake_tmux.killed == []


def test_a_request_inside_its_deadline_is_left_alone(fake_tmux):
    """A long turn is not a wedge. Most of them are just Claude thinking."""
    session = brain.BRAIN_SESSION
    _write_current(session, deadline=time.time() + 300)
    fake_tmux.ready = False

    brain_daemon.check_wedge(_state(session), time.monotonic())
    assert fake_tmux.keys == [] and fake_tmux.killed == []


def test_a_past_deadline_with_a_returned_prompt_is_not_a_wedge(fake_tmux):
    """The prompt glyph means the session is listening again — whatever
    happened to that request is the caller's problem, not a stuck session."""
    session = brain.BRAIN_SESSION
    _write_current(session, deadline=time.time() - 10_000)
    fake_tmux.ready = True

    brain_daemon.check_wedge(_state(session), time.monotonic())
    assert fake_tmux.keys == [] and fake_tmux.killed == []


def test_a_wedged_session_gets_escape_before_it_gets_killed(fake_tmux):
    """Escape costs nothing and often works. A kill throws away the context
    that makes a resident session worth having."""
    session = brain.BRAIN_SESSION
    _write_current(session, deadline=time.time() - 10_000)
    fake_tmux.ready = False
    state = _state(session)

    brain_daemon.check_wedge(state, time.monotonic())
    assert fake_tmux.keys == [(session, "Escape")]
    assert fake_tmux.killed == []


def test_escape_is_sent_without_a_following_enter(fake_tmux):
    """Enter after Escape submits whatever is in the input box — an unwedge
    attempt that turns into a stray turn."""
    session = brain.BRAIN_SESSION
    _write_current(session, deadline=time.time() - 10_000)
    fake_tmux.ready = False

    brain_daemon.check_wedge(_state(session), time.monotonic())
    assert fake_tmux.injected == [], "Escape must not go through inject()"


def test_a_session_that_ignores_escape_is_killed(fake_tmux):
    session = brain.BRAIN_SESSION
    _write_current(session, deadline=time.time() - 10_000)
    fake_tmux.ready = False
    state = _state(session)
    now = time.monotonic()

    brain_daemon.check_wedge(state, now)
    brain_daemon.check_wedge(state, now + brain_daemon.ESCAPE_GRACE_S + 1)

    assert fake_tmux.killed == [session]
    assert state.holder is None, \
        "the holder was attached to a session that no longer exists"


def test_recovery_between_escape_and_kill_spares_the_session(fake_tmux):
    session = brain.BRAIN_SESSION
    _write_current(session, deadline=time.time() - 10_000)
    fake_tmux.ready = False
    state = _state(session)
    now = time.monotonic()

    brain_daemon.check_wedge(state, now)
    fake_tmux.ready = True  # the prompt came back
    brain_daemon.check_wedge(state, now + brain_daemon.ESCAPE_GRACE_S + 1)

    assert fake_tmux.killed == []


# ---------------------------------------------------------------------------
# Context recycling
# ---------------------------------------------------------------------------

def test_recycling_never_happens_mid_request(fake_tmux, monkeypatch):
    """A /clear between the nudge and the reply loses the request outright,
    and the caller can only see that as a timeout."""
    from datetime import datetime
    session = brain.BRAIN_SESSION
    _write_current(session)
    state = _state(session)
    state.turns = brain_daemon.CONTEXT_TURNS + 5

    brain_daemon.maybe_recycle(state, datetime(2026, 8, 17, 3, 0, tzinfo=brain_daemon.HOBART))
    assert fake_tmux.injected == []


def test_recycling_waits_for_an_empty_inbox_too(fake_tmux):
    """A queued request with no deadline reads as live — an unreadable
    deadline must not become a reason to recycle over a real request."""
    session = brain.BRAIN_SESSION
    brain.ensure_mailbox(session)
    (brain.inbox_dir(session) / "queued.json").write_text("{}")
    state = _state(session)
    state.turns = brain_daemon.CONTEXT_TURNS + 5

    brain_daemon.maybe_recycle(state, datetime(2026, 8, 17, 3, 0, tzinfo=brain_daemon.HOBART))
    assert fake_tmux.injected == []


def test_recycling_journals_before_it_clears(fake_tmux):
    """The journal is the only continuity across /clear. Clearing first
    destroys exactly what it was meant to preserve."""
    from datetime import datetime
    session = brain.BRAIN_SESSION
    brain.ensure_mailbox(session)
    state = _state(session)
    state.turns = brain_daemon.CONTEXT_TURNS

    brain_daemon.maybe_recycle(state, datetime(2026, 8, 17, 3, 0, tzinfo=brain_daemon.HOBART))
    assert len(fake_tmux.injected) == 1
    _, text = fake_tmux.injected[0]
    assert text.index("journal") < text.index("/clear"), \
        "journal must be written before the context is cleared"
    assert state.turns == 0


def test_the_nightly_recycle_happens_once_a_day(fake_tmux):
    from datetime import datetime
    session = brain.BRAIN_SESSION
    brain.ensure_mailbox(session)
    state = _state(session)
    night = datetime(2026, 8, 17, 2, 30, tzinfo=brain_daemon.HOBART)

    brain_daemon.maybe_recycle(state, night)
    brain_daemon.maybe_recycle(state, night.replace(hour=5))
    assert len(fake_tmux.injected) == 1

    brain_daemon.maybe_recycle(state, night.replace(day=18))
    assert len(fake_tmux.injected) == 2, "a new day gets a new recycle"


def test_a_busy_brain_delays_the_nightly_recycle_rather_than_preempting(fake_tmux):
    """Overnight research and compose are allowed to run past the window. The
    recycle waits for idle; it never interrupts work someone is waiting on."""
    from datetime import datetime
    session = brain.BRAIN_SESSION
    _write_current(session)
    state = _state(session)
    late = datetime(2026, 8, 17, 5, 0, tzinfo=brain_daemon.HOBART)

    brain_daemon.maybe_recycle(state, late)
    assert fake_tmux.injected == []

    brain.current_path(session).unlink()
    brain_daemon.maybe_recycle(state, late)
    assert len(fake_tmux.injected) == 1, "recycle runs at the first idle moment"


def test_turns_are_counted_once_per_request(fake_tmux):
    session = brain.BRAIN_SESSION
    state = _state(session)
    _write_current(session, request_id="abc")
    for _ in range(5):
        brain_daemon.count_turns(state)
    _write_current(session, request_id="def")
    brain_daemon.count_turns(state)
    assert state.turns == 2


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

def test_a_tick_never_raises(monkeypatch, fake_tmux):
    """A supervisor that dies supervises nothing — and systemd restarting it
    into the same exception is a crash loop, not a recovery."""
    def _boom(*a, **k):
        raise RuntimeError("tmux exploded")

    monkeypatch.setattr(brain_daemon, "start_session", _boom)
    brain_daemon.tick({brain.BRAIN_SESSION: _state()})  # must not raise


def test_run_once_completes_a_single_pass(fake_tmux, monkeypatch):
    # Not about the handshake — stub it so the pass doesn't block on a real,
    # unanswered validation. The stub reports success without writing a
    # marker; this test only checks which sessions got created, not their
    # validation state.
    monkeypatch.setattr(brain_daemon, "run_handshake", lambda state, reason: True)
    assert brain_daemon.run(once=True) == 0
    assert set(fake_tmux.created) == {brain.BRAIN_SESSION, brain.IO_SESSION}


def test_the_journal_is_seeded(fake_tmux, monkeypatch):
    # Same stub, same reason — this test only checks the journal file.
    monkeypatch.setattr(brain_daemon, "run_handshake", lambda state, reason: True)
    brain_daemon.run(once=True)
    assert brain_daemon.journal_path().exists()


def test_both_sessions_report_health_separately():
    """One wedged session must be visible without the other masking it."""
    from pxh import health
    assert "px-brain" in health.STALE_AFTER_S
    assert "px-brain-io" in health.STALE_AFTER_S
    assert _state(brain.BRAIN_SESSION).component == "px-brain"
    assert _state(brain.IO_SESSION).component == "px-brain-io"


# ---------------------------------------------------------------------------
# The handshake — one real request, one real reply (§2.3)
# ---------------------------------------------------------------------------

@pytest.fixture
def _fast_handshake(monkeypatch):
    """Real logic, no waiting."""
    monkeypatch.setattr(brain, "SETTLE_S", 0.0)
    monkeypatch.setattr(brain, "HANDSHAKE_TIMEOUT_S", 0.5)
    monkeypatch.setattr(brain, "HANDSHAKE_ATTEMPTS", 2)
    monkeypatch.setattr(brain_daemon, "HANDSHAKE_POLL_S", 0.01)


def _echo_when_nudged(fake_tmux, session, answer=True, wrong_echo=False):
    """Answer the handshake the way a real session does: read the inbox, write
    the outbox. Installed as the fake tmux's inject hook."""
    def _inject(text, spec=None):
        fake_tmux.injected.append((fake_tmux._name(spec), text))
        if not answer or "NEW REQUEST" not in text:
            return True
        # Match the id the nudge actually names, not "whatever glob() returns
        # first" — a stray orphan sitting in the same inbox has no ordering
        # guarantee relative to the request this nudge is about.
        match = re.search(r"/([0-9a-f-]{36})\.json", text)
        if match is None:
            return True
        entry = brain.inbox_dir(session) / f"{match.group(1)}.json"
        if not entry.exists():
            return True
        request = json.loads(entry.read_text())
        echo = "not-the-nonce" if wrong_echo else request["payload"]["echo"]
        brain.outbox_dir(session).mkdir(parents=True, exist_ok=True)
        (brain.outbox_dir(session) / f"{request['id']}.json").write_text(
            json.dumps({"id": request["id"], "status": "ok",
                        "reply": {"echo": echo}}))
        return True
    return _inject


def test_a_round_trip_writes_a_validated_marker(fake_tmux, _fast_handshake, monkeypatch):
    """The whole point: `validated` means a real reply came back through the
    real channel, not that a prompt glyph appeared."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    monkeypatch.setattr(tmux_claude, "inject", _echo_when_nudged(fake_tmux, session))

    assert brain_daemon.run_handshake(_state(session), "no_marker") is True
    marker = brain.read_validation_marker(session)
    assert marker["state"] == "validated"
    assert marker["model"] == brain.configured_model(session)
    assert brain.session_state(session) == brain.VALIDATED


def test_the_handshake_leaves_no_request_behind(fake_tmux, _fast_handshake, monkeypatch):
    """A leftover inbox entry pins _is_idle() and blocks every later recycle."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    monkeypatch.setattr(tmux_claude, "inject", _echo_when_nudged(fake_tmux, session))

    brain_daemon.run_handshake(_state(session), "no_marker")
    assert not list(brain.inbox_dir(session).glob("*.json"))
    assert not brain.current_path(session).exists()


def test_a_reply_with_the_wrong_nonce_does_not_validate(fake_tmux, _fast_handshake, monkeypatch):
    """Echoing the nonce is what stops a stale reply from a previous handshake
    validating the current one."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    monkeypatch.setattr(tmux_claude, "inject",
                        _echo_when_nudged(fake_tmux, session, wrong_echo=True))

    assert brain_daemon.run_handshake(_state(session), "no_marker") is False
    assert brain.session_state(session) != brain.VALIDATED


def test_a_silent_session_is_escaped_retried_then_killed(fake_tmux, _fast_handshake, monkeypatch):
    """The handshake does its own escalation. It cannot rely on check_wedge,
    which clears itself the moment the glyph is back — exactly the case a
    permission dialog produces."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    monkeypatch.setattr(tmux_claude, "inject",
                        _echo_when_nudged(fake_tmux, session, answer=False))

    assert brain_daemon.run_handshake(_state(session), "no_marker") is False
    assert (session, "Escape") in fake_tmux.keys
    assert session in fake_tmux.killed
    assert brain.session_state(session) == brain.SESSION_ABSENT


def test_a_retry_re_nudges_the_same_request_id(fake_tmux, _fast_handshake, monkeypatch):
    """Two live requests for one handshake is two turns billed for one answer.
    The session may simply have been slow."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    monkeypatch.setattr(tmux_claude, "inject",
                        _echo_when_nudged(fake_tmux, session, answer=False))

    brain_daemon.run_handshake(_state(session), "no_marker")
    nudges = [text for _, text in fake_tmux.injected if "NEW REQUEST" in text]
    assert len(nudges) == 2, "HANDSHAKE_ATTEMPTS nudges, not more"
    ids = {re.search(r"/([0-9a-f-]{36})\.json", text).group(1) for text in nudges}
    assert len(ids) == 1, f"a retry must re-nudge the same id, saw {ids}"


def test_a_handshake_after_a_kill_uses_a_fresh_id(fake_tmux, _fast_handshake, monkeypatch):
    """The sweep and the new handshake would otherwise handle one id in both
    roles — one moving it to dead/, the other waiting on it."""
    session = brain.BRAIN_SESSION
    brain.ensure_mailbox(session)
    monkeypatch.setattr(tmux_claude, "inject",
                        _echo_when_nudged(fake_tmux, session, answer=False))
    first = _state(session)
    fake_tmux.sessions.add(session)
    brain_daemon.run_handshake(first, "no_marker")
    first_ids = {re.search(r"/([0-9a-f-]{36})\.json", t).group(1)
                 for _, t in fake_tmux.injected if "NEW REQUEST" in t}

    fake_tmux.injected.clear()
    fake_tmux.sessions.add(session)
    brain_daemon.run_handshake(_state(session), "no_marker")
    second_ids = {re.search(r"/([0-9a-f-]{36})\.json", t).group(1)
                  for _, t in fake_tmux.injected if "NEW REQUEST" in t}
    assert first_ids.isdisjoint(second_ids)


def test_handshakes_are_metered_like_any_other_request(fake_tmux, _fast_handshake, monkeypatch):
    """They are real Claude turns and cost real money, and a spike in the count
    is the visible symptom of a session restart-looping."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    monkeypatch.setattr(tmux_claude, "inject", _echo_when_nudged(fake_tmux, session))

    brain_daemon.run_handshake(_state(session), "no_marker")
    assert brain.meter_summary()["by_kind"].get("handshake") == 1


def test_the_marker_says_validating_while_the_handshake_runs(fake_tmux, _fast_handshake, monkeypatch):
    """`validating` is a normal state covering every boot and nightly recycle.
    A reader that saw `no_marker` there would alarm several times a day."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    seen = []

    def _inject(text, spec=None):
        seen.append(brain.read_validation_marker(session))
        return _echo_when_nudged(fake_tmux, session)(text, spec)

    monkeypatch.setattr(tmux_claude, "inject", _inject)
    brain_daemon.run_handshake(_state(session), "no_marker")
    assert seen[0]["state"] == "validating"
    assert seen[0]["attempt"] == 1


def test_a_caller_holding_the_lock_defers_the_handshake(fake_tmux, _fast_handshake, monkeypatch):
    """Injecting mid-turn splices two prompts into one and produces a
    plausible-looking wrong answer. There is another tick in ten seconds."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    monkeypatch.setattr(brain, "LOCK_WAIT_S", 0.05)
    held = brain._lock_for(session)
    held.acquire()
    try:
        assert brain_daemon.run_handshake(_state(session), "no_marker") is False
        assert not any("NEW REQUEST" in t for _, t in fake_tmux.injected)
    finally:
        held.release()


def test_a_write_failure_does_not_orphan_the_inbox_entry(fake_tmux, _fast_handshake, monkeypatch):
    """The inbox write can succeed while the current.json write fails right
    after it (ENOSPC, EACCES — this runs on an SD card). No marker names this
    id yet, so nothing could ever recover that file later."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    monkeypatch.setattr(tmux_claude, "inject", _echo_when_nudged(fake_tmux, session))

    real_atomic_write = brain_daemon.atomic_write
    calls = []

    def _flaky_write(path, data, **kwargs):
        calls.append(path)
        if path == brain.current_path(session):
            raise OSError("no space left on device")
        return real_atomic_write(path, data, **kwargs)

    monkeypatch.setattr(brain_daemon, "atomic_write", _flaky_write)

    assert brain_daemon.run_handshake(_state(session), "no_marker") is False
    assert not list(brain.inbox_dir(session).glob("*.json"))


def test_run_handshake_sweeps_the_request_the_stale_marker_names(fake_tmux, _fast_handshake, monkeypatch):
    """A supervisor that died mid-handshake leaves a `validating` marker
    naming a request nobody will ever answer. The next handshake's narrow
    sweep is what records that orphan."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    monkeypatch.setattr(tmux_claude, "inject", _echo_when_nudged(fake_tmux, session))

    orphan_id = "11111111-1111-1111-1111-111111111111"
    brain.write_validation_marker(session, state=brain.VALIDATING,
                                  request_id=orphan_id,
                                  model=brain.configured_model(session), attempt=1)
    (brain.inbox_dir(session) / f"{orphan_id}.json").write_text("{}")

    brain_daemon.run_handshake(_state(session), "no_marker")
    assert (brain.dead_dir(session) / f"{orphan_id}.json").exists()
    assert not (brain.inbox_dir(session) / f"{orphan_id}.json").exists()


def test_a_killed_handshake_stops_and_drops_the_holder(fake_tmux, _fast_handshake, monkeypatch):
    """The holder is attached to a session that no longer exists after a kill.
    Leaving it in place would leak a client attached to nothing."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    monkeypatch.setattr(tmux_claude, "inject",
                        _echo_when_nudged(fake_tmux, session, answer=False))

    state = _state(session)
    holder = _FakeHolder(brain.spec_for_session(session))
    holder.start()
    state.holder = holder

    assert brain_daemon.run_handshake(state, "no_marker") is False
    assert holder.alive() is False
    assert state.holder is None


@pytest.mark.parametrize("prompt", ["spark-brain-system.md", "spark-io-system.md"])
def test_both_prompts_explain_the_handshake_with_the_placeholder(prompt):
    """A session that does not know how to answer a handshake cannot be
    validated, and the placeholder is the only spelling that is right in both
    the prompt and the allowlist."""
    text = (ROOT / "docs" / "prompts" / prompt).read_text()
    assert "handshake" in text.lower()
    assert "payload.echo" in text
    assert "tool-brain-reply" not in text.replace("{{TOOL_BRAIN_REPLY}}", ""), \
        "the reply tool is named only via the placeholder"


# ---------------------------------------------------------------------------
# Triggers and fairness (§2.3, §2.6)
# ---------------------------------------------------------------------------

def test_no_marker_on_a_live_session_is_repaired_without_a_human(fake_tmux, monkeypatch):
    """`no_marker` is the only state reachable by *aging*, so no edge ever fires
    for it. Without a level-triggered tick the loud state has a repair line and
    no path to it, and the session sits broken until someone attaches."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    # A supervisor died mid-handshake: a stale `validating` marker, aged out.
    monkeypatch.setattr(brain, "VALIDATION_CEILING_S", 0.01)
    brain.write_validation_marker(session, state="validating", request_id="old",
                                  model=brain.configured_model(session), attempt=1)
    time.sleep(0.05)

    called = []
    monkeypatch.setattr(brain_daemon, "run_handshake",
                        lambda state, reason: called.append((state.name, reason)) or True)
    brain_daemon.tick({session: _state(session, real_clock=True)})
    assert called == [(session, "no_marker")]


def test_a_validated_session_is_not_re_handshaked(fake_tmux, monkeypatch):
    """Handshakes cost money. A working session is left alone."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    brain.write_validation_marker(session, state="validated", request_id="r",
                                  model=brain.configured_model(session), attempt=1)
    called = []
    monkeypatch.setattr(brain_daemon, "run_handshake",
                        lambda state, reason: called.append(reason) or True)
    brain_daemon.tick({session: _state(session, real_clock=True)})
    assert called == []


def test_a_model_mismatch_triggers_a_transition(fake_tmux, monkeypatch):
    """Someone changed the configuration. The marker's model is only ever
    written by a handshake the new model actually answered."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    brain.write_validation_marker(session, state="validated", request_id="r",
                                  model="claude-opus-4-6", attempt=1)
    monkeypatch.setenv("PX_CLAUDE_TMUX_MODEL", "claude-haiku-4-5-20251001")
    called = []
    monkeypatch.setattr(brain_daemon, "run_handshake",
                        lambda state, reason: called.append(reason) or True)
    brain_daemon.tick({session: _state(session, real_clock=True)})
    assert called == ["model_change"]


def test_at_most_one_session_is_validated_per_tick(fake_tmux, monkeypatch):
    """Two back-to-back validations in one tick would double the sibling's
    health blackout against its 300s staleness window."""
    fake_tmux.sessions.update({brain.BRAIN_SESSION, brain.IO_SESSION})
    called = []
    monkeypatch.setattr(brain_daemon, "run_handshake",
                        lambda state, reason: called.append(state.name) or False)
    states = {n: _state(n, real_clock=True) for n in (brain.BRAIN_SESSION, brain.IO_SESSION)}
    brain_daemon.tick(states)
    assert len(called) == 1


def test_a_failing_session_cannot_starve_a_healthy_one(fake_tmux, monkeypatch):
    """The level trigger changed the failure shape: edge-triggered validation is
    self-limiting, level-triggered is self-perpetuating. First-in-iteration-order
    would hand every tick to a crash-looping spark-brain forever, and spark-io
    would report failure for never having had a turn."""
    fake_tmux.sessions.update({brain.BRAIN_SESSION, brain.IO_SESSION})
    called = []

    def _fail(state, reason):
        state.last_validation_attempt = time.monotonic()
        called.append(state.name)
        return False

    monkeypatch.setattr(brain_daemon, "run_handshake", _fail)
    states = {n: _state(n, real_clock=True) for n in (brain.BRAIN_SESSION, brain.IO_SESSION)}
    for _ in range(6):
        brain_daemon.tick(states)

    brain_n = called.count(brain.BRAIN_SESSION)
    io_n = called.count(brain.IO_SESSION)
    assert io_n > 0, "a session that never gets a turn reports failure for no fault of its own"
    assert abs(brain_n - io_n) <= 1, f"validation must alternate, saw {called}"


def test_health_success_requires_validation_not_a_glyph(fake_tmux, monkeypatch):
    """Glyph site 7 — the original bug. This is the line that recorded `ok` for
    a session that could not answer a single request, forever.

    Uses `real_clock=True` and asserts the stub was actually consulted: built
    with a fixed (empty) `last_recycle_day` and no `real_clock`, `nightly_due`
    is true for ~22 hours of the day, `tick()`'s recycle fires first, stamps
    `last_recycle_at`, and the quiet window means the stubbed `run_handshake`
    below is never called at all — `status != "ok"` then passes for the
    unrelated reason that the status is `missing`, not because anything
    proved the glyph doesn't count."""
    from pxh import health

    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    fake_tmux.ready = True          # the pane looks perfect
    brain.ensure_mailbox(session)   # and there is no marker
    called = []
    monkeypatch.setattr(brain_daemon, "run_handshake",
                        lambda state, reason: called.append(reason) or False)

    brain_daemon.tick({session: _state(session, real_clock=True)})
    assert called, "the stub must actually be consulted for this test to mean anything"
    record = health.read_health(("px-brain",))["components"]["px-brain"]
    assert record["status"] != "ok", \
        "a ready pane is not evidence of anything; a permission dialog renders one"


def test_a_validated_session_reports_ok(fake_tmux, monkeypatch):
    """The positive control for the test above."""
    from pxh import health

    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    brain.write_validation_marker(session, state="validated", request_id="r",
                                  model=brain.configured_model(session), attempt=1)
    brain_daemon.tick({session: _state(session, real_clock=True)})
    record = health.read_health(("px-brain",))["components"]["px-brain"]
    assert record["status"] == "ok"


def test_validating_records_neither_success_nor_failure(fake_tmux, monkeypatch):
    """Not a success (it cannot serve) and not a failure (it is working on it).
    If it never resolves, staleness catches it — that is the alarm that was
    missing."""
    from pxh import health

    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    brain.write_validation_marker(session, state="validating", request_id="r",
                                  model=brain.configured_model(session), attempt=1)
    monkeypatch.setattr(brain_daemon, "run_handshake",
                        lambda state, reason: pytest.fail("validating is not due"))
    brain_daemon.tick({session: _state(session, real_clock=True)})
    # read_health always lists a requested component; an absent record derives
    # to "missing" (health.py:192), which is exactly "nobody has reported yet".
    record = health.read_health(("px-brain",))["components"]["px-brain"]
    assert record["status"] == "missing", \
        "validating writes neither success nor failure; staleness is the alarm"


def test_session_absent_needs_no_handshake(fake_tmux, monkeypatch):
    """A session that does not exist cannot be handshaked. `start_session`
    recreates it (as `no_marker`, since creation clears the marker) and the
    *next* tick is the one that handshakes it — not this one."""
    session = brain.BRAIN_SESSION
    assert brain.session_state(session) == brain.SESSION_ABSENT
    assert brain_daemon.handshake_reason(_state(session)) is None


def test_a_busy_pane_extends_the_quiet_window_past_its_fixed_floor(fake_tmux):
    """I2: RECYCLE_QUIET_S alone is a floor, not the whole story. A recycle's
    journal+/clear turn is a real Claude turn and can outlast one
    HANDSHAKE_TIMEOUT_S on a slow night — if the pane is still busy once the
    fixed window expires, handshake_reason must keep withholding rather than
    nudge a session that is demonstrably still working, or the nudge cancels
    the very recycle turn the supervisor already believes landed."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    fake_tmux.ready = False  # the pane is still busy with the recycle turn
    brain.ensure_mailbox(session)  # no marker — the recycle already cleared it
    state = _state(session)
    # Past RECYCLE_QUIET_S (60s), well inside VALIDATION_CEILING_S (180s).
    state.last_recycle_at = time.monotonic() - 90

    assert brain_daemon.handshake_reason(state) is None, \
        "a still-busy pane must keep suppressing the handshake past the fixed floor"


def test_a_pane_that_never_goes_idle_is_eventually_handshaked_anyway(fake_tmux):
    """I2's other half: the extension is bounded. An unbounded idle gate on
    this path would break the state machine's closure property — a session
    whose pane never shows a glyph again would never be handshaked and so
    never repaired. Once VALIDATION_CEILING_S has passed since the recycle,
    handshake_reason must stop waiting for idle and return to the level
    trigger, busy pane or not."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    fake_tmux.ready = False  # still busy — this must not matter any more
    brain.ensure_mailbox(session)
    state = _state(session)
    # Past VALIDATION_CEILING_S (180s) since the recycle.
    state.last_recycle_at = time.monotonic() - 190

    assert brain_daemon.handshake_reason(state) == "no_marker", \
        "the extension must not wait on idle forever"


def test_run_handshake_bumps_last_validation_attempt_even_on_a_fast_failure(
        fake_tmux, monkeypatch):
    """The fairness sort in `_validate_one` reads `last_validation_attempt`,
    and `run_handshake` sets it at entry — before the lock, before any I/O —
    specifically so a session that fails immediately still records that it
    was attempted. Nothing else pins that assignment: if it moved below an
    early return, this is the only thing that would catch it, because
    `_validate_one`'s own fairness test fakes the field with its stub rather
    than exercising the real function."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    monkeypatch.setattr(brain, "_lock_for", lambda session: None)
    state = _state(session)
    assert state.last_validation_attempt == 0.0

    assert brain_daemon.run_handshake(state, "no_marker") is False
    assert state.last_validation_attempt > 0.0


# ---------------------------------------------------------------------------
# _is_idle asks about live requests, not files (§2.3)
# ---------------------------------------------------------------------------

def _orphan(session, deadline, request_id="orphan"):
    """A pending inbox entry with nobody waiting on it."""
    brain.ensure_mailbox(session)
    body = {"id": request_id, "kind": "research"}
    if deadline is not None:
        body["deadline"] = deadline
    (brain.inbox_dir(session) / f"{request_id}.json").write_text(json.dumps(body))


def test_a_past_deadline_orphan_does_not_block_a_recycle(fake_tmux):
    """A killed caller leaves an inbox entry `ask_brain`'s finally: would have
    removed. Globbing for files means that session never recycles again — not
    nightly, not on turn count."""
    session = brain.BRAIN_SESSION
    _orphan(session, deadline=time.time() - 5)
    state = _state(session)
    state.turns = brain_daemon.CONTEXT_TURNS

    assert brain_daemon._is_idle(state) is True
    brain_daemon.maybe_recycle(state, datetime(2026, 8, 17, 12, 0, tzinfo=brain_daemon.HOBART))
    assert any("/clear" in text for _, text in fake_tmux.injected), \
        "the point is that a due recycle actually fires"


def test_a_live_request_still_withholds_the_recycle(fake_tmux):
    """A `/clear` between the nudge and the reply loses the request entirely,
    and the caller can only see that as a timeout."""
    session = brain.BRAIN_SESSION
    _orphan(session, deadline=time.time() + 300)
    state = _state(session)
    state.turns = brain_daemon.CONTEXT_TURNS

    assert brain_daemon._is_idle(state) is False
    brain_daemon.maybe_recycle(state, datetime(2026, 8, 17, 12, 0, tzinfo=brain_daemon.HOBART))
    assert not any("/clear" in text for _, text in fake_tmux.injected)


@pytest.mark.parametrize("body", ["{not json", '{"id": "x"}', '{"id": "x", "deadline": "soon"}'])
def test_an_unreadable_deadline_counts_as_live(fake_tmux, body):
    """A predicate that cannot read a deadline must not become a reason to
    recycle over a real request. Same conservative reading as check_wedge's
    isinstance guard next door."""
    session = brain.BRAIN_SESSION
    brain.ensure_mailbox(session)
    (brain.inbox_dir(session) / "weird.json").write_text(body)
    assert brain_daemon._is_idle(_state(session)) is False


def test_the_narrow_sweep_records_the_dead_handshake_and_recycling_recovers(
        fake_tmux, _fast_handshake, monkeypatch):
    """A supervisor killed mid-handshake leaves a request the replacement never
    claims. The sweep is how it reaches dead/ — the audit trail covers what the
    supervisor owns."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    _orphan(session, deadline=time.time() - 5, request_id="deadhs")
    _orphan(session, deadline=time.time() - 5, request_id="someoneelse")
    monkeypatch.setattr(brain, "VALIDATION_CEILING_S", 0.01)
    brain.write_validation_marker(session, state="validating", request_id="deadhs",
                                  model=brain.configured_model(session), attempt=1)
    time.sleep(0.05)
    monkeypatch.setattr(tmux_claude, "inject", _echo_when_nudged(fake_tmux, session))

    brain_daemon.run_handshake(_state(session), "no_marker")

    assert (brain.dead_dir(session) / "deadhs.json").exists(), \
        "the supervisor's own orphan is recorded"
    assert (brain.inbox_dir(session) / "someoneelse.json").exists(), \
        "a request the supervisor did not write is not its to delete"
    state = _state(session)
    state.turns = brain_daemon.CONTEXT_TURNS
    fake_tmux.injected.clear()
    brain_daemon.maybe_recycle(state, datetime(2026, 8, 17, 12, 0, tzinfo=brain_daemon.HOBART))
    assert any("/clear" in text for _, text in fake_tmux.injected), \
        "a test that only checked the file vanished would pass against a fix that swept the wrong one"


# ---------------------------------------------------------------------------
# Model changes and recycles are transitions, not side effects (§2.5)
# ---------------------------------------------------------------------------

def test_a_model_change_clears_the_marker_before_injecting(fake_tmux, _fast_handshake, monkeypatch):
    """Crash-safe direction: a supervisor killed between the two steps drops the
    lock at process death and leaves no marker, so the next reader falls back.
    The reverse order has a window where the marker vouches for a session whose
    context has just been cleared."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    brain.write_validation_marker(session, state="validated", request_id="r",
                                  model="claude-opus-4-6", attempt=1)
    order = []

    def _inject(text, spec=None):
        order.append(("marker" if brain.validation_path(session).exists() else "no-marker", text))
        return _echo_when_nudged(fake_tmux, session)(text, spec)

    monkeypatch.setattr(tmux_claude, "inject", _inject)
    brain_daemon.run_handshake(_state(session), "model_change")

    model_injects = [entry for entry in order if entry[1].startswith("/model")]
    assert model_injects, "a model change injects /model"
    assert model_injects[0][0] == "no-marker", \
        "the marker must be gone before the keystroke that invalidates it"
    assert brain.read_validation_marker(session)["model"] == brain.configured_model(session), \
        "the marker's model is only written by a handshake the new model answered"


def test_a_recycle_holds_the_single_flight_lock(fake_tmux):
    """Today /clear is injected with no lock held at all, which races any
    caller's nudge."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    state = _state(session)
    state.turns = brain_daemon.CONTEXT_TURNS

    held = brain._lock_for(session)
    held.acquire()
    try:
        brain_daemon.maybe_recycle(
            state, datetime(2026, 8, 17, 12, 0, tzinfo=brain_daemon.HOBART))
        assert not any("/clear" in text for _, text in fake_tmux.injected), \
            "a caller mid-turn must not have its context cleared underneath it"
    finally:
        held.release()

    brain_daemon.maybe_recycle(
        state, datetime(2026, 8, 17, 12, 0, tzinfo=brain_daemon.HOBART))
    assert any("/clear" in text for _, text in fake_tmux.injected)


def test_a_recycle_clears_the_marker_so_the_next_tick_re_handshakes(fake_tmux):
    """A session that has just forgotten its identity prompt has not been
    validated on that context. Every recycle is followed by a handshake."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    brain.write_validation_marker(session, state="validated", request_id="r",
                                  model=brain.configured_model(session), attempt=1)
    state = _state(session)
    state.turns = brain_daemon.CONTEXT_TURNS

    brain_daemon.maybe_recycle(
        state, datetime(2026, 8, 17, 12, 0, tzinfo=brain_daemon.HOBART))
    assert brain.session_state(session) == brain.NO_MARKER


def test_a_recycle_is_not_followed_by_a_handshake_in_the_same_tick(
        fake_tmux, _fast_handshake, monkeypatch):
    """maybe_recycle and _validate_one run over the same live list inside one
    tick(). A recycle clears the marker, which makes handshake_reason see
    `no_marker` in that same pass — without the quiet window, _validate_one
    would nudge into the exact pane the recycle just made busy with the
    journal+/clear turn. `_fast_handshake` and the echo hook are only a safety
    net so a regression here fails fast instead of hanging on the real
    handshake timeout."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    brain.write_validation_marker(session, state="validated", request_id="r",
                                  model=brain.configured_model(session), attempt=1)
    monkeypatch.setattr(tmux_claude, "inject", _echo_when_nudged(fake_tmux, session))
    state = _state(session)
    state.turns = brain_daemon.CONTEXT_TURNS

    brain_daemon.tick({session: state})

    assert any("/clear" in text for _, text in fake_tmux.injected), "the recycle fired"
    assert not any("NEW REQUEST" in text for _, text in fake_tmux.injected), \
        "the handshake nudge must not land in the same tick as the recycle"


def test_a_failed_recycle_inject_does_not_advance_the_bookkeeping(fake_tmux, monkeypatch):
    """The marker is already gone by the time the keystroke is attempted. If
    the keystroke itself fails, turns/last_recycle_day/last_recycle_at must
    stay put — advancing them would make the supervisor believe it recycled,
    and wait another CONTEXT_TURNS before trying again, while the context was
    never actually cleared."""
    session = brain.BRAIN_SESSION
    fake_tmux.sessions.add(session)
    brain.ensure_mailbox(session)
    monkeypatch.setattr(tmux_claude, "inject", lambda text, spec=None: False)
    state = _state(session)
    state.turns = brain_daemon.CONTEXT_TURNS
    state.last_recycle_day = "2026-08-01"
    state.last_recycle_at = 0.0

    brain_daemon.maybe_recycle(
        state, datetime(2026, 8, 17, 12, 0, tzinfo=brain_daemon.HOBART))

    assert state.turns == brain_daemon.CONTEXT_TURNS, "a failed inject is not a recycle"
    assert state.last_recycle_day == "2026-08-01"
    assert state.last_recycle_at == 0.0
    assert brain.session_state(session) == brain.NO_MARKER, \
        "the marker clears regardless, so the next tick still handshakes"


# ---------------------------------------------------------------------------
# One supervisor (§2.8)
# ---------------------------------------------------------------------------

def test_a_second_supervisor_refuses_to_start(fake_tmux):
    """start_session, sweep_pending, kill_session and check_wedge all run
    outside the single-flight lock, so a second supervisor can sweep the first
    one's in-flight handshake into dead/ and the handshake then times out
    against a request that no longer exists."""
    assert brain_daemon.acquire_supervisor_lock() is True
    try:
        assert brain_daemon.run(once=True) != 0, \
            "the loser exits non-zero rather than starting"
    finally:
        brain_daemon.release_supervisor_lock()


def test_the_winner_writes_its_pid_as_a_hint(fake_tmux):
    """flock fails with EWOULDBLOCK and nothing else — there is no F_GETLK for
    it — so the holder's pid is not recoverable from the call. The winner writes
    it, and the loser reads it as a hint that may be stale."""
    assert brain_daemon.acquire_supervisor_lock() is True
    try:
        assert brain_daemon.supervisor_lock_path().read_text().strip() == str(os.getpid())
    finally:
        brain_daemon.release_supervisor_lock()


def test_a_live_holder_is_refused_and_a_sigkilled_holder_releases(fake_tmux):
    """The actual production hazard is refusal while the first supervisor is
    still running, not merely after it exits cleanly — and because a
    supervisor can be SIGKILLed, the guard must be released by the kernel at
    death, not by any cleanup code a kill signal skips entirely. A holder
    that runs sequentially and exits normally, as an earlier version of this
    test did, would pass even if acquire_supervisor_lock were a no-op."""
    holder_script = (
        "import os, sys, time;"
        "sys.path.insert(0, %r);"
        "os.environ['PX_STATE_DIR'] = %r;"
        "from pxh import brain, brain_daemon;"
        "brain.brain_root = lambda: __import__('pathlib').Path(%r) / 'brain';"
        "ok = brain_daemon.acquire_supervisor_lock();"
        "print('locked' if ok else 'failed', flush=True);"
        "sys.exit(1) if not ok else time.sleep(60)"
    )
    probe_script = (
        "import os, sys;"
        "sys.path.insert(0, %r);"
        "os.environ['PX_STATE_DIR'] = %r;"
        "from pxh import brain, brain_daemon;"
        "brain.brain_root = lambda: __import__('pathlib').Path(%r) / 'brain';"
        "sys.exit(0 if brain_daemon.acquire_supervisor_lock() else 1)"
    )
    root = str(ROOT / "src")
    state = str(brain.brain_root().parent)
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_script % (root, state, state)],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        # Wait for the holder to confirm it has the guard, rather than
        # sleeping a fixed interval, so the probe below is never racing it.
        signal = holder.stdout.readline().strip()
        assert signal == "locked", "the holder must hold the guard before we probe it"

        probe = subprocess.run([sys.executable, "-c", probe_script % (root, state, state)])
        assert probe.returncode == 1, "a second supervisor is refused while the first is alive"
    finally:
        holder.kill()  # SIGKILL
        holder.wait(timeout=5)

    released = subprocess.run([sys.executable, "-c", probe_script % (root, state, state)])
    assert released.returncode == 0, "the kernel releases the guard when the holder is killed"


def test_px_brain_status_prints_the_state_vocabulary_and_free_space(tmp_path, monkeypatch):
    """The state names are the words a human uses to describe the fault, so the
    tool prints them unchanged rather than prettifying them into a different
    set. Free space is there because `stale` has two causes: health._write_record
    swallows OSError, so a full disk and a dead supervisor look identical."""
    env = os.environ.copy()
    env["PX_STATE_DIR"] = str(tmp_path)
    env["PROJECT_ROOT"] = str(ROOT)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run([str(ROOT / "bin" / "px-brain-status")],
                            capture_output=True, text=True, timeout=30, env=env)
    assert result.returncode == 0, result.stderr
    assert "spark-brain" in result.stdout and "spark-io" in result.stdout
    assert any(word in result.stdout
               for word in ("session_absent", "no_marker", "validating", "validated"))
    assert "free" in result.stdout.lower()
