"""Every kind the dispatcher knows has exactly one backend, and only one.

The audit of 2026-08-19 found the resident brain had been *built* but not made
*authoritative*: `public_chat` and `obi_chat` were classified with deadlines and
session routing, and `api.py` ignored all of it and shelled out anyway. These
tests pin classification to execution, so a kind cannot be classified on paper
and bypassed in practice again.

#317 Phase 3 retired the other destination, so the invariant is simpler and
stronger than it was: there is one backend, and every kind is either on it or
deliberately has none. A kind with no backend fails loudly — it does not fall
through to a CLI, and it does not quietly stop working.
"""
from __future__ import annotations

import pytest

from pxh import model_session


# ── Fail closed ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind", sorted(model_session.KNOWN_KINDS))
def test_no_kind_reaches_a_cli(kind, monkeypatch):
    """Either a kind runs on the cognition tier, or it has no backend at all.

    Neither cold-starts a process. That was the point of the fail-closed
    default, and it is now stated over the whole table instead of over a
    hand-listed subset, so a new kind cannot land in a gap.
    """
    def _boom(*a, **k):
        raise AssertionError("cold-started a CLI")

    monkeypatch.setattr(model_session.subprocess, "run", _boom)
    monkeypatch.setattr(model_session, "BUDGET_DISABLED", True)
    monkeypatch.setattr(model_session, "_log_session", lambda *a, **kw: None)

    if kind in model_session._COGNITION_KINDS:
        from pxh import m5
        monkeypatch.setattr(m5, "ask_m5",
                            lambda *a, **k: m5.M5Result(status="available", response="ok"))
        assert model_session.run_model_session(kind, "hi").returncode == 0
    else:
        with pytest.raises(model_session.ColdStartForbidden):
            model_session.run_model_session(kind, "hi")


def test_evolve_is_disabled_not_cold_started(monkeypatch):
    """evolve needs a git worktree, which a tool-free API call cannot provide.
    Disabled is the honest state; a "legacy cold Claude" bucket is exactly what
    this change abolished."""
    def _boom(*a, **k):
        raise AssertionError("cold-started a CLI for evolve")

    monkeypatch.setattr(model_session.subprocess, "run", _boom)

    with pytest.raises(model_session.ColdStartForbidden):
        model_session.run_model_session("evolve", "hi", skip_budget_check=True)


def test_every_known_kind_has_a_home_or_a_stated_reason_for_not():
    """The old failure mode was a kind missing from the dial — disabled by
    omission, which reads as a bug rather than a decision.

    With one backend the invariant is: every kind in the quota/priority tables
    is either served by the tier or named here as deliberately backendless.
    """
    cognition = model_session._COGNITION_KINDS
    assert cognition == {"consolidate", "research", "compose", "blog", "self_debug"}

    # Deliberately backendless, and named rather than omitted:
    #   evolve       — needs a git worktree the tier cannot reach
    #   conversation — legacy kind kept only in the quota/priority tables; no
    #                  caller has used it since before the resident session
    backendless = {"evolve", "conversation"}
    for kind in model_session.KNOWN_KINDS:
        if kind in backendless:
            continue
        assert kind in cognition, (
            f"{kind} has no backend — add it to _COGNITION_KINDS deliberately")


def test_asking_for_tools_on_the_tier_is_refused_not_ignored(monkeypatch):
    """The tempting failure mode is to answer without the tools and look fine."""
    from pxh import m5
    called = []
    monkeypatch.setattr(m5, "ask_m5", lambda *a, **k: called.append(1) or
                        m5.M5Result(status="available", response="ok"))
    monkeypatch.setattr(model_session, "BUDGET_DISABLED", True)

    with pytest.raises(model_session.CognitionTierToolsForbidden):
        model_session.run_model_session("self_debug", "hi", allowed_tools="Read")

    assert called == []
