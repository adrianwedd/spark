"""Tool-free kinds are served by the cognition tier, not the resident session (#317).

The point of the migration is a failure mode that cannot happen: the answer is
the HTTP response body, so there is no session to be logged out of, no inbox
file addressed by request id, no reply command to shell-quote, and no
permission dialog in front of it. #314 is what the other path costs — a turn
that was worked and answered in 47s and then never reached its caller.

So these tests pin three things, in order of how much they matter:

1. A migrated kind cannot reach the resident session, at all, ever — not even
   when `PX_BRAIN_KINDS` still names it. A rollback dial that worked would be
   the silent Claude fallback this migration exists to remove.
2. A failure is reported, classified, and left failed. It does not escalate.
3. The session log says what actually served the call (`provider` + model),
   because `model` alone was ambiguous the moment two tiers existed.
"""
from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

from pxh import brain, claude_session as cs, m5

MIGRATED = ("consolidate", "research", "compose", "blog")


def _ok(response="the answer", **kw):
    return m5.M5Result(status="available", response=response, **kw)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "SESSION_LOG", tmp_path / "claude_sessions.jsonl")
    monkeypatch.setattr(cs, "STATE_DIR", tmp_path)
    monkeypatch.setattr(cs, "BUDGET_DISABLED", True)


def _entries():
    text = cs.SESSION_LOG.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def _no_brain(*_a, **_k):
    raise AssertionError("a cognition-tier kind reached the resident session")


@pytest.mark.parametrize("kind", MIGRATED)
def test_a_migrated_kind_is_served_by_the_cognition_tier(kind, monkeypatch):
    monkeypatch.setattr(brain, "ask_brain", _no_brain)
    monkeypatch.setattr(m5, "ask_m5", lambda *a, **k: _ok())

    result = cs.run_claude_session(kind, "a prompt", timeout=5)

    assert result.returncode == 0
    assert result.stdout == "the answer"
    assert result.provider == cs.COGNITION_PROVIDER


@pytest.mark.parametrize("kind", MIGRATED)
def test_the_brain_dial_cannot_route_a_migrated_kind_back(kind, monkeypatch):
    """`PX_BRAIN_KINDS` is still a live dial for the kinds that remain resident.

    It must not be one for these: the incident this migration answers was nine
    nights of a mailbox nobody could make reliable, and "rolled back into the
    mailbox by an environment variable" is not a shape a fix should have.
    """
    monkeypatch.setenv("PX_BRAIN_KINDS", ",".join(MIGRATED))
    monkeypatch.setattr(brain, "ask_brain", _no_brain)
    monkeypatch.setattr(m5, "ask_m5", lambda *a, **k: _ok())

    assert cs.run_claude_session(kind, "a prompt", timeout=5).returncode == 0


def test_the_prompt_and_timeout_reach_the_cognition_tier_intact(monkeypatch):
    seen = {}

    def _capture(kind, prompt, system, *, timeout_s=None, model=None):
        seen.update(kind=kind, prompt=prompt, system=system,
                    timeout_s=timeout_s, model=model)
        return _ok()

    monkeypatch.setattr(m5, "ask_m5", _capture)

    cs.run_claude_session("consolidate", "distil the day", timeout=600)

    assert seen == {"kind": "consolidate", "prompt": "distil the day",
                    "system": "", "timeout_s": 600, "model": None}


@pytest.mark.parametrize("status", ["timeout", "offline", "bad_response", "busy"])
def test_a_failure_is_classified_and_never_falls_back_to_claude(status, monkeypatch):
    """#317: "Ollama failure should be explicit/deferred under the existing
    caller semantics, not silently resurrect the old transport." """
    monkeypatch.setattr(brain, "ask_brain", _no_brain)
    monkeypatch.setattr(m5, "ask_m5",
                        lambda *a, **k: m5.M5Result(status=status, error="boom"))

    result = cs.run_claude_session("research", "a question", timeout=5)

    assert result.returncode != 0
    assert result.stdout == ""
    assert status in result.stderr
    assert result.provider == cs.COGNITION_PROVIDER
    entry = _entries()[-1]
    assert entry["outcome"] == f"cognition_{status}"
    assert entry["returncode"] == 1


def test_the_log_names_the_tier_that_answered(monkeypatch):
    monkeypatch.setattr(m5, "ask_m5", lambda *a, **k: _ok(
        model="deepseek-v4.1-flash:cloud", prompt_eval_count=1200, eval_count=310))

    cs.run_claude_session("blog", "write a post", timeout=60)

    entry = _entries()[-1]
    assert entry["provider"] == "ollama-cloud"
    assert entry["model"] == "deepseek-v4.1-flash:cloud"
    assert entry["tokens"] == {"prompt": 1200, "eval": 310}


def test_the_resident_path_records_its_provider_too(monkeypatch):
    """One field, two values — a reader must never have to infer the tier."""
    monkeypatch.setattr(brain, "ask_brain",
                        lambda kind, payload, timeout_s=None, model=None: {"reply": "ok"})

    cs.run_claude_session("self_debug", "diagnose", timeout=5)

    entry = _entries()[-1]
    assert entry["provider"] == cs.RESIDENT_PROVIDER
    assert "tokens" not in entry


def test_a_provider_neutral_model_override_is_honoured(monkeypatch):
    seen = {}
    monkeypatch.setattr(m5, "ask_m5",
                        lambda kind, prompt, system, *, timeout_s=None, model=None:
                        seen.update(model=model) or _ok())
    monkeypatch.setenv("PX_MODEL_RESEARCH", "qwen3:32b-cloud")

    cs.run_claude_session("research", "a question", timeout=5)

    assert seen["model"] == "qwen3:32b-cloud"


def test_the_claude_era_model_variable_is_not_handed_to_ollama(monkeypatch):
    """`PX_CLAUDE_MODEL_RESEARCH` holds a Claude model id. Ignoring it is the
    honest outcome; passing it to Ollama would be a second, quieter bug."""
    seen = {}
    monkeypatch.setattr(m5, "ask_m5",
                        lambda kind, prompt, system, *, timeout_s=None, model=None:
                        seen.update(model=model) or _ok())
    monkeypatch.setenv("PX_CLAUDE_MODEL_RESEARCH", "claude-opus-4-6")

    cs.run_claude_session("research", "a question", timeout=5)

    assert seen["model"] is None


def test_budget_and_quota_still_apply_to_migrated_kinds(monkeypatch):
    """The migration is not a way around the quota (#317 keeps policy as-is)."""
    called = []
    monkeypatch.setattr(m5, "ask_m5", lambda *a, **k: called.append(1) or _ok())
    monkeypatch.setattr(cs, "check_budget", lambda kind: "daily cap reached")

    with pytest.raises(cs.SessionBudgetExhausted):
        cs.run_claude_session("research", "a question")

    assert called == [], "the tier must not be called once the budget refuses"
