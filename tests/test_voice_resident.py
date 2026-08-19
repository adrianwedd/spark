"""The "Hey Spark" turn runs on the resident brain, and never spawns Claude.

These assertions are process-level on purpose. Mocking the old fallback ladder
and checking it was not entered would pass just as happily against a rewritten
ladder; what must hold is the physical fact that no second Claude process
appears on the Pi. So the tests poison `subprocess` inside the module under
test and assert nothing reaches it.

Context: 2026-08-19. A 5s tmux delivery timeout was reported as "brain
unavailable", reflection fell through to `claude -p`, that process competed
with the voice turn's own `claude -p`, and a child waited 151 seconds. The
brain was healthy and idle the whole time.
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


def _stub_brain(monkeypatch, replies, delay=0.0, deadline=45):
    """Patch ask_brain on the real module, rather than swapping the module.

    `run_voice_turn` does `from pxh import brain`, which reads the attribute off
    the already-imported package — so replacing `sys.modules["pxh.brain"]` only
    works if nothing has imported it yet. Under the full suite something always
    has, and the stub is silently ignored while the test still passes for the
    wrong reason. Patching the attributes is order-independent.
    """
    import pxh.brain
    calls = []

    def _ask(kind, payload, **kw):
        calls.append((kind, payload))
        if delay:
            time.sleep(delay)
        return replies[min(len(calls) - 1, len(replies) - 1)]

    monkeypatch.setattr(pxh.brain, "ask_brain", _ask)
    monkeypatch.setattr(pxh.brain, "deadline_for_kind", lambda kind: deadline)
    return calls


# ── The happy path ─────────────────────────────────────────────────────────

def test_voice_turn_goes_to_the_resident_brain(monkeypatch):
    action = {"tool": "tool_voice", "params": {"text": "All cool here, Obi."}}
    calls = _stub_brain(monkeypatch, [{"reply": action}])

    rc, stdout, stderr = voice_loop.run_voice_turn("prompt text")

    assert rc == 0
    assert json.loads(stdout) == action
    assert [k for k, _ in calls] == [voice_loop.VOICE_TURN_KIND]


def test_voice_turn_kind_is_classified():
    """An unclassified kind cannot reach the privileged session by any path."""
    from pxh import brain
    assert brain.is_classified_kind(voice_loop.VOICE_TURN_KIND)
    assert brain.session_for_kind(voice_loop.VOICE_TURN_KIND) == brain.BRAIN_SESSION


def test_payload_tells_the_session_not_to_act(monkeypatch):
    """The session returns an action; voice_loop dispatches it through policy.

    A session that speaks directly makes it happen twice, and does it without
    passing the audio gate.
    """
    calls = _stub_brain(monkeypatch, [{"reply": {"tool": "tool_voice", "params": {}}}])
    voice_loop.run_voice_turn("p")
    _, payload = calls[0]
    assert "Do not speak, move or remember" in payload["respond_with"]


# ── Failure must reduce load, never escalate ───────────────────────────────

def test_brain_unavailable_never_spawns_anything(monkeypatch):
    """The whole point. No `claude -p`, no ollama, no subprocess at all."""
    _stub_brain(monkeypatch, [None])
    rc, _, stderr = voice_loop.run_voice_turn("p")
    assert rc == voice_loop.VOICE_BRAIN_UNAVAILABLE
    assert "unavailable" in stderr


def test_fast_delivery_failure_retries_exactly_once(monkeypatch):
    """Failing well inside the deadline means delivery, not thinking.

    Contention on the tmux send-keys path is the case a cheap immediate retry
    actually fixes, and it is what a loaded Pi produces.
    """
    calls = _stub_brain(monkeypatch, [None, None])
    rc, _, _ = voice_loop.run_voice_turn("p", attempts=2)
    assert rc == voice_loop.VOICE_BRAIN_UNAVAILABLE
    assert len(calls) == 2


def test_slow_failure_does_not_retry(monkeypatch):
    """A brain that consumed its deadline is saturated. Asking twice adds load.

    Bounded by classified failure, not by sleeping — and never by raising the
    deadline, which would only make the child wait longer for the same answer.
    """
    calls = _stub_brain(monkeypatch, [None], delay=0.06, deadline=0.08)

    rc, _, _ = voice_loop.run_voice_turn("p", attempts=2)
    assert rc == voice_loop.VOICE_BRAIN_UNAVAILABLE
    assert len(calls) == 1, "a saturated brain must not be asked again"


def test_a_raising_brain_does_not_escalate(monkeypatch):
    import pxh.brain

    def _raise(*a, **k):
        raise RuntimeError("tmux socket gone")

    monkeypatch.setattr(pxh.brain, "ask_brain", _raise)
    monkeypatch.setattr(pxh.brain, "deadline_for_kind", lambda kind: 45)
    rc, _, stderr = voice_loop.run_voice_turn("p")
    assert rc == voice_loop.VOICE_BRAIN_UNAVAILABLE
    assert "raised" in stderr


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
