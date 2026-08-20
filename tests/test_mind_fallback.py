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
    """A canary: nothing in the Claude tier may resolve a binary any more.

    This originally existed because `call_claude_haiku` resolved the CLI
    *before* the mocked `subprocess.run` got a look in, so on this robot the
    tier ran and on any host without `claude` installed it short-circuited —
    four tests passing for an environmental reason rather than the one they
    stated. That helper is gone; the tier is the resident session now, and
    nothing here looks up a binary at all.

    The variable is kept, still pointing at a path that does not exist,
    because that makes it a regression detector: if a future change
    reintroduces binary resolution under this tier, it resolves to nothing and
    fails loudly instead of quietly working on a developer machine where
    `claude` happens to be installed.
    """
    monkeypatch.setenv("PX_CLAUDE_BIN", "/nonexistent/claude-under-test")
    monkeypatch.setenv("PX_M5_SPARK_MODEL", "spark:fixed")


@pytest.fixture(autouse=True)
def _clean_mind_state(tmp_path):
    old_log = getattr(pxh.mind, "LOG_FILE", None)
    pxh.mind.LOG_FILE = tmp_path / "px-mind.log"
    _reset_state()
    from pxh import m5
    m5.reset_for_tests()
    yield
    _reset_state()
    m5.reset_for_tests()
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

def test_reflection_uses_pinned_m5_without_claude():
    with patch("subprocess.run", return_value=_fake_claude(1, stderr="auth error")), \
         patch("urllib.request.urlopen", return_value=_fake_ollama_cm("quantum foam")):
        result = call_llm("prompt", "system", persona="spark")
    assert "error" not in result
    assert "quantum foam" in result["response"]


# ── M5 returns HTTP 200 but an empty completion (thinking model burned the
# whole token budget on reasoning) → must be treated as failure, not success ──

def test_empty_m5_response_defers_without_claude():
    def urlopen_side(req, timeout=30):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.endswith("/api/generate"):
            return _fake_ollama_empty_cm()
        raise urllib.error.URLError("skip model-resolution probe")

    import pxh.brain
    with patch("subprocess.run", side_effect=AssertionError("spawned a process")), \
         patch.object(pxh.brain, "ask_brain",
                      return_value={"reply": {"thought": "recovered via claude"}}), \
         patch("urllib.request.urlopen", side_effect=urlopen_side):
        result = call_llm("prompt", "system", persona="spark")

    assert result.get(pxh.mind.BRAIN_DEFER) is True


# ── Tier-3 fallback: Claude + M1 fail → local Ollama succeeds ──────

def test_non_spark_reflection_defers_when_m5_fails():
    """The Ollama chain still walks, for the personas that actually use it.

    This used to run as `persona="spark"`, which no longer reaches here: for
    SPARK the brain tier sits between M5 and the cloud, and a brain failure is
    terminal. Tiers 3 and 4 are not dead code though — a non-SPARK persona
    skips the brain tier entirely, so an M5 failure legitimately walks to
    cloud and then to localhost. That distinction is the point, so the test is
    re-pointed rather than deleted; the SPARK half is pinned by
    test_spark_never_reaches_local_ollama_after_a_brain_failure below.

    Distinguish by URL: M5 and cloud requests fail; localhost succeeds.
    call_count-based mocking is fragile because _resolve_ollama_model makes
    extra urlopen calls (api/ps + api/tags) before the actual generate request.
    """
    import pxh.brain

    def urlopen_side(req, timeout=30):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "M5.local" in url or "localhost" not in url:
            raise urllib.error.URLError("M5 unreachable")
        return _fake_ollama_cm("running on fumes")

    def _never(*a, **k):
        raise AssertionError("a non-SPARK persona reached the brain tier")

    # Local fallback is opt-in via PX_MIND_LOCAL_OLLAMA=1
    old_val = os.environ.get("PX_MIND_LOCAL_OLLAMA")
    os.environ["PX_MIND_LOCAL_OLLAMA"] = "1"
    try:
        with patch("subprocess.run", side_effect=AssertionError("spawned a process")), \
             patch.object(pxh.brain, "ask_brain", side_effect=_never), \
             patch("urllib.request.urlopen", side_effect=urlopen_side):
            result = call_llm("prompt", "system", persona="vixen")

        assert result.get(pxh.mind.BRAIN_DEFER) is True
    finally:
        if old_val is None:
            os.environ.pop("PX_MIND_LOCAL_OLLAMA", None)
        else:
            os.environ["PX_MIND_LOCAL_OLLAMA"] = old_val


def test_spark_never_reaches_local_ollama_after_a_brain_failure():
    """Loading a model on the Pi is the largest escalation available.

    Tier 4 is off by default precisely because a Pi 4 cannot hold a model
    alongside px-wake-listen and SenseVoice without filling swap. Reaching it
    *because the brain was slow* would be the 2026-08-19 cascade with a worse
    ending. Enabled here on purpose: the assertion is that it stays unreached
    even when it is available.
    """
    import pxh.brain

    def urlopen_side(req, timeout=30):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "localhost" in url:
            raise AssertionError("SPARK reached local ollama after a brain failure")
        raise urllib.error.URLError("M5 unreachable")

    old_val = os.environ.get("PX_MIND_LOCAL_OLLAMA")
    os.environ["PX_MIND_LOCAL_OLLAMA"] = "1"
    try:
        with patch("subprocess.run", side_effect=AssertionError("spawned a process")), \
             patch.object(pxh.brain, "ask_brain", return_value=None), \
             patch("urllib.request.urlopen", side_effect=urlopen_side):
            result = call_llm("prompt", "system", persona="spark")

        assert result.get(pxh.mind.BRAIN_DEFER) is True, result
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


def test_m5_failure_never_uses_claude():
    """M5 down, SPARK falls through to the resident brain — still labelled
    `claude`, because the tier is still Claude; what changed is that it is a
    session already running rather than a process started for this thought."""
    import pxh.brain
    with patch("urllib.request.urlopen",
               side_effect=urllib.error.URLError("M5 unreachable")), \
         patch("subprocess.run", side_effect=AssertionError("spawned a process")), \
         patch.object(pxh.brain, "ask_brain",
                      return_value={"reply": {"thought": "hi", "mood": "curious"}}):
        result = call_llm("prompt", "system", persona="spark")
    assert result.get(pxh.mind.BRAIN_DEFER) is True


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


# ── Tier 2 is the resident brain, with NOTHING behind it ──────────────────
#
# There used to be a `claude -p` subprocess here, and these tests used to pin
# it. The old comment said "a brain that is down must be invisible" — that was
# the bug, stated as a requirement. Making a down brain invisible meant
# spawning a fresh Claude on a Pi that was already saturated, which is how one
# slow tmux keystroke became two competing Claude processes, a 120s timeout, an
# M5 timeout and a 403 from Ollama Cloud (2026-08-19).
#
# A down brain is now *visible and cheap*: reflection defers and backs off.
# Reflection is the most skippable work SPARK does; failing to have a thought
# costs nothing, and a loaded Pi should be doing less, not searching wider.

def _brain_reply(obj):
    """Shape of what ask_brain() hands back: the session's JSON under 'reply'."""
    return {"id": "x", "reply": obj}


def test_reflection_m5_failure_does_not_use_resident_brain():
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

    assert result.get(pxh.mind.BRAIN_DEFER) is True
    assert ask.call_count == 0
    assert not any("-p" in (cmd or []) for cmd in ran), ran


def test_reflection_defers_when_the_brain_is_unavailable():
    """A resident-brain failure is terminal. No process, no wider search."""
    import pxh.brain

    spawned = []

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("M5 down")), \
         patch.object(pxh.brain, "ask_brain", return_value=None), \
         patch("subprocess.run", side_effect=lambda *a, **k: spawned.append(a)):
        result = call_llm("prompt", "system", persona="spark")

    assert "error" in result
    assert result.get(pxh.mind.BRAIN_DEFER) is True, result
    assert spawned == [], f"reflection spawned a process: {spawned}"


def test_reflection_failure_does_not_reach_ollama_cloud():
    """The 403 in the 2026-08-19 cascade was reached *from* a brain failure.

    Tier 3 exists for an M5 failure, not as a rescue for the resident session.
    Opening an internet request because a keystroke was slow is the escalation
    this whole change removes.
    """
    import pxh.brain

    seen_urls = []

    def _record(req, *a, **kw):
        seen_urls.append(req.full_url if hasattr(req, "full_url") else str(req))
        raise urllib.error.URLError("M5 down")

    with patch("urllib.request.urlopen", side_effect=_record), \
         patch.object(pxh.brain, "ask_brain", return_value=None), \
         patch("subprocess.run", side_effect=AssertionError("spawned a process")):
        result = call_llm("prompt", "system", persona="spark")

    # Assert on *where* the requests went, not how many there were: M5 may do a
    # model-resolution probe before generating, and its own offline backoff may
    # skip the network entirely, so any count is a hostage to unrelated state.
    # What must hold is that nothing reached the cloud after the brain failed.
    assert not [u for u in seen_urls if "ollama.com" in u], seen_urls
    assert result.get(pxh.mind.BRAIN_DEFER) is True, "the brain tier did not stop the chain"


def test_narrowing_the_dial_cannot_restore_a_cold_path():
    """There is no rollback destination any more, so the dial cannot open one.

    PX_BRAIN_KINDS used to be able to route reflection back to `claude -p`.
    Whatever it is set to now, no process may be spawned.
    """
    import pxh.brain

    with patch.dict(os.environ, {"PX_BRAIN_KINDS": "research,compose"}), \
         patch("urllib.request.urlopen", side_effect=urllib.error.URLError("M5 down")), \
         patch.object(pxh.brain, "ask_brain", return_value=None), \
         patch("subprocess.run", side_effect=AssertionError("spawned a process")):
        result = call_llm("prompt", "system", persona="spark")

    assert "error" in result
