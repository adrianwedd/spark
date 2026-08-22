"""Tests for the brain mailbox — the channel that replaces `claude -p`.

The protocol is a filesystem handshake between two processes that never see
each other, so the interesting cases are all failure cases: a session that never
answers, two callers racing, a reply that arrives after the caller gave up, and
a reply tool being fed something a language model made up. The happy path is
covered by a round-trip through the *real* `bin/tool-brain-reply`, because a
mocked reply writer would prove nothing about the handshake.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from pxh import brain, tmux_claude

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "bin" / "tool-brain-reply"


def _reply_spellings(text: str) -> list[str]:
    """Every way `text` names the reply tool, each with its whole prefix.

    Asserting `"tool-brain-reply" in text` is what let three different
    spellings coexist: the substring is present in all of them, and the prefix
    is the entire semantic content, because the allowlist matches on it.
    """
    return re.findall(r"[^\s`'\"]*tool-brain-reply", text)


@pytest.fixture(autouse=True)
def _mailbox(tmp_path, monkeypatch):
    """Point the mailbox at tmp and make waiting instant.

    PX_STATE_DIR is tmp_path itself, not tmp_path/"state", so that the mailbox
    the subprocess tests compute from the environment lands in the same place
    as the one conftest's autouse fixture redirects in-process. A mismatch here
    makes the round-trip tests fail in a way that looks like a protocol bug.
    """
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("PROJECT_ROOT", str(ROOT))
    monkeypatch.setattr(brain, "POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(brain, "LOCK_WAIT_S", 0.5)
    return tmp_path / "state"


@pytest.fixture
def _live_pane(monkeypatch):
    """A tmux session that exists, is validated, and swallows every injection.

    Readiness used to mean "the pane shows the prompt glyph"; now it means "the
    marker says a handshake landed" (§3), so this fixture writes one — the same
    fact ask_brain's session_state() checks, not the glyph it used to check.
    """
    injected: list[str] = []
    monkeypatch.setattr(tmux_claude, "session_exists", lambda spec=None: True)
    monkeypatch.setattr(tmux_claude, "ensure_session", lambda *a, **k: True)
    monkeypatch.setattr(tmux_claude, "inject",
                        lambda text, spec=None: (injected.append(text), True)[1])
    brain.write_validation_marker(brain.BRAIN_SESSION, state="validated",
                                  request_id=str(uuid.uuid4()),
                                  model=brain.configured_model(brain.BRAIN_SESSION),
                                  attempt=1)
    return injected


def _reply_via_tool(session: str, request_id: str, payload: dict) -> subprocess.CompletedProcess:
    """Answer a request the way a real session does — by running the tool."""
    env = os.environ.copy()
    env["PX_BRAIN_SESSION"] = session
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [str(TOOL), request_id, json.dumps(payload)],
        capture_output=True, text=True, timeout=30, env=env,
    )


def _pending_id(session: str, timeout_s: float = 5.0) -> str:
    """Wait for a request to appear in the inbox and return its id."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        entries = list(brain.inbox_dir(session).glob("*.json"))
        if entries:
            return entries[0].stem
        time.sleep(0.01)
    raise AssertionError("no request appeared in the inbox")


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------

def test_round_trip_through_the_real_reply_tool(_live_pane):
    """A fake brain answers via bin/tool-brain-reply and ask_brain returns it."""
    session = brain.session_for_kind("research")
    result: dict = {}

    def _fake_brain():
        request_id = _pending_id(session)
        request = json.loads((brain.inbox_dir(session) / f"{request_id}.json").read_text())
        result["saw"] = request
        _reply_via_tool(session, request_id, {"verdict": "yes"})

    worker = threading.Thread(target=_fake_brain, daemon=True)
    worker.start()
    reply = brain.ask_brain("research", {"question": "why"}, timeout_s=15)
    worker.join(timeout=10)

    assert reply is not None, "a reply written to the outbox must be returned"
    assert reply["reply"] == {"verdict": "yes"}
    assert result["saw"]["payload"] == {"question": "why"}
    assert result["saw"]["kind"] == "research"


def test_nudge_names_the_request_file_and_the_reply_verb(_live_pane):
    """The session may have drifted or been /cleared — the nudge has to carry
    the whole protocol, not assume the system prompt is still in context."""
    session = brain.session_for_kind("research")
    threading.Thread(
        target=lambda: _reply_via_tool(session, _pending_id(session), {"ok": True}),
        daemon=True,
    ).start()
    brain.ask_brain("research", {"q": 1}, timeout_s=15)

    nudge = [line for line in _live_pane if "NEW REQUEST" in line]
    assert nudge, f"no nudge injected, got {_live_pane}"
    assert _reply_spellings(nudge[0]) == [brain.TOOL_BRAIN_REPLY]
    assert str(brain.inbox_dir(session)) in nudge[0]


# ---------------------------------------------------------------------------
# Failure paths — every one of these must fall back, never raise
# ---------------------------------------------------------------------------

def test_timeout_returns_none_so_the_caller_falls_back(_live_pane):
    """No answer is not an error. Callers drop to the Ollama tiers on None."""
    assert brain.ask_brain("research", {"q": 1}, timeout_s=0.2) is None


def test_timeout_clears_current_json(_live_pane):
    """current.json is what wedge detection keys on. A caller-side timeout is
    not a wedge, so an abandoned request must not leave one behind."""
    session = brain.session_for_kind("research")
    brain.ask_brain("research", {"q": 1}, timeout_s=0.2)
    assert not brain.current_path(session).exists()
    assert not list(brain.inbox_dir(session).glob("*.json")), \
        "an abandoned request must not stay pending"


def test_a_dead_session_returns_none(monkeypatch):
    monkeypatch.setattr(tmux_claude, "session_exists", lambda spec=None: False)
    assert brain.ask_brain("research", {"q": 1}, timeout_s=1) is None


def test_second_caller_falls_back_rather_than_interleaving(_live_pane, monkeypatch):
    """Single-flight: two concurrent send-keys runs garble each other, so the
    loser of the lock must fall back instead of waiting behind a long turn."""
    session = brain.session_for_kind("research")
    monkeypatch.setattr(brain, "LOCK_WAIT_S", 0.1)

    results: list = []
    holder = threading.Thread(
        target=lambda: results.append(brain.ask_brain("research", {"q": 1}, timeout_s=2)),
        daemon=True,
    )
    holder.start()
    _pending_id(session)  # the holder now owns the lock
    assert brain.ask_brain("research", {"q": 2}, timeout_s=2) is None, \
        "a second concurrent request must fall back, not queue"
    holder.join(timeout=10)


def test_a_reply_that_lands_after_the_deadline_is_not_returned_later(_live_pane):
    """A late reply belongs to a request nobody is waiting on. It must not be
    picked up by the *next* request, which would answer the wrong question."""
    session = brain.session_for_kind("research")
    assert brain.ask_brain("research", {"q": 1}, timeout_s=0.2) is None
    stale_id = str(uuid.uuid4())
    brain.ensure_mailbox(session)
    (brain.outbox_dir(session) / f"{stale_id}.json").write_text(
        json.dumps({"id": stale_id, "reply": {"verdict": "stale"}}))

    assert brain.ask_brain("research", {"q": 2}, timeout_s=0.2) is None, \
        "an unrelated outbox file must not satisfy a new request"


def test_unparseable_reply_degrades_to_a_timeout(_live_pane, monkeypatch):
    """A half-written file is treated as absent, not as an error — a wrong
    answer is worse than a fallback."""
    session = brain.session_for_kind("research")

    def _garbage():
        request_id = _pending_id(session)
        (brain.outbox_dir(session) / f"{request_id}.json").write_text("{not json")

    threading.Thread(target=_garbage, daemon=True).start()
    assert brain.ask_brain("research", {"q": 1}, timeout_s=0.6) is None


# ---------------------------------------------------------------------------
# Session routing and the trust boundary
# ---------------------------------------------------------------------------

def test_migrated_untrusted_kinds_route_to_m5_not_a_resident_session():
    """Stage 1 keeps untrusted text away from both resident session envelopes."""
    for kind in ("post_qa", "public_chat", "obi_chat"):
        assert brain.session_for_kind(kind) == brain.M5_SESSION, kind
    for kind in ("research", "compose", "evolve"):
        assert brain.session_for_kind(kind) == brain.BRAIN_SESSION, kind


def test_an_unclassified_kind_can_never_reach_the_privileged_session():
    """The default must be the SAFE direction, not the convenient one.

    Routing used to be `IO if kind in _IO_KINDS else BRAIN` — so anything not
    explicitly named as untrusted landed on the session holding SPARK's tools,
    at the repo root. That makes forgetting to classify a new kind the
    dangerous mistake, which is exactly backwards: the person adding a kind
    that chews text from strangers is the person most likely to forget.

    Since Stage 2 (#242) removed the unprivileged `spark-io` session, there is
    no longer a safe session to fall back to at all — `session_for_kind` must
    refuse outright rather than pick between two sessions, one of which no
    longer exists.

    Typos, near-misses and case differences are the realistic shape of this —
    they are not *unknown* to a human reading the diff, only to the frozenset.
    """
    invented = (
        "obi_chat_v2",         # a plausible next kind, handling stranger text
        "public_chat ",        # a stray space
        "POST_QA",             # wrong case
        "webhook",             # something nobody has thought of yet
        "",                    # a caller that forgot to pass one
        "../evolve",           # a kind built from untrusted input
    )
    for kind in invented:
        with pytest.raises(ValueError):
            brain.session_for_kind(kind)


def test_every_kind_with_a_deadline_is_explicitly_classified():
    """A kind real enough to have a deadline is real enough to classify.

    This is the structural half: it fails when someone adds a kind to
    _DEADLINE_S and to nothing else, which is the moment the omission is
    cheap to fix rather than after it has shipped.
    """
    unclassified = [k for k in brain._DEADLINE_S
                    if k not in brain._M5_KINDS and k not in brain._BRAIN_KINDS]
    assert unclassified == [], (
        f"kinds with a deadline but no trust classification: {unclassified}")


def test_ask_brain_refuses_an_unclassified_kind_outright(_live_pane):
    """Least-privilege routing is the backstop; refusing is the front door.

    Asserting on the *injected pane text* rather than on the return value or
    the inbox, because both of those pass for the wrong reason: ask_brain
    returns None for a dozen ordinary causes, and `cleanup_request` unlinks
    the inbox entry on the way out, so a request that WAS sent leaves the
    directory looking exactly like one that was refused.

    An injection is the irreversible step — it is text typed into a live
    Claude — so "nothing was injected" is the only claim worth pinning.
    """
    started = time.monotonic()
    assert brain.ask_brain("a_kind_nobody_declared", {"q": 1},
                           timeout_s=5) is None
    elapsed = time.monotonic() - started

    assert [line for line in _live_pane if "NEW REQUEST" in line] == [], (
        f"an unclassified kind was nudged into a live pane: {_live_pane}")
    assert elapsed < 2.0, (
        f"refusal must be immediate, not a {elapsed:.1f}s deadline wait — a "
        "slow refusal means the request was sent and merely went unanswered")


def test_ask_brain_refuses_an_m5_kind_it_would_otherwise_route(_live_pane):
    """The Stage 2 trust boundary: M5 kinds are classified — session_for_kind
    correctly names M5_SESSION for them — but ask_brain must still refuse
    before injecting anything. Untrusted text stays off Claude Code entirely;
    it is not merely routed to a different, still-Claude, destination.
    """
    started = time.monotonic()
    assert brain.ask_brain("public_chat", {"message": "hi"}, timeout_s=5) is None
    elapsed = time.monotonic() - started

    assert [line for line in _live_pane if "NEW REQUEST" in line] == [], (
        f"an M5-classified kind was nudged into the privileged session: {_live_pane}")
    assert elapsed < 2.0, (
        f"refusal must be immediate, not a {elapsed:.1f}s deadline wait")


# ---------------------------------------------------------------------------
# One spelling of the reply tool
#
# Claude Code matches `Bash(...)` allowlist patterns against the command it is
# about to run, by prefix. `Bash(/abs/bin/tool-brain-reply:*)` therefore admits
# an absolute invocation and nothing else — a bare or repo-relative spelling
# misses the pattern and raises a permission dialog, which is a wedge, because
# nothing is attached to answer it. So the nudge and the system prompt have to
# say one identical absolute thing.
# ---------------------------------------------------------------------------

def _launch_argv(tmp_path, env_extra):
    """Run bin/px-claude-session with a stub `claude` and capture its argv.

    The launcher hands the binary its argv (backgrounded and waited on, not
    `exec`'d, so its exit instrumentation has something left running to
    observe how it stops), so a stub that dumps its arguments shows exactly
    what the real session would have been started with — including the
    rendered system prompt, which is the half of the interface a unit test on
    the Python side cannot see.
    """
    argv_out = tmp_path / "argv"
    stub = tmp_path / "claude-stub"
    stub.write_text('#!/usr/bin/env bash\nprintf "%s\\0" "$@" > "$ARGV_OUT"\n')
    stub.chmod(0o755)

    env = os.environ.copy()
    env.update({"PX_CLAUDE_BIN": str(stub), "ARGV_OUT": str(argv_out)})
    env.pop("PX_CLAUDE_TMUX_PROMPT", None)
    env.pop("PX_CLAUDE_ALLOWED_TOOLS", None)
    # The launcher's exit instrumentation writes
    # state/brain/<session>/last_stderr.log. Without an explicit override
    # here PX_STATE_DIR falls through to this checkout's real state/ —
    # the same class of live-state pollution LOG_DIR isolation exists to
    # prevent elsewhere in this suite.
    env["PX_STATE_DIR"] = str(tmp_path / "state")
    env.update(env_extra)

    run = subprocess.run([str(ROOT / "bin" / "px-claude-session")],
                         capture_output=True, text=True, timeout=60, env=env)
    assert run.returncode == 0, f"launcher failed: {run.stderr}"

    parts = argv_out.read_bytes().decode("utf-8").split("\0")[:-1]
    tools: list[str] = []
    prompt = ""
    i = 0
    while i < len(parts):
        if parts[i] == "--allowedTools":
            i += 1
            while i < len(parts) and not parts[i].startswith("--"):
                tools.append(parts[i])
                i += 1
        elif parts[i] == "--append-system-prompt":
            prompt = parts[i + 1]
            i += 2
        else:
            i += 1
    return tools, prompt


def test_nudge_and_allowlist_agree_on_the_absolute_spelling():
    """The two ends of the permission check, compared directly."""
    assert brain.TOOL_BRAIN_REPLY.startswith("/"), "a relative allowlist cannot match"
    assert brain.TOOL_BRAIN_REPLY_ALLOW == f"Bash({brain.TOOL_BRAIN_REPLY}:*)"
    assert _reply_spellings(brain.nudge_line(brain.BRAIN_SESSION, "abc")) == \
        [brain.TOOL_BRAIN_REPLY]


def test_the_nudge_says_to_run_the_reply_not_type_it():
    """The 2026-08-20 outage: a context-degraded session typed the reply
    command as pane text 300+ times instead of running it as a tool, and the
    pane rendered both identically so nothing caught it for 4h43m. The nudge
    must tell the session to RUN the command and that typing it delivers
    nothing — the earlier bare 'reply with: <command>' wording invited exactly
    the narration that broke it."""
    line = brain.nudge_line(brain.BRAIN_SESSION, "abc")
    lowered = line.lower()
    assert "run" in lowered, "the nudge must say to run the command"
    # It must warn that typing/narrating the command achieves nothing.
    assert "typing" in lowered or "text delivers nothing" in lowered, \
        "the nudge must say that typing the command as text delivers nothing"


def test_launcher_renders_one_absolute_reply_spelling(tmp_path):
    """End to end across the language boundary: what bash actually hands
    `claude` must match what Python tells the session to type."""
    extra = {"PX_BRAIN_SESSION": brain.BRAIN_SESSION}
    tools, prompt = _launch_argv(tmp_path, extra)

    assert brain.TOOL_BRAIN_REPLY_ALLOW in tools, tools
    assert prompt, "no system prompt was passed"
    assert set(_reply_spellings(prompt)) == {brain.TOOL_BRAIN_REPLY}, \
        f"prompt names the tool a way the allowlist will not match: " \
        f"{sorted(set(_reply_spellings(prompt)))}"
    assert "{{" not in prompt, "unsubstituted placeholder left in the rendered prompt"


# ---------------------------------------------------------------------------
# Mailbox housekeeping
# ---------------------------------------------------------------------------

def test_mailbox_dirs_are_world_writable(_mailbox):
    """Mixed-uid writers: a root-created 0755 dir locks every pi daemon out of
    atomic_write's mkstemp. Same lesson as state/health/."""
    brain.ensure_mailbox(brain.BRAIN_SESSION)
    for path in (brain.inbox_dir(brain.BRAIN_SESSION),
                 brain.outbox_dir(brain.BRAIN_SESSION),
                 brain.dead_dir(brain.BRAIN_SESSION)):
        assert path.stat().st_mode & 0o7777 == 0o1777, f"{path} must be 1777"


def test_sweep_moves_pending_requests_to_dead(_mailbox):
    """A restarted session must never replay requests whose callers have
    already fallen back and moved on."""
    session = brain.BRAIN_SESSION
    brain.ensure_mailbox(session)
    ids = [str(uuid.uuid4()) for _ in range(3)]
    for rid in ids:
        (brain.inbox_dir(session) / f"{rid}.json").write_text("{}")
    brain.current_path(session).write_text("{}")

    assert brain.sweep_pending(session) == 3
    assert not list(brain.inbox_dir(session).glob("*.json"))
    assert len(list(brain.dead_dir(session).glob("*.json"))) == 3
    assert not brain.current_path(session).exists()


def test_sweep_one_moves_the_named_entry_to_dead(_mailbox):
    """The narrow sweep: it moves, it does not delete — the orphan is kept as
    a record, the same way sweep_pending keeps its own swept entries."""
    session = brain.BRAIN_SESSION
    brain.ensure_mailbox(session)
    rid = str(uuid.uuid4())
    (brain.inbox_dir(session) / f"{rid}.json").write_text("{}")

    assert brain.sweep_one(session, rid) is True
    assert not (brain.inbox_dir(session) / f"{rid}.json").exists()
    assert (brain.dead_dir(session) / f"{rid}.json").exists()


def test_sweep_one_leaves_a_sibling_inbox_entry_alone(_mailbox):
    """This is the property that makes sweep_one safe to run without the
    single-flight lock: it names its target rather than globbing, so it can
    never pick up a request someone else is still waiting on."""
    session = brain.BRAIN_SESSION
    brain.ensure_mailbox(session)
    target = str(uuid.uuid4())
    sibling = str(uuid.uuid4())
    (brain.inbox_dir(session) / f"{target}.json").write_text("{}")
    (brain.inbox_dir(session) / f"{sibling}.json").write_text("{}")

    brain.sweep_one(session, target)
    assert (brain.inbox_dir(session) / f"{sibling}.json").exists()


def test_sweep_one_on_a_missing_entry_returns_false(_mailbox):
    session = brain.BRAIN_SESSION
    brain.ensure_mailbox(session)
    assert brain.sweep_one(session, str(uuid.uuid4())) is False


def test_meter_counts_every_request(_live_pane):
    """Reflection Tier 2 bypassed budget accounting entirely and spent
    hundreds of unbudgeted calls. ask_brain is the chokepoint that cannot be
    bypassed, so the count lives here."""
    brain.ask_brain("research", {"q": 1}, timeout_s=0.2)
    brain.ask_brain("research", {"q": 2}, timeout_s=0.2)
    assert brain.meter_summary()["by_kind"]["research"] == 2


# ---------------------------------------------------------------------------
# Turn health (#258) — the durable, per-request signal brain_daemon's
# context-recycle and validated-but-slow self-heal detector both read
# ---------------------------------------------------------------------------

def test_ask_brain_records_a_delivered_turn_even_on_a_timeout(_live_pane):
    """A caller-side timeout is not "nothing happened" — the nudge was
    injected and the session spent real context on it, so it must still be
    counted (#258's fix for count_turns's tick-sampling gap), and an
    unambiguous miss counts as slow for the self-heal detector too."""
    session = brain.session_for_kind("research")
    assert brain.ask_brain("research", {"q": 1}, timeout_s=0.2) is None
    health = brain.read_turn_health(session)
    assert health["count"] == 1
    assert health["consecutive_slow"] == 1


def test_ask_brain_never_counts_a_request_whose_inject_failed(monkeypatch, _live_pane):
    """Only a turn actually delivered to the session should count — matching
    the pre-existing turn-counting semantics count_turns always had."""
    session = brain.session_for_kind("research")
    monkeypatch.setattr(tmux_claude, "inject", lambda text, spec=None: False)
    assert brain.ask_brain("research", {"q": 1}, timeout_s=0.2) is None
    assert brain.read_turn_health(session)["count"] == 0


def test_ask_brain_marks_a_fast_reply_as_not_slow(_live_pane):
    session = brain.session_for_kind("research")

    def _fast_brain():
        request_id = _pending_id(session)
        _reply_via_tool(session, request_id, {"ok": True})

    threading.Thread(target=_fast_brain, daemon=True).start()
    assert brain.ask_brain("research", {"q": 1}, timeout_s=15) is not None
    health = brain.read_turn_health(session)
    assert health["count"] == 1
    assert health["consecutive_slow"] == 0


def test_ask_brain_marks_a_reply_past_the_slow_fraction_as_slow(_live_pane):
    """A reply that lands but eats most of its own deadline is degradation
    the supervisor needs to see even though the caller got an answer —
    SELF_HEAL_SLOW_FRACTION (0.8) of a 5s deadline is 4s. Writes the outbox
    reply directly rather than through the real tool-brain-reply subprocess:
    this test is about the threshold arithmetic, and subprocess-spawn jitter
    would make a real round trip an unreliable clock for it."""
    session = brain.session_for_kind("research")

    def _slow_brain():
        request_id = _pending_id(session)
        time.sleep(4.3)
        brain.ensure_mailbox(session)
        (brain.outbox_dir(session) / f"{request_id}.json").write_text(
            json.dumps({"id": request_id, "reply": {"ok": True}}))

    threading.Thread(target=_slow_brain, daemon=True).start()
    assert brain.ask_brain("research", {"q": 1}, timeout_s=5) is not None
    assert brain.read_turn_health(session)["consecutive_slow"] == 1


def test_ask_brain_reply_separates_lock_wait_from_execution_time(_live_pane, monkeypatch):
    """duration_s alone conflates queue contention (another kind holding the
    single-flight lock) with the resident session actually taking a long
    time — Track B of the 2026-08-22 assay needs the two told apart to tell
    whether tail latency is contention or slow model execution.

    The queue wait is simulated deterministically by wrapping the real lock's
    acquire() rather than by genuinely contending it from two threads: this
    codebase's own test_second_caller_falls_back_rather_than_interleaving
    already establishes that two same-process FileLock instances on this
    filelock install do not block-and-retry against each other the way two
    separate OS processes do — real cross-request contention (the case this
    field exists to diagnose) only arises across processes in production."""
    session = brain.session_for_kind("research")
    captured = []
    monkeypatch.setattr(brain, "_log", lambda event, **f: captured.append((event, f)))

    real_lock_for = brain._lock_for

    def _slow_to_acquire(sess):
        lock = real_lock_for(sess)
        real_acquire = lock.acquire

        def _acquire(*a, **k):
            time.sleep(0.2)
            return real_acquire(*a, **k)

        lock.acquire = _acquire
        return lock

    monkeypatch.setattr(brain, "_lock_for", _slow_to_acquire)

    def _reply():
        # Generous margins throughout: a real subprocess spawn (tool-brain-reply)
        # has been observed taking up to ~6.8s under this Pi's real load
        # (feedback_spark_subprocess_test_pitfalls), so a tight budget here is
        # exactly what makes this class of test flaky under the full suite
        # rather than in isolation.
        request_id = _pending_id(session, timeout_s=15.0)
        _reply_via_tool(session, request_id, {"ok": True})

    threading.Thread(target=_reply, daemon=True).start()
    assert brain.ask_brain("research", {"q": 1}, timeout_s=30) is not None

    replies = [fields for event, fields in captured if event == "brain_reply"]
    assert len(replies) == 1
    fields = replies[0]
    assert fields["lock_wait_s"] >= 0.15, "queue wait should reflect the slow acquire"
    assert fields["exec_s"] >= 0
    # duration_s spans the same start/end as lock_wait_s + exec_s combined.
    assert abs(fields["duration_s"] - (fields["lock_wait_s"] + fields["exec_s"])) < 0.1
    assert fields["turns_since_reset"] == 0
    assert fields["consecutive_slow_before"] == 0


def test_ask_brain_timeout_also_reports_lock_wait_and_exec_time(_live_pane, monkeypatch):
    session = brain.session_for_kind("research")
    captured = []
    monkeypatch.setattr(brain, "_log", lambda event, **f: captured.append((event, f)))

    assert brain.ask_brain("research", {"q": 1}, timeout_s=0.2) is None

    timeouts = [fields for event, fields in captured if event == "brain_timeout"]
    assert len(timeouts) == 1
    fields = timeouts[0]
    assert fields["lock_wait_s"] >= 0
    assert fields["exec_s"] >= 0.15, "exec time should span the full unanswered deadline"
    assert fields["turns_since_reset"] == 0


def test_consecutive_slow_streak_resets_on_a_fast_reply(_live_pane):
    """The streak measures a *run* of degraded turns; one real fast answer
    proves the session recovered on its own and starts it over — the same
    principle as consecutive_handshake_failures."""
    session = brain.session_for_kind("research")
    assert brain.ask_brain("research", {"q": 1}, timeout_s=0.2) is None  # times out -> slow
    assert brain.read_turn_health(session)["consecutive_slow"] == 1

    def _fast_brain():
        request_id = _pending_id(session)
        _reply_via_tool(session, request_id, {"ok": True})

    threading.Thread(target=_fast_brain, daemon=True).start()
    assert brain.ask_brain("research", {"q": 2}, timeout_s=15) is not None
    health = brain.read_turn_health(session)
    assert health["consecutive_slow"] == 0
    assert health["count"] == 2


def test_reset_consecutive_slow_turns_preserves_the_total_count():
    session = brain.BRAIN_SESSION
    brain.ensure_mailbox(session)
    brain.record_turn_completion(session, slow=True)
    brain.record_turn_completion(session, slow=True)
    brain.reset_consecutive_slow_turns(session)
    health = brain.read_turn_health(session)
    assert health["consecutive_slow"] == 0
    assert health["count"] == 2, "resetting the streak must not touch the turn total"


def test_read_turn_health_defaults_when_unwritten():
    assert brain.read_turn_health("no-such-session") == {"count": 0, "consecutive_slow": 0}


def test_turn_health_survives_corrupt_json(monkeypatch, tmp_path):
    session = brain.BRAIN_SESSION
    brain.ensure_mailbox(session)
    brain._turn_health_path(session).write_text("{not json")
    assert brain.read_turn_health(session) == {"count": 0, "consecutive_slow": 0}
    # A write after a corrupt read must recover rather than propagate the corruption.
    brain.record_turn_completion(session, slow=False)
    assert brain.read_turn_health(session) == {"count": 1, "consecutive_slow": 0}


# ---------------------------------------------------------------------------
# tool-brain-reply validation — the io session's only tool
# ---------------------------------------------------------------------------

def _tool_env(session: str) -> dict:
    env = os.environ.copy()
    env["PX_BRAIN_SESSION"] = session
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _run_tool(session: str, request_id: str, raw_payload: str):
    return subprocess.run([str(TOOL), request_id, raw_payload],
                          capture_output=True, text=True, timeout=30,
                          env=_tool_env(session))


def _run_tool_stdin(session: str, request_id: str, raw_payload: str):
    """Feed the payload on stdin — the only transport that survives a big reply.

    Linux caps every argv *and* envp string at MAX_ARG_STRLEN (32 pages =
    131072 bytes), so anything larger cannot be handed over as an argument at
    all: the caller's own execve fails before the tool runs.
    """
    return subprocess.run([str(TOOL), request_id, "--stdin"], input=raw_payload,
                          capture_output=True, text=True, timeout=30,
                          env=_tool_env(session))


@pytest.mark.parametrize("bad_id", [
    "../../../etc/passwd",
    "not-a-uuid",
    "",
    "../" + str(uuid.uuid4()),
    str(uuid.uuid4()) + "/../escape",
])
def test_reply_tool_rejects_ids_that_are_not_bare_uuids(_mailbox, bad_id):
    """The id becomes a filename. This is the path-traversal guard."""
    brain.ensure_mailbox(brain.BRAIN_SESSION)
    out = _run_tool(brain.BRAIN_SESSION, bad_id, '{"a": 1}')
    payload = json.loads(out.stdout.strip().splitlines()[-1])
    assert payload["status"] == "error", payload
    assert not list(brain.outbox_dir(brain.BRAIN_SESSION).glob("*")), \
        "a rejected id must not write anything"


def test_reply_tool_rejects_an_id_nobody_is_waiting_on(_mailbox):
    """Without this, a valid uuid is a write primitive aimed at the outbox."""
    brain.ensure_mailbox(brain.BRAIN_SESSION)
    out = _run_tool(brain.BRAIN_SESSION, str(uuid.uuid4()), '{"a": 1}')
    assert json.loads(out.stdout.strip().splitlines()[-1])["status"] == "error"


def test_reply_tool_rejects_non_json_and_oversized_payloads(_mailbox):
    session = brain.BRAIN_SESSION
    brain.ensure_mailbox(session)
    rid = str(uuid.uuid4())
    (brain.inbox_dir(session) / f"{rid}.json").write_text("{}")

    bad = _run_tool(session, rid, "this is not json")
    assert json.loads(bad.stdout.strip().splitlines()[-1])["status"] == "error"

    huge = json.dumps({"x": "a" * (brain.MAX_REPLY_BYTES + 1)})
    big = _run_tool_stdin(session, rid, huge)
    assert json.loads(big.stdout.strip().splitlines()[-1])["status"] == "error"

    assert (brain.inbox_dir(session) / f"{rid}.json").exists(), \
        "a rejected reply must leave the request pending"


def test_reply_tool_accepts_a_payload_the_environment_could_not_carry(_mailbox):
    """A legal reply must not be capped by the kernel's per-string exec limit.

    The tool used to hand the payload to its Python step through an environment
    variable. Linux caps every argv/envp string at MAX_ARG_STRLEN (32 pages =
    131072 bytes), which is *half* MAX_REPLY_BYTES — so a reply in that band
    died at execve with exit 126 and an empty stdout, breaking the one-JSON-
    object contract every tool has, and making the tool's own size guard
    unreachable dead code. Sized above the kernel limit and below our own.
    """
    session = brain.BRAIN_SESSION
    brain.ensure_mailbox(session)
    rid = str(uuid.uuid4())
    (brain.inbox_dir(session) / f"{rid}.json").write_text("{}")

    body = "a" * 200_000
    assert len(body) > 131072, "must exceed MAX_ARG_STRLEN to pin the bug"
    assert len(body) < brain.MAX_REPLY_BYTES, "must stay a legal reply"

    out = _run_tool_stdin(session, rid, json.dumps({"essay": body}))

    assert out.stdout.strip(), f"tool emitted nothing (rc={out.returncode}): {out.stderr[:200]}"
    assert json.loads(out.stdout.strip().splitlines()[-1])["status"] == "ok", out.stderr
    written = json.loads((brain.outbox_dir(session) / f"{rid}.json").read_text())
    assert written["reply"]["essay"] == body


def test_reply_tool_retires_the_request_on_success(_mailbox):
    session = brain.BRAIN_SESSION
    brain.ensure_mailbox(session)
    rid = str(uuid.uuid4())
    (brain.inbox_dir(session) / f"{rid}.json").write_text("{}")

    out = _run_tool(session, rid, '{"verdict": "yes"}')
    assert json.loads(out.stdout.strip().splitlines()[-1])["status"] == "ok", out.stdout
    assert not (brain.inbox_dir(session) / f"{rid}.json").exists()
    written = json.loads((brain.outbox_dir(session) / f"{rid}.json").read_text())
    assert written["reply"] == {"verdict": "yes"}


# ---------------------------------------------------------------------------
# Async seam
# ---------------------------------------------------------------------------

def test_ask_brain_async_does_not_block_the_event_loop(monkeypatch):
    """api.py's handlers are async. A blocking poll on the loop thread would
    stall every other request for the length of a Claude turn."""
    import asyncio

    loop_thread = threading.get_ident()
    seen: dict = {}

    def _fake_ask(kind, payload, timeout_s=None, model=None):
        seen["thread"] = threading.get_ident()
        return {"reply": "ok"}

    monkeypatch.setattr(brain, "ask_brain", _fake_ask)

    async def _go():
        return await brain.ask_brain_async("public_chat", {"m": "hi"})

    assert asyncio.run(_go()) == {"reply": "ok"}
    assert seen["thread"] != loop_thread, "ask_brain must run off the event loop"


def test_module_contains_no_claude_p_subprocess():
    """The whole point. A regression here is the thing Adrian has been asking
    to be rid of for months."""
    source = (ROOT / "src" / "pxh" / "brain.py").read_text()
    assert '"-p"' not in source and "'-p'" not in source
    # Prose about subprocesses is fine — reaching for one is not.
    assert "import subprocess" not in source
    assert "subprocess.run" not in source and "subprocess.Popen" not in source, \
        "brain.py must reach Claude through the resident session, not a subprocess"


# ---------------------------------------------------------------------------
# Validation marker and derived state (§2.1, §2.2, §2.6)
# ---------------------------------------------------------------------------

@pytest.fixture
def _session_present(monkeypatch):
    """tmux has the session. Says nothing about whether it can answer."""
    monkeypatch.setattr(tmux_claude, "session_exists", lambda spec=None: True)


@pytest.fixture
def _session_missing(monkeypatch):
    monkeypatch.setattr(tmux_claude, "session_exists", lambda spec=None: False)


def test_a_marker_absent_on_a_live_session_is_no_marker(_mailbox, _session_present):
    """The loud state: the session is up and nothing is handshaking it."""
    assert brain.session_state(brain.BRAIN_SESSION) == brain.NO_MARKER


def test_no_session_at_all_is_its_own_state(_mailbox, _session_missing):
    """`session_absent` and `no_marker` are two different repairs — px-brain is
    down, versus the session is up and cannot answer."""
    brain.write_validation_marker(brain.BRAIN_SESSION, state="validated",
                                  request_id=str(uuid.uuid4()),
                                  model="claude-haiku-4-5-20251001", attempt=1)
    assert brain.session_state(brain.BRAIN_SESSION) == brain.SESSION_ABSENT, \
        "a marker must never outvote tmux — a dead supervisor cannot leave a lying validated behind"


def test_a_fresh_validating_marker_is_quiet(_mailbox, _session_present):
    """`validating` covers every boot and every nightly recycle. An alarm that
    fires on healthy operation several times a day is un-taught within a week."""
    brain.write_validation_marker(brain.BRAIN_SESSION, state="validating",
                                  request_id=str(uuid.uuid4()),
                                  model="claude-haiku-4-5-20251001", attempt=1)
    assert brain.session_state(brain.BRAIN_SESSION) == brain.VALIDATING


def test_a_stale_validating_marker_degrades_to_no_marker(_mailbox, _session_present, monkeypatch):
    """A supervisor killed mid-handshake leaves exactly this, and the repair is
    the same as any other 'nobody is working on it'."""
    monkeypatch.setattr(brain, "VALIDATION_CEILING_S", 0.05)
    brain.write_validation_marker(brain.BRAIN_SESSION, state="validating",
                                  request_id=str(uuid.uuid4()),
                                  model="claude-haiku-4-5-20251001", attempt=1)
    time.sleep(0.1)
    assert brain.session_state(brain.BRAIN_SESSION) == brain.NO_MARKER


def test_a_validated_marker_is_validated(_mailbox, _session_present):
    brain.write_validation_marker(brain.BRAIN_SESSION, state="validated",
                                  request_id=str(uuid.uuid4()),
                                  model="claude-haiku-4-5-20251001", attempt=2)
    assert brain.session_state(brain.BRAIN_SESSION) == brain.VALIDATED


def test_a_caller_naming_a_model_the_marker_does_not_carry_is_not_validated(
        _mailbox, _session_present):
    """A session's model is a property of the session. One caller must not be
    able to retune the mind out from under the next one, so it falls back."""
    brain.write_validation_marker(brain.BRAIN_SESSION, state="validated",
                                  request_id=str(uuid.uuid4()),
                                  model="claude-haiku-4-5-20251001", attempt=1)
    assert brain.session_state(brain.BRAIN_SESSION, model="claude-opus-4-6") != brain.VALIDATED
    assert brain.session_state(brain.BRAIN_SESSION,
                               model="claude-haiku-4-5-20251001") == brain.VALIDATED


def test_a_caller_that_names_no_model_accepts_the_session_default(_mailbox, _session_present):
    brain.write_validation_marker(brain.BRAIN_SESSION, state="validated",
                                  request_id=str(uuid.uuid4()),
                                  model="claude-haiku-4-5-20251001", attempt=1)
    assert brain.session_state(brain.BRAIN_SESSION) == brain.VALIDATED


def test_a_corrupt_marker_reads_as_no_marker(_mailbox, _session_present):
    """Unparseable is not validated. The one direction this may fail is quiet."""
    brain.ensure_mailbox(brain.BRAIN_SESSION)
    brain.validation_path(brain.BRAIN_SESSION).write_text("{not json")
    assert brain.session_state(brain.BRAIN_SESSION) == brain.NO_MARKER


def test_an_invalid_utf8_marker_reads_as_no_marker_not_a_raised_exception(
        _mailbox, _session_present):
    """A `UnicodeDecodeError` is a `ValueError`, not an `OSError` — the narrower
    except clause this pins against let SD-card corruption raise straight out
    of `read_validation_marker`, up through `session_state`, and into
    `ask_brain`, which the module docstring promises never happens."""
    brain.ensure_mailbox(brain.BRAIN_SESSION)
    brain.validation_path(brain.BRAIN_SESSION).write_bytes(b"\xff\xfe\x00bad")
    assert brain.read_validation_marker(brain.BRAIN_SESSION) is None
    assert brain.session_state(brain.BRAIN_SESSION) == brain.NO_MARKER
    assert brain.ask_brain("research", {"x": 1}) is None


def test_the_marker_is_single_writer_readable_not_world_writable(_mailbox, _session_present):
    """The 1777 reasoning for the mailbox does not transfer: one writer, and
    write permission for uids that never write would let a confused caller
    forge a validated marker."""
    brain.write_validation_marker(brain.BRAIN_SESSION, state="validated",
                                  request_id=str(uuid.uuid4()),
                                  model="claude-haiku-4-5-20251001", attempt=1)
    mode = brain.validation_path(brain.BRAIN_SESSION).stat().st_mode & 0o777
    assert mode == 0o644, f"marker mode is {oct(mode)}, must be 0o644 (readable by all, writable by pi)"


def test_validation_budget_fits_inside_the_staleness_window():
    """The bound that has to hold (§2.6), read from the modules rather than from
    literals: if someone adds a second glyph wait, the identity below stops
    describing the code and this test is what forces the conversation."""
    from pxh import health

    assert brain.STARTUP_CEILING_S is tmux_claude.STARTUP_TIMEOUT_S, \
        "there is ONE glyph wait per session start and it lives in ensure_session"
    ceiling = 0.6 * health.STALE_AFTER_S["px-brain"]
    assert brain.VALIDATION_CEILING_S == ceiling
    budget = (brain.STARTUP_CEILING_S + brain.SETTLE_S
              + brain.HANDSHAKE_ATTEMPTS * brain.HANDSHAKE_TIMEOUT_S)
    assert budget <= brain.VALIDATION_CEILING_S, (
        f"{budget}s of validation exceeds the {brain.VALIDATION_CEILING_S}s ceiling; the fix is "
        "a state machine that advances one step per tick, not a bigger number")


def test_the_configured_model_default_matches_the_launcher(monkeypatch):
    """brain.configured_model() and bin/px-claude-session must agree on the
    default, or the supervisor sees a permanent model mismatch and re-handshakes
    a healthy session forever."""
    monkeypatch.delenv("PX_CLAUDE_TMUX_MODEL", raising=False)
    launcher = (ROOT / "bin" / "px-claude-session").read_text()
    assert f'MODEL="${{PX_CLAUDE_TMUX_MODEL:-{brain.configured_model(brain.BRAIN_SESSION)}}}"' in launcher


# ---------------------------------------------------------------------------
# Callers read the marker, never the pane (§3)
# ---------------------------------------------------------------------------

def _validate(session, model=None):
    """Mark a session as having answered a handshake."""
    brain.write_validation_marker(session, state="validated",
                                  request_id=str(uuid.uuid4()),
                                  model=model or brain.configured_model(session),
                                  attempt=1)


def test_an_unvalidated_session_is_not_injected_into(_mailbox, monkeypatch):
    """The pane may look perfect — a permission dialog renders a prompt glyph.
    Injecting anyway is how a caller times out against a session that was never
    going to answer."""
    injected = []
    monkeypatch.setattr(tmux_claude, "session_exists", lambda spec=None: True)
    monkeypatch.setattr(tmux_claude, "pane_ready", lambda spec=None: True)
    monkeypatch.setattr(tmux_claude, "inject",
                        lambda text, spec=None: injected.append(text) or True)

    assert brain.ask_brain("research", {"q": "why"}, timeout_s=1) is None
    assert injected == [], "a caller must never inject into an unvalidated session"
    assert not list(brain.inbox_dir(brain.BRAIN_SESSION).glob("*.json")), \
        "and must not leave a request file behind either"


def test_the_pre_lock_check_is_fast_and_takes_no_lock(_mailbox, monkeypatch):
    """The common case during startup. A caller that queued behind the
    supervisor's lock would burn LOCK_WAIT_S to learn what the marker already
    said."""
    monkeypatch.setattr(tmux_claude, "session_exists", lambda spec=None: True)
    monkeypatch.setattr(brain, "_lock_for", lambda session: pytest.fail(
        "the pre-lock check must return before the lock is touched"))
    assert brain.ask_brain("research", {"q": "why"}, timeout_s=1) is None


def test_the_post_lock_recheck_prevents_a_confident_wrong_answer(_mailbox, _live_pane, monkeypatch):
    """A caller can pass the pre-lock check, block on acquire(), and wake on the
    far side of the supervisor's /clear. Injecting then produces a confident
    answer generated with no context — which is worse than an error, because
    nothing downstream can tell."""
    session = brain.BRAIN_SESSION
    monkeypatch.setattr(tmux_claude, "session_exists", lambda spec=None: True)
    _validate(session)

    real_lock_for = brain._lock_for

    def _invalidating_lock(sess):
        lock = real_lock_for(sess)
        real_acquire = lock.acquire

        def _acquire(*args, **kwargs):
            result = real_acquire(*args, **kwargs)
            brain.clear_validation_marker(sess)  # the supervisor's /clear lands
            return result

        lock.acquire = _acquire
        return lock

    monkeypatch.setattr(brain, "_lock_for", _invalidating_lock)

    assert brain.ask_brain("research", {"q": "why"}, timeout_s=1) is None
    assert _live_pane == [], \
        "the symptom being prevented is a confident wrong answer, not an error"


def test_a_post_lock_bailout_leaves_a_foreign_current_json_alone(_mailbox, _live_pane, monkeypatch):
    """A caller that bails at the post-lock recheck still runs cleanup_request
    in its `finally`. current.json may belong to someone else's in-flight
    request at that moment — deleting it unconditionally would report a
    healthy request as wedged."""
    session = brain.BRAIN_SESSION
    monkeypatch.setattr(tmux_claude, "session_exists", lambda spec=None: True)
    _validate(session)

    foreign_id = str(uuid.uuid4())
    brain.ensure_mailbox(session)
    brain.current_path(session).write_text(json.dumps(
        {"id": foreign_id, "kind": "evolve", "deadline": time.time() + 999}))

    real_lock_for = brain._lock_for

    def _invalidating_lock(sess):
        lock = real_lock_for(sess)
        real_acquire = lock.acquire

        def _acquire(*args, **kwargs):
            result = real_acquire(*args, **kwargs)
            brain.clear_validation_marker(sess)  # the supervisor's /clear lands
            return result

        lock.acquire = _acquire
        return lock

    monkeypatch.setattr(brain, "_lock_for", _invalidating_lock)

    assert brain.ask_brain("research", {"q": "why"}, timeout_s=1) is None
    current = json.loads(brain.current_path(session).read_text())
    assert current["id"] == foreign_id, \
        "cleanup_request must not delete a current.json it does not own"


@pytest.mark.parametrize("garbage", ["{not json", "[]", "null", '{"kind": "evolve"}'])
def test_cleanup_request_removes_a_current_json_nothing_could_ever_claim(_mailbox, garbage):
    """The flip side of the fix above: a current.json that is corrupt, not an
    object, or missing "id" can never match any request's id, so leaving it
    behind is the same permanent-stale-file bug the id check was written to
    stop — just reached from the opposite direction. Only a readable,
    id-bearing file naming someone else is left alone."""
    session = brain.BRAIN_SESSION
    brain.ensure_mailbox(session)
    brain.current_path(session).write_text(garbage)

    brain.cleanup_request(session, str(uuid.uuid4()))

    assert not brain.current_path(session).exists(), \
        f"an unclaimable current.json ({garbage!r}) must be removed, not left forever"


def test_a_validated_session_is_used(_mailbox, _live_pane, monkeypatch):
    """The positive control: without this, every test above would pass against a
    version of ask_brain that never works at all."""
    session = brain.BRAIN_SESSION
    monkeypatch.setattr(tmux_claude, "session_exists", lambda spec=None: True)
    _validate(session)

    def _fake_brain():
        request_id = _pending_id(session)
        _reply_via_tool(session, request_id, {"verdict": "yes"})

    worker = threading.Thread(target=_fake_brain, daemon=True)
    worker.start()
    reply = brain.ask_brain("research", {"q": "why"}, timeout_s=15)
    worker.join(timeout=10)
    assert reply is not None and reply["reply"] == {"verdict": "yes"}


def test_a_caller_never_injects_a_model_switch(_mailbox, _live_pane, monkeypatch):
    """A session's model is a property of the session. Switching it per request
    retunes the mind out from under the next caller."""
    session = brain.BRAIN_SESSION
    monkeypatch.setattr(tmux_claude, "session_exists", lambda spec=None: True)
    _validate(session, model="claude-haiku-4-5-20251001")

    assert brain.ask_brain("research", {"q": "why"},
                           timeout_s=1, model="claude-opus-4-6") is None
    assert not any("/model" in text for text in _live_pane), \
        "ask_brain must fall back on a model mismatch, never inject /model"


def test_every_rolled_out_kind_matches_the_session_model():
    """A kind whose model differs from the session default would fall back on
    every single call, forever, silently. Failing here is how PX_BRAIN_KINDS
    gets widened deliberately rather than by accident."""
    from pxh import claude_session

    default_kinds = ("research", "compose", "post_qa")
    for kind in default_kinds:
        model = claude_session._DEFAULT_MODELS.get(kind)
        if model is None:
            continue  # post_qa has no claude_session entry; it names no model
        assert model == brain.configured_model(brain.session_for_kind(kind)), (
            f"{kind} asks for {model} but its session runs "
            f"{brain.configured_model(brain.session_for_kind(kind))}")


def test_brain_module_no_longer_consults_the_glyph():
    """Glyph sites 3 and 4 (§3.1). The pane is for humans; callers read the
    marker. A test rather than a comment, so the pair cannot quietly come back."""
    source = (ROOT / "src" / "pxh" / "brain.py").read_text()
    assert "pane_ready" not in source, \
        "brain.py must not consult the prompt glyph — readiness is the marker"


def test_pane_ready_does_not_claim_the_session_can_answer():
    """The docstring said "True once Claude is actually listening", which is the
    claim the first end-to-end run disproved: a permission dialog renders the
    glyph. It means the pane is accepting input, and nothing more."""
    doc = tmux_claude.pane_ready.__doc__ or ""
    assert "accepting input" in doc
    assert "actually listening" not in doc


def test_check_wedge_carries_its_warning_in_the_code():
    """A limitation recorded only in a spec is a limitation the next reader
    re-derives from scratch after it bites them a second time."""
    source = (ROOT / "src" / "pxh" / "brain_daemon.py").read_text()
    branch = source.split("def check_wedge")[1].split("def ")[0]
    assert "permission dialog" in branch, \
        "the tolerated weakness must be labelled where it lives"


# --------------------------------------------------------------------------
# The never-raises contract, against corrupt state on disk
#
# Every one of these is the same shape: a file that `read_text(encoding="utf-8")`
# cannot decode raises `UnicodeDecodeError` — a `ValueError`, not an `OSError` —
# so an except clause naming only `OSError` and `json.JSONDecodeError` lets it
# through. `read_validation_marker` was widened for exactly this; these are the
# sites on the same `ask_brain` chain that were not.
# --------------------------------------------------------------------------

def test_corrupt_meter_does_not_break_the_never_raises_contract(_mailbox):
    """record_request is called on every ask_brain, from a daemon."""
    brain.ensure_mailbox(brain.BRAIN_SESSION)
    brain._meter_path().write_bytes(b"\xff\xfe not utf-8 at all")
    brain.record_request("research")  # must not raise
    assert brain.meter_summary()["by_kind"] == {"research": 1}


def test_meter_holding_valid_json_that_is_not_an_object_recovers(_mailbox):
    """`[]` parses fine, then `.get` raises AttributeError on a list."""
    brain.ensure_mailbox(brain.BRAIN_SESSION)
    brain._meter_path().write_text("[1, 2, 3]", encoding="utf-8")
    brain.record_request("compose")  # must not raise
    assert brain.meter_summary()["by_kind"] == {"compose": 1}


def test_corrupt_current_json_does_not_escape_cleanup_request(_mailbox):
    """The sharpest of the three: cleanup_request runs in ask_brain's `finally`,
    *before* lock.release(). An exception here does not just break the contract,
    it leaks the single-flight lock and wedges the session for the process."""
    brain.ensure_mailbox(brain.BRAIN_SESSION)
    brain.current_path(brain.BRAIN_SESSION).write_bytes(b"\xff\xfe\x00")
    brain.cleanup_request(brain.BRAIN_SESSION, str(uuid.uuid4()))  # must not raise
    assert not brain.current_path(brain.BRAIN_SESSION).exists(), \
        "an undecodable current.json is unclaimable — it must be unlinked, not left"


def test_ask_brain_returns_none_on_an_unserializable_payload(_live_pane):
    """`json.dumps` raises TypeError, not OSError. The caller is a daemon that
    has a fallback for None and no handler for an exception."""
    assert brain.ask_brain("research", {"bad": {object()}}, timeout_s=1.0) is None


def test_ask_brain_releases_the_lock_after_an_unserializable_payload(_live_pane):
    """Proof the failure above is not merely quiet: the next caller still runs."""
    brain.ask_brain("research", {"bad": {object()}}, timeout_s=1.0)
    lock = brain._lock_for(brain.BRAIN_SESSION)
    lock.acquire(timeout=0.5)  # would raise Timeout if the first call leaked it
    lock.release()
