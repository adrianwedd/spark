"""Tests for px-brain, the supervisor over SPARK's resident Claude sessions.

Every behaviour asserted here exists to stop a specific way of destroying work:
killing a session that was about to answer, replaying requests nobody wants,
clearing context mid-request, or — the quiet one — running without an attached
tmux client so that injection fails only when nobody is watching.
"""
import json
import re
import time
from pathlib import Path

import pytest

from pxh import brain, brain_daemon, tmux_claude

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _mailbox(tmp_path, monkeypatch):
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(brain, "brain_root", lambda: tmp_path / "brain")


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


def _state(name=brain.BRAIN_SESSION):
    return brain_daemon.SessionState(name=name)


def _write_current(session, request_id="r1", deadline=None):
    brain.ensure_mailbox(session)
    brain.current_path(session).write_text(json.dumps({
        "id": request_id,
        "deadline": deadline if deadline is not None else time.time() + 300,
    }))


# ---------------------------------------------------------------------------
# The holder — the reason this daemon exists
# ---------------------------------------------------------------------------

def test_a_holder_is_attached_to_every_session(fake_tmux):
    """tmux 3.3a's send-keys fails with no attached client, and a daemon is
    exactly the case where nothing is watching. No holder, no injection."""
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
    """A queued request is still someone waiting, even before it starts."""
    from datetime import datetime
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


def test_run_once_completes_a_single_pass(fake_tmux):
    assert brain_daemon.run(once=True) == 0
    assert set(fake_tmux.created) == {brain.BRAIN_SESSION, brain.IO_SESSION}


def test_the_journal_is_seeded(fake_tmux):
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
        entries = list(brain.inbox_dir(session).glob("*.json"))
        if not entries:
            return True
        request = json.loads(entries[0].read_text())
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
