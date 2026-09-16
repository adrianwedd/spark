"""Every Claude-backed kind is routed, and an unrouted one fails closed.

The audit of 2026-08-19 found the resident brain had been *built* but not made
*authoritative*: `public_chat` and `obi_chat` were classified in brain.py with
deadlines and session routing, and api.py ignored all of it and shelled out
anyway. These tests pin the classification to the call sites, so a kind cannot
be classified on paper and bypassed in execution again.
"""
from __future__ import annotations

import pytest

from pxh import brain, claude_session


# ── Classification ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind,session", [
    ("voice_turn", brain.BRAIN_SESSION),
    ("cron_say", brain.BRAIN_SESSION),
    ("reflection", brain.M5_SESSION),
    ("public_chat", brain.M5_SESSION),
    ("obi_chat", brain.M5_SESSION),
    ("post_qa", brain.M5_SESSION),
])
def test_kind_is_classified_and_routed(kind, session):
    assert brain.is_classified_kind(kind)
    assert brain.session_for_kind(kind) == session


def test_every_classified_kind_has_a_deadline():
    """A kind with no deadline silently inherits 300s — five minutes of a
    daemon loop blocked on optional work."""
    for kind in brain._BRAIN_KINDS | brain._M5_KINDS:
        assert brain.deadline_for_kind(kind) > 0


# ── Fail closed ────────────────────────────────────────────────────────────

def test_unrouted_kind_does_not_cold_start(monkeypatch):
    """The trust direction. Absent from the routing set must mean "no backend",
    never "spawn a fresh Claude" — the same fix already made for
    brain.session_for_kind (it raises rather than defaulting to a session),
    applied to the one place that still defaulted open."""
    monkeypatch.setenv("PX_BRAIN_KINDS", "research")

    def _boom(*a, **k):
        raise AssertionError("cold-started Claude for an unrouted kind")

    monkeypatch.setattr(claude_session.subprocess, "run", _boom)

    with pytest.raises(claude_session.ColdStartForbidden):
        claude_session.run_claude_session("self_debug", "hi", skip_budget_check=True)


def test_evolve_is_disabled_not_cold_started(monkeypatch):
    """evolve needs a git worktree, which a resident session's fixed tool
    envelope cannot provide. Disabled is the honest state; a "legacy cold
    Claude" bucket is exactly what this change abolishes."""
    def _boom(*a, **k):
        raise AssertionError("cold-started Claude for evolve")

    monkeypatch.setattr(claude_session.subprocess, "run", _boom)
    assert "evolve" not in claude_session.brain_kinds()

    with pytest.raises(claude_session.ColdStartForbidden):
        claude_session.run_claude_session("evolve", "hi", skip_budget_check=True)


def test_every_known_kind_has_a_home_and_no_kind_has_two():
    """The dial's old failure mode was a kind missing from it — disabled by
    omission, which reads as a bug rather than a decision.

    There are two destinations now (#317), so the invariant is stronger than
    "every kind is listed": every kind the dispatcher knows about is served by
    exactly one of them, and the two sets never overlap. An overlap would mean
    a migrated kind could still be reached through the resident session, which
    is the silent fallback the migration exists to remove.
    """
    routed = claude_session.brain_kinds()
    cognition = claude_session._COGNITION_KINDS

    assert not (routed & cognition)
    assert routed == {"self_debug"}
    assert cognition == {"consolidate", "research", "compose", "blog"}

    # Deliberately backendless, and named rather than omitted:
    #   evolve       — needs a git worktree the fixed tool envelope cannot give
    #   conversation — legacy kind retained only in the quota/priority tables;
    #                  no caller has used it since before the resident brain
    backendless = {"evolve", "conversation"}
    for kind in claude_session._DEFAULT_MODELS:
        if kind in backendless:
            continue
        assert kind in routed or kind in cognition, (
            f"{kind} has no backend — add it to _COGNITION_KINDS or to "
            f"_DEFAULT_BRAIN_KINDS deliberately")
