"""Tool-free kinds are served by the cognition tier, and only by it (#317).

The point of the migration is a failure mode that cannot happen: the answer is
the HTTP response body, so there is no session to be logged out of, no inbox
file addressed by request id, no reply command to shell-quote, and no
permission dialog in front of it. #314 is what the other path costs — a turn
that was worked and answered in 47s and then never reached its caller.

So these tests pin three things, in order of how much they matter:

1. There is no resident session to reach. #317 Phase 3 deleted the module, the
   reply tool, the session launcher and its unit — so this is asserted by
   *absence* rather than by a dial whose off position is trusted, because a
   disabled route is one environment variable away from being a route again.
2. A failure is reported, classified, and left failed. It does not escalate.
3. The session log says what actually served the call (`provider` + model),
   because `model` alone was ambiguous the moment two tiers existed.
"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from pxh import claude_session as cs, m5

REPO = Path(__file__).resolve().parent.parent

MIGRATED = ("consolidate", "research", "compose", "blog", "self_debug")


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


def test_the_resident_transport_is_deleted_rather_than_disabled():
    """#317 Phase 3. A route that is switched off is a route.

    Deleting the module is the assertion: an `if` that is currently false can
    be flipped, and a supervisor that is stopped on the Pi can be started
    again. This is the only form of "cannot reach it" that survives someone
    re-enabling something at 3am.
    """
    with pytest.raises(ImportError):
        importlib.import_module("pxh.brain")
    with pytest.raises(ImportError):
        importlib.import_module("pxh.brain_daemon")
    with pytest.raises(ImportError):
        importlib.import_module("pxh.tmux_claude")

    for gone in ("bin/tool-brain-reply", "bin/px-claude-session", "bin/px-brain",
                 "systemd/px-brain.service"):
        assert not (REPO / gone).exists(), f"{gone} is still present"


def test_the_brain_dial_cannot_route_anything_anywhere(monkeypatch):
    """`PX_BRAIN_KINDS` used to be a live dial. Setting it must do nothing."""
    monkeypatch.setenv("PX_BRAIN_KINDS", ",".join(MIGRATED))
    monkeypatch.setattr(m5, "ask_m5", lambda *a, **k: _ok())
    for kind in MIGRATED:
        assert cs.run_claude_session(kind, "a prompt", timeout=5).returncode == 0
    assert not hasattr(cs, "brain_kinds")


@pytest.mark.parametrize("kind", MIGRATED)
def test_a_migrated_kind_is_served_by_the_cognition_tier(kind, monkeypatch):
    monkeypatch.setattr(m5, "ask_m5", lambda *a, **k: _ok())

    result = cs.run_claude_session(kind, "a prompt", timeout=5)

    assert result.returncode == 0
    assert result.stdout == "the answer"
    assert result.provider == cs.COGNITION_PROVIDER


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


def test_a_cognition_kind_asked_for_tools_is_refused(monkeypatch):
    """#317 Phase 2: the tier has no tool envelope, and the tempting failure
    mode is to *ignore* the request and answer without the tools.

    A caller that asked for `Read,Glob,Grep` and got a tool-less answer has no
    way to tell that from a working one. `self_debug` was the last caller to
    ask, and it now collects what it needs in Python instead.
    """
    called = []
    monkeypatch.setattr(m5, "ask_m5", lambda *a, **k: called.append(1) or _ok())

    with pytest.raises(cs.CognitionTierToolsForbidden) as excinfo:
        cs.run_claude_session("self_debug", "diagnose",
                              allowed_tools="Read,Glob,Grep",
                              skip_permissions=True, timeout=10)

    assert "no tools" in str(excinfo.value)
    assert "Read,Glob,Grep" in str(excinfo.value)
    assert called == [], "the tier was called for a request it cannot honour"
