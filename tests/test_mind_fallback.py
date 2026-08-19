"""Tests for px-mind three-tier LLM fallback: Claude -> M1 Ollama -> local Ollama."""
from __future__ import annotations

import json
import os
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

import pxh.mind
from pxh.mind import call_llm, _reset_state


@pytest.fixture(autouse=True)
def _pin_claude_binary(monkeypatch):
    """Make the Claude tier reachable without a `claude` on PATH.

    Every test here patches `subprocess.run` to fake the CLI, but
    `call_claude_haiku` resolves the binary *before* it gets there
    (`mind.py:2445`: PX_CLAUDE_BIN, then shutil.which, then a ~/.nvm glob).
    On this robot `claude` is installed, so the tier ran and the mock fired;
    on any host without it the tier short-circuits to "claude binary not
    found" and falls through to the next one — so four tests asserting the
    Claude fallback passed here for an environmental reason rather than the
    reason they state, and failed everywhere else.

    The path is deliberately one that does not exist. subprocess.run is
    mocked, so it is never executed; if a future test forgets that mock it
    gets a loud FileNotFoundError instead of silently exercising a different
    tier, which is the failure mode this fixture exists to close.
    """
    monkeypatch.setenv("PX_CLAUDE_BIN", "/nonexistent/claude-under-test")


@pytest.fixture(autouse=True)
def _clean_mind_state(tmp_path):
    old_log = getattr(pxh.mind, "LOG_FILE", None)
    pxh.mind.LOG_FILE = tmp_path / "px-mind.log"
    _reset_state()
    yield
    _reset_state()
    if old_log is not None:
        pxh.mind.LOG_FILE = old_log


def _fake_ollama_cm(text: str):
    """Mock urlopen context manager returning a valid Ollama response."""
    body = json.dumps({"response": text}).encode()
    inner = MagicMock()
    inner.read = lambda: body
    cm = MagicMock()
    cm.__enter__ = lambda s: inner
    cm.__exit__  = MagicMock(return_value=False)
    return cm


def _fake_claude(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode, m.stdout, m.stderr = returncode, stdout, stderr
    return m


def _fake_ollama_empty_cm(done_reason: str = "length"):
    """Mock a 200 OK Ollama response whose 'response' field is empty — e.g. a
    thinking-capable model burning its whole token budget on reasoning."""
    body = json.dumps({"response": "", "done_reason": done_reason, "thinking": "..."}).encode()
    inner = MagicMock()
    inner.read = lambda: body
    cm = MagicMock()
    cm.__enter__ = lambda s: inner
    cm.__exit__  = MagicMock(return_value=False)
    return cm


# ── Tier-2 fallback: Claude fails → M1 Ollama succeeds ─────────────

def test_falls_back_to_m1_ollama_when_claude_fails():
    with patch("subprocess.run", return_value=_fake_claude(1, stderr="auth error")), \
         patch("urllib.request.urlopen", return_value=_fake_ollama_cm("quantum foam")):
        result = call_llm("prompt", "system", persona="spark")
    assert "error" not in result
    assert "quantum foam" in result["response"]


# ── M5 returns HTTP 200 but an empty completion (thinking model burned the
# whole token budget on reasoning) → must be treated as failure, not success ──

def test_falls_back_to_claude_when_ollama_returns_empty_response():
    def urlopen_side(req, timeout=30):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.endswith("/api/generate"):
            return _fake_ollama_empty_cm()
        raise urllib.error.URLError("skip model-resolution probe")

    with patch("subprocess.run",
               return_value=_fake_claude(0, stdout='{"thought": "recovered via claude"}')), \
         patch("urllib.request.urlopen", side_effect=urlopen_side):
        result = call_llm("prompt", "system", persona="spark")

    assert "error" not in result
    assert "recovered via claude" in result["response"]


# ── Tier-3 fallback: Claude + M1 fail → local Ollama succeeds ──────

def test_falls_back_to_local_ollama_when_m1_fails():
    # Distinguish by URL: M5 and cloud requests fail; localhost succeeds.
    # call_count-based mocking is fragile because _resolve_ollama_model makes
    # extra urlopen calls (api/ps + api/tags) before the actual generate request.
    def urlopen_side(req, timeout=30):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "M5.local" in url or "localhost" not in url:
            raise urllib.error.URLError("M5 unreachable")
        return _fake_ollama_cm("running on fumes")

    # Local fallback is opt-in via PX_MIND_LOCAL_OLLAMA=1
    old_val = os.environ.get("PX_MIND_LOCAL_OLLAMA")
    os.environ["PX_MIND_LOCAL_OLLAMA"] = "1"
    try:
        with patch("subprocess.run", return_value=_fake_claude(1, stderr="offline")), \
             patch("urllib.request.urlopen", side_effect=urlopen_side):
            result = call_llm("prompt", "system", persona="spark")

        assert "error" not in result
        assert "fumes" in result["response"]
    finally:
        if old_val is None:
            os.environ.pop("PX_MIND_LOCAL_OLLAMA", None)
        else:
            os.environ["PX_MIND_LOCAL_OLLAMA"] = old_val


def test_skips_local_ollama_when_not_opted_in():
    """Without PX_MIND_LOCAL_OLLAMA=1, M1 failure → error (no local fallback)."""
    old_val = os.environ.pop("PX_MIND_LOCAL_OLLAMA", None)
    try:
        with patch("subprocess.run", return_value=_fake_claude(1, stderr="offline")), \
             patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("M1 unreachable")):
            result = call_llm("prompt", "system", persona="spark")
        assert "error" in result
    finally:
        if old_val is not None:
            os.environ["PX_MIND_LOCAL_OLLAMA"] = old_val


# ── Full failure: all three tiers fail → error dict, no exception ───

def test_returns_error_when_all_tiers_fail():
    with patch("subprocess.run", return_value=_fake_claude(1, stderr="offline")), \
         patch("urllib.request.urlopen",
               side_effect=urllib.error.URLError("all down")):
        result = call_llm("prompt", "system", persona="spark")
    assert "error" in result


# ── Which tier actually served ─────────────────────────────────────
# The `backend=` reflection log line reports the *configured* primary, so the
# tier that answered was previously only recoverable by grepping for "falling
# back". call_llm() now labels the result, and that label is what makes paid-
# tier drift measurable (see token_log.by_backend).

def test_result_is_labelled_with_the_tier_that_served():
    with patch("urllib.request.urlopen", return_value=_fake_ollama_cm("hello")):
        result = call_llm("prompt", "system", persona="spark")
    assert result["backend"] == "ollama-m5"


def test_claude_fallback_is_labelled_claude():
    """M5 down, SPARK falls through to Claude — the expensive tier must be
    identifiable, since this is the unbudgeted path."""
    with patch("urllib.request.urlopen",
               side_effect=urllib.error.URLError("M5 unreachable")), \
         patch("subprocess.run",
               return_value=_fake_claude(0, stdout='{"thought": "hi", "mood": "curious"}')):
        result = call_llm("prompt", "system", persona="spark")
    assert "error" not in result
    assert result["backend"] == "claude"


def test_token_usage_is_split_by_backend(tmp_path, monkeypatch):
    """Top-level totals mix free Ollama with paid Claude and cannot answer
    'what am I spending' — only the per-backend split can."""
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    from pxh import token_log

    token_log.log_usage("prompt text", "response text", "ollama-m5")
    token_log.log_usage("prompt text", "response text", "claude")
    token_log.log_usage("prompt text", "response text", "claude")

    data = json.loads((tmp_path / "token_usage.json").read_text())
    assert data["call_count"] == 3
    assert data["by_backend"]["ollama-m5"]["call_count"] == 1
    assert data["by_backend"]["claude"]["call_count"] == 2
    assert data["by_backend"]["claude"]["input_tokens"] > 0


def test_token_usage_backend_defaults_to_unknown(tmp_path, monkeypatch):
    """Two-arg callers predate the split and must keep working."""
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    from pxh import token_log

    token_log.log_usage("prompt", "response")
    data = json.loads((tmp_path / "token_usage.json").read_text())
    assert data["by_backend"]["unknown"]["call_count"] == 1


# ── Tier 2 is the resident brain, with `claude -p` behind it ───────────────
#
# Reflection's Claude tier was the last hot path still shelling out to
# `claude -p` on every call: a fresh process, a cold context, no metering, and
# ~10s of startup before it says a word. The resident session answers from
# warm context and `ask_brain` meters it. These tests pin the ordering and,
# more importantly, the fallback — a brain that is down must be invisible.

def _brain_reply(obj):
    """Shape of what ask_brain() hands back: the session's JSON under 'reply'."""
    return {"id": "x", "reply": obj}


def test_reflection_claude_tier_prefers_the_resident_brain():
    """With the brain up, the Claude tier must not spawn a subprocess at all."""
    import pxh.brain

    ran = []

    def _record(*args, **kwargs):
        ran.append(args[0] if args else kwargs.get("args"))
        return _fake_claude(1, stderr="should never be reached")

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("M5 down")), \
         patch.object(pxh.brain, "ask_brain",
                      return_value=_brain_reply({"thought": "from the brain",
                                                 "mood": "curious"})) as ask, \
         patch("subprocess.run", side_effect=_record):
        result = call_llm("prompt", "system", persona="spark")

    assert "error" not in result, result
    assert "from the brain" in result["response"]
    assert result["backend"] == "claude"
    assert ask.call_count == 1
    assert ask.call_args.args[0] == "reflection"
    assert not any("-p" in (cmd or []) for cmd in ran), ran


def test_reflection_falls_back_to_claude_p_when_the_brain_is_unavailable():
    """ask_brain() returns None for every failure; that must mean 'old path'."""
    import pxh.brain

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("M5 down")), \
         patch.object(pxh.brain, "ask_brain", return_value=None), \
         patch("subprocess.run",
               return_value=_fake_claude(0, stdout='{"thought": "from claude -p"}')):
        result = call_llm("prompt", "system", persona="spark")

    assert "error" not in result, result
    assert "from claude -p" in result["response"]
    assert result["backend"] == "claude"


def test_reflection_brain_routing_is_read_at_call_time():
    """PX_BRAIN_KINDS is a live dial — dropping reflection rolls it back."""
    import pxh.brain

    with patch.dict(os.environ, {"PX_BRAIN_KINDS": "research,compose"}), \
         patch("urllib.request.urlopen", side_effect=urllib.error.URLError("M5 down")), \
         patch.object(pxh.brain, "ask_brain") as ask, \
         patch("subprocess.run",
               return_value=_fake_claude(0, stdout='{"thought": "old path"}')):
        result = call_llm("prompt", "system", persona="spark")

    assert ask.call_count == 0
    assert "old path" in result["response"]
