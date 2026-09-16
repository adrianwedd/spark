"""Tests for px-mind's reflection tier: one tier, and a defer behind it.

The ladder this file was written against (Claude -> M1 Ollama -> Ollama Cloud ->
Pi-local Ollama) no longer exists. `call_llm()` reaches `pxh.m5.ask_m5()` and
defers on failure (#308 moved that tier from a LAN daemon to Ollama Cloud, which
changed where it points and nothing about the shape). The tests below that still
name a ladder are the ones asserting a rung is *unreachable* — that is the
property worth keeping, so they are re-stated rather than deleted.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
from unittest.mock import MagicMock, patch

import pytest

import pxh.mind
from pxh.mind import call_llm, _reset_state


@pytest.fixture(autouse=True)
def _pin_tier_model(monkeypatch):
    """Pin the model, and record why the environment must not decide these tests.

    This fixture used to also point `PX_CLAUDE_BIN` at a nonexistent path. That
    was a real guard once: `call_claude_haiku` resolved the CLI *before* the
    mocked `subprocess.run` got a look in, so four tests ran here and
    short-circuited on any host without `claude` installed — passing for an
    environmental reason rather than the one they stated.

    There is no binary to resolve any more (#317 Phase 3), so the pointer is
    gone. Nothing reads that variable on this path, and a fixture setting a
    variable nothing reads is a fixture that looks like a guard and is not one.
    What genuinely guards this property now is
    `tools/check_resident_claude.py`, which fails CI on the syntax and on the
    reappearance of the retired module.
    """
    monkeypatch.setenv("PX_M5_SPARK_MODEL", "spark:fixed")
    # The tier is hosted (#308): without a key every call fails closed before
    # reaching the network, which would make these tests pass for the wrong
    # reason.
    monkeypatch.setenv("PX_M5_SPARK_API_KEY", "test-key")


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

    with patch("subprocess.run", side_effect=AssertionError("spawned a process")), \
         patch("urllib.request.urlopen", side_effect=urlopen_side):
        result = call_llm("prompt", "system", persona="spark")

    assert result.get(pxh.mind.BRAIN_DEFER) is True


# ── Tier-3 fallback: Claude + M1 fail → local Ollama succeeds ──────

def test_non_spark_reflection_defers_when_the_cognition_tier_fails():
    """Persona does not change the shape of a failure any more.

    This test existed because a non-SPARK persona used to walk further down the
    ladder than SPARK did. With the ladder gone (#308), every persona defers at
    the same point — that uniformity is now the property, so the test asserts it
    for the persona that used to diverge.

    Distinguish by URL: the cognition tier fails; a Pi-local daemon would
    succeed, so a walk to it would show up as a non-defer result.
    """
    def urlopen_side(req, timeout=30):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "localhost" in url:
            raise AssertionError("a persona reached a Pi-local Ollama daemon")
        raise urllib.error.URLError("cognition tier unreachable")

    # Local fallback is opt-in via PX_MIND_LOCAL_OLLAMA=1
    old_val = os.environ.get("PX_MIND_LOCAL_OLLAMA")
    os.environ["PX_MIND_LOCAL_OLLAMA"] = "1"
    try:
        with patch("subprocess.run", side_effect=AssertionError("spawned a process")), \
             patch("urllib.request.urlopen", side_effect=urlopen_side):
            result = call_llm("prompt", "system", persona="vixen")

        assert result.get(pxh.mind.BRAIN_DEFER) is True
    finally:
        if old_val is None:
            os.environ.pop("PX_MIND_LOCAL_OLLAMA", None)
        else:
            os.environ["PX_MIND_LOCAL_OLLAMA"] = old_val


def test_spark_never_reaches_local_ollama_after_a_tier_failure():
    """Loading a model on the Pi is the largest escalation available.

    Tier 4 is off by default precisely because a Pi 4 cannot hold a model
    alongside px-wake-listen and SenseVoice without filling swap. Reaching it
    *because the tier was slow* would be the 2026-08-19 cascade with a worse
    ending. Enabled here on purpose: the assertion is that it stays unreached
    even when it is available.
    """
    def urlopen_side(req, timeout=30):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "localhost" in url:
            raise AssertionError("SPARK reached local ollama after a tier failure")
        raise urllib.error.URLError("M5 unreachable")

    old_val = os.environ.get("PX_MIND_LOCAL_OLLAMA")
    os.environ["PX_MIND_LOCAL_OLLAMA"] = "1"
    try:
        with patch("subprocess.run", side_effect=AssertionError("spawned a process")), \
             patch("urllib.request.urlopen", side_effect=urlopen_side):
            result = call_llm("prompt", "system", persona="spark")

        assert result.get(pxh.mind.BRAIN_DEFER) is True, result
    finally:
        if old_val is None:
            os.environ.pop("PX_MIND_LOCAL_OLLAMA", None)
        else:
            os.environ["PX_MIND_LOCAL_OLLAMA"] = old_val


def test_skips_local_ollama_when_not_opted_in():
    """A cognition-tier failure is an error, never a Pi-local fallback."""
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


def test_m5_failure_never_spawns_a_process():
    """A cognition-tier failure is terminal for reflection.

    This used to assert that a failed tier reached a *resident* session
    instead of spawning one. #317 Phase 3 deleted the resident session, so the
    surviving half is the one that always mattered: no process appears, and the
    result is a defer. The module it used to reach is asserted absent in
    `tests/test_cognition_tier_routing.py`.
    """
    with patch("urllib.request.urlopen",
               side_effect=urllib.error.URLError("M5 unreachable")), \
         patch("subprocess.run", side_effect=AssertionError("spawned a process")):
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


# ── There is no tier 2 ────────────────────────────────────────────────────
#
# There used to be a `claude -p` subprocess here, then a resident session, and
# these tests pinned both in turn. The old comment said "a brain that is down
# must be invisible" — that was the bug, stated as a requirement. Making a down
# brain invisible meant spawning a fresh Claude on a Pi that was already
# saturated, which is how one slow tmux keystroke became two competing Claude
# processes, a 120s timeout, an M5 timeout and a 403 from Ollama Cloud
# (2026-08-19).
#
# There is no tier 2 at all now (#317 Phase 3). A failure is *visible and
# cheap*: reflection defers and backs off. Reflection is the most skippable
# work SPARK does; failing to have a thought costs nothing, and a loaded Pi
# should be doing less, not searching wider.


def test_reflection_spawns_nothing_when_the_tier_is_down():
    """No process, no wider search, whatever the failure looked like."""
    ran = []

    def _record(*args, **kwargs):
        ran.append(args[0] if args else kwargs.get("args"))
        return _fake_claude(1, stderr="should never be reached")

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("M5 down")), \
         patch("subprocess.run", side_effect=_record):
        result = call_llm("prompt", "system", persona="spark")

    assert result.get(pxh.mind.BRAIN_DEFER) is True
    assert ran == [], ran


def test_reflection_defers_when_the_tier_is_unavailable():
    """A cognition-tier failure is terminal. No process, no wider search."""
    spawned = []

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("M5 down")), \
         patch("subprocess.run", side_effect=lambda *a, **k: spawned.append(a)):
        result = call_llm("prompt", "system", persona="spark")

    assert "error" in result
    assert result.get(pxh.mind.BRAIN_DEFER) is True, result
    assert spawned == [], f"reflection spawned a process: {spawned}"


def test_reflection_failure_does_not_escalate_past_the_cognition_tier():
    """The 403 in the 2026-08-19 cascade was reached *from* a failure above it.

    #308 made the cloud the *primary* tier rather than the bottom rung, so the
    invariant is no longer "nothing reaches ollama.com" — it is "nothing is
    tried after the cognition tier fails": no CLI, no Pi-local daemon, no
    second model. Since #317 Phase 3 there is no second destination to try,
    which is the strongest form this invariant can take.
    """
    seen_urls = []

    def _record(req, *a, **kw):
        seen_urls.append(req.full_url if hasattr(req, "full_url") else str(req))
        raise urllib.error.URLError("cognition tier down")

    with patch("urllib.request.urlopen", side_effect=_record), \
         patch("subprocess.run", side_effect=AssertionError("spawned a process")):
        result = call_llm("prompt", "system", persona="spark")

    # Assert on *where* the requests went, not how many there were: the tier may
    # probe before generating, and its own offline backoff may skip the network
    # entirely, so any count is a hostage to unrelated state.
    hosts = {urllib.parse.urlsplit(u).hostname for u in seen_urls}
    assert hosts <= {"ollama.com"}, seen_urls
    assert not [u for u in seen_urls if u.startswith("http://")], seen_urls
    assert result.get(pxh.mind.BRAIN_DEFER) is True, "the tier did not stop the chain"


def test_the_retired_dial_cannot_restore_a_cold_path():
    """There is no rollback destination any more, so the dial cannot open one.

    PX_BRAIN_KINDS used to be able to route reflection back to a session, and
    before that to `claude -p`. The variable is still read by nothing; whatever
    it is set to, no process may be spawned.
    """
    with patch.dict(os.environ, {"PX_BRAIN_KINDS": "research,compose"}), \
         patch("urllib.request.urlopen", side_effect=urllib.error.URLError("M5 down")), \
         patch("subprocess.run", side_effect=AssertionError("spawned a process")):
        result = call_llm("prompt", "system", persona="spark")

    assert "error" in result
