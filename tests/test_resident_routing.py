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
    ("reflection", brain.BRAIN_SESSION),
    ("public_chat", brain.IO_SESSION),
    ("obi_chat", brain.IO_SESSION),
    ("post_qa", brain.IO_SESSION),
])
def test_kind_is_classified_and_routed(kind, session):
    assert brain.is_classified_kind(kind)
    assert brain.session_for_kind(kind) == session


def test_every_classified_kind_has_a_deadline():
    """A kind with no deadline silently inherits 300s — five minutes of a
    daemon loop blocked on optional work."""
    for kind in brain._BRAIN_KINDS | brain._IO_KINDS:
        assert brain.deadline_for_kind(kind) > 0


# ── Fail closed ────────────────────────────────────────────────────────────

def test_unrouted_kind_does_not_cold_start(monkeypatch):
    """The trust direction. Absent from the routing set must mean "no backend",
    never "spawn a fresh Claude" — the same fix already made for _BRAIN_KINDS
    vs _IO_KINDS, applied to the one place that still defaulted open."""
    monkeypatch.setenv("PX_BRAIN_KINDS", "research")

    def _boom(*a, **k):
        raise AssertionError("cold-started Claude for an unrouted kind")

    monkeypatch.setattr(claude_session.subprocess, "run", _boom)

    with pytest.raises(claude_session.ColdStartForbidden):
        claude_session.run_claude_session("compose", "hi", skip_budget_check=True)


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


def test_default_routing_covers_every_kind_that_can_run_resident():
    """If a kind can be served resident, it must be — otherwise it is disabled
    by omission, which reads as a bug rather than a decision."""
    routed = claude_session.brain_kinds()
    for kind in ("research", "compose", "post_qa", "reflection",
                 "blog", "consolidate", "self_debug"):
        assert kind in routed, f"{kind} would fail closed by accident"
