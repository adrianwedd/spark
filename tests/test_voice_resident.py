"""The "Hey Spark" turn answers from the cognition tier, and never spawns Claude.

These assertions are process-level on purpose. Mocking a fallback ladder and
checking it was not entered would pass just as happily against a rewritten
ladder; what must hold is the physical fact that no process appears on the Pi.
So the tests poison `subprocess` inside the module under test and assert
nothing reaches it — the fixture is broader than "no argv containing claude"
because a fallback that shells out to something else expensive would satisfy a
claude-only check and still be the bug.

Context: 2026-08-19. A 5s tmux delivery timeout was reported as "brain
unavailable", reflection fell through to `claude -p`, that process competed
with the voice turn's own `claude -p`, and a child waited 151 seconds. The
brain was healthy and idle the whole time.

Since #317 Phase 3 the destination is the cognition tier (`pxh.m5`), so the
*transport* changed and the policy did not: failure still reduces work, nothing
escalates, and the acknowledgement is still a local constant. The one rule that
had to be re-stated rather than deleted is the retry bound — `ask_brain`
collapsed every failure into `None` and the old rule inferred the cause from
elapsed time, while the tier reports it. `busy` and `timeout` are the same
"do not ask twice" case; `offline`/`bad_response` are the same "a cheap retry
actually fixes this" case.
"""
from __future__ import annotations

import json
import time

import pytest

from pxh import voice_loop


class _ClaudeSpawned(AssertionError):
    """Raised if anything under test tries to start a process at all."""


@pytest.fixture(autouse=True)
def _no_subprocess(monkeypatch):
    """Any process spawn from the voice turn is a failure, Claude or not.

    Deliberately broader than "no argv containing claude": the invariant is
    that a resident-brain failure *reduces* work. A fallback that shells out to
    something else expensive would satisfy a claude-only check and still be the
    bug — that is exactly how the Ollama M5 and Ollama Cloud tiers ended up
    under this path.
    """
    def _boom(*args, **kwargs):
        raise _ClaudeSpawned(f"voice turn spawned a process: {args!r}")

    monkeypatch.setattr(voice_loop.subprocess, "run", _boom)
    monkeypatch.setattr(voice_loop.subprocess, "Popen", _boom)


def _stub_tier(monkeypatch, results):
    """Patch ask_m5 on the real module, rather than swapping the module.

    `run_voice_turn` does `from pxh import m5`, which reads the attribute off
    the already-imported package — so replacing `sys.modules["pxh.m5"]` only
    works if nothing has imported it yet. Under the full suite something always
    has, and the stub is silently ignored while the test still passes for the
    wrong reason. Patching the attributes is order-independent.
    """
    import pxh.m5
    calls = []

    def _ask(kind, prompt, system, **kw):
        calls.append((kind, prompt, system, kw))
        return results[min(len(calls) - 1, len(results) - 1)]

    monkeypatch.setattr(pxh.m5, "ask_m5", _ask)
    return calls


def _ok(action=None):
    import pxh.m5
    return pxh.m5.M5Result(
        status="available",
        response=json.dumps(action or {"tool": "tool_voice", "params": {"text": "hi"}}))


def _failed(status, error="nope"):
    import pxh.m5
    return pxh.m5.M5Result(status=status, error=error)


# ── The happy path ─────────────────────────────────────────────────────────

def test_voice_turn_goes_to_the_cognition_tier(monkeypatch):
    action = {"tool": "tool_voice", "params": {"text": "All cool here, Obi."}}
    calls = _stub_tier(monkeypatch, [_ok(action)])

    rc, stdout, stderr = voice_loop.run_voice_turn("prompt text")

    assert rc == 0
    assert json.loads(stdout) == action
    assert stderr == ""
    assert [kind for kind, _p, _s, _k in calls] == [voice_loop.VOICE_TURN_KIND]


def test_voice_turn_kind_is_classified():
    """An unclassified kind cannot reach the privileged session by any path."""
    from pxh import brain
    assert brain.is_classified_kind(voice_loop.VOICE_TURN_KIND)
    assert brain.session_for_kind(voice_loop.VOICE_TURN_KIND) == brain.BRAIN_SESSION


def test_the_call_carries_the_act_dont_narrate_frame(monkeypatch):
    """The resident session used to supply this on top of the voice prompt.

    Measured, not guessed: with the frame absent, the tier returned a valid
    `tool_perform` for "what do you make of this room?" whose three speak steps
    promised to look at the room — "Let me actually look at it first" — and no
    tool that looks. The JSON contract held and the behaviour was a lie to the
    person standing there, which is why this is pinned separately from the
    output-shape assertions.
    """
    calls = _stub_tier(monkeypatch, [_ok()])
    voice_loop.run_voice_turn("p")

    system = calls[0][2]
    assert system == voice_loop.VOICE_TURN_SYSTEM
    assert "not by narrating" in system
    assert '"tool"' in system


def test_the_prompt_tells_the_model_not_to_act(monkeypatch):
    """The model returns an action; voice_loop dispatches it through policy.

    A model that speaks directly makes it happen twice, and does it without
    passing the audio gate. On the resident path this travelled as a
    `respond_with` field in the payload; the tier takes one prompt, so the same
    instruction is part of it — and it is asserted here so it cannot be lost in
    a future re-wording of the call.
    """
    calls = _stub_tier(monkeypatch, [_ok()])
    voice_loop.run_voice_turn("p")
    _kind, prompt, _system, _kw = calls[0]
    assert prompt.startswith("p")
    assert "Do not speak, move or remember" in prompt
    assert '"tool"' in prompt


def test_an_unavailable_tier_never_spawns_anything(monkeypatch):
    """The whole point. No `claude -p`, no ollama subprocess, nothing."""
    _stub_tier(monkeypatch, [_failed("timeout")])
    rc, _, stderr = voice_loop.run_voice_turn("p")
    assert rc == voice_loop.VOICE_BRAIN_UNAVAILABLE
    assert "timeout" in stderr


def test_a_transport_fault_retries_exactly_once(monkeypatch):
    """The one failure a cheap immediate retry actually fixes.

    Same rule as before — contention is worth one retry — stated over a
    classification instead of inferred from how long the attempt took.
    """
    calls = _stub_tier(monkeypatch, [_failed("offline"), _failed("offline")])
    rc, _, _ = voice_loop.run_voice_turn("p", attempts=2)
    assert rc == voice_loop.VOICE_BRAIN_UNAVAILABLE
    assert len(calls) == 2


def test_a_saturated_tier_is_not_asked_twice(monkeypatch):
    """`timeout` means the model told us it had nothing to give; `busy` means
    the lock was held for the whole bounded wait. Asking twice adds load while
    a child waits — and the fix is never to raise the deadline, which only
    makes him wait longer for the same answer."""
    for status in ("timeout", "busy"):
        calls = _stub_tier(monkeypatch, [_failed(status)])
        rc, _, _ = voice_loop.run_voice_turn("p", attempts=2)
        assert rc == voice_loop.VOICE_BRAIN_UNAVAILABLE
        assert len(calls) == 1, f"a saturated tier must not be asked again ({status})"


def test_a_raising_tier_does_not_escalate(monkeypatch):
    import pxh.m5

    def _raise(*a, **k):
        raise RuntimeError("socket gone")

    monkeypatch.setattr(pxh.m5, "ask_m5", _raise)
    rc, _, stderr = voice_loop.run_voice_turn("p")
    assert rc == voice_loop.VOICE_BRAIN_UNAVAILABLE
    assert "raised" in stderr


# ── What the tier is asked for ─────────────────────────────────────────────

def test_the_interactive_deadline_and_lock_wait_are_the_ones_passed(monkeypatch):
    """A background caller defers rather than queues; the interactive caller
    waits a bounded moment, because reflection holds the same lock for a few
    seconds every few minutes and refusing instantly trades an answer for an
    acknowledgement."""
    import pxh.m5
    seen = {}
    monkeypatch.setattr(pxh.m5, "ask_m5",
                        lambda *a, **k: seen.update(k) or _ok())

    voice_loop.run_voice_turn("prompt")

    assert seen["timeout_s"] == voice_loop.VOICE_TURN_DEADLINE_S
    assert seen["lock_wait_s"] == voice_loop.VOICE_TURN_LOCK_WAIT_S
    assert 0 < voice_loop.VOICE_TURN_LOCK_WAIT_S < voice_loop.VOICE_TURN_DEADLINE_S


def test_the_deadline_still_matches_the_brain_table():
    """Two homes for one number, pinned together until Phase 3 deletes one.

    `brain._DEADLINE_S` was the single source of truth while the brain answered
    this kind. It answers no longer, but the entry is still there, so the two
    must not drift in the meantime — a silent 45s → 90s change is a longer wait
    for a child, and a silent 45s → 10s change is a turn that gives up while
    the model is still working.
    """
    from pxh import brain
    assert voice_loop.VOICE_TURN_DEADLINE_S == brain._DEADLINE_S["voice_turn"]


# ── The deterministic acknowledgement ──────────────────────────────────────

def test_acknowledgement_is_a_local_constant():
    """Availability acknowledgement, not cognition. No model produces it."""
    assert isinstance(voice_loop.VOICE_UNAVAILABLE_ACK, str)
    assert voice_loop.VOICE_UNAVAILABLE_ACK.strip()


def test_acknowledgement_passes_through_the_policy_sink(monkeypatch):
    """Quiet mode binds this line exactly as it binds anything SPARK says."""
    seen = {}

    def _validate(action):
        seen["action"] = action
        return ("tool_voice", {})

    monkeypatch.setattr(voice_loop, "validate_action", _validate)
    monkeypatch.setattr(voice_loop, "execute_tool", lambda *a, **k: (0, "", ""))

    assert voice_loop.acknowledge_unavailable(dry_run=True) is True
    assert seen["action"]["params"]["text"] == voice_loop.VOICE_UNAVAILABLE_ACK


def test_acknowledgement_stays_silent_when_policy_blocks(monkeypatch):
    """If the gate says no audio, the right outcome is silence — not a bypass."""
    def _blocked(action):
        raise voice_loop.VoiceLoopError("blocked: quiet_mode")

    monkeypatch.setattr(voice_loop, "validate_action", _blocked)
    monkeypatch.setattr(voice_loop, "execute_tool",
                        lambda *a, **k: pytest.fail("spoke through a closed gate"))

    assert voice_loop.acknowledge_unavailable(dry_run=True) is False


# ── The fossil is gone ─────────────────────────────────────────────────────

def test_bridge_is_deleted_not_deprecated():
    """Leaving fossils executable is how they turn back into architecture."""
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    assert not (repo / "bin" / "claude-voice-bridge").exists()


# ── The same rule, for the scheduled line ──────────────────────────────────

def test_cron_say_answers_from_the_cognition_tier_too():
    """`bin/px-cron-say` is a bash+python script with no importable surface, so
    this is a source invariant rather than a behavioural one — the shape the
    repo already uses for `bin/claude-voice-bridge` being deleted and for the
    describe-scene timeout pin.

    It speaks, so it is the same voice question as `voice_turn`, and it fails
    the same way if it drifts back: a resident session that is logged out or
    mid-recycle takes the slot with it.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "bin" / "px-cron-say").read_text(encoding="utf-8")

    assert "ask_m5" in src, "cron-say no longer asks the cognition tier"
    assert "ask_brain" not in src, "cron-say went back to the resident mailbox"
    # Deliberately not asserting on the *phrase* "claude -p": this file's
    # docstring has to be able to say what it replaced, and prose is not a call
    # path. The two assertions above are about calls.
    assert "call_claude" not in src
