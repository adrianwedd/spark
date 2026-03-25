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


# ── Tier-2 fallback: Claude fails → M1 Ollama succeeds ─────────────

def test_falls_back_to_m1_ollama_when_claude_fails():
    with patch("subprocess.run", return_value=_fake_claude(1, stderr="auth error")), \
         patch("urllib.request.urlopen", return_value=_fake_ollama_cm("quantum foam")):
        result = call_llm("prompt", "system", persona="spark")
    assert "error" not in result
    assert "quantum foam" in result["response"]


# ── Tier-3 fallback: Claude + M1 fail → local Ollama succeeds ──────

def test_falls_back_to_local_ollama_when_m1_fails():
    call_count = [0]

    def urlopen_side(req, timeout=30):
        call_count[0] += 1
        if call_count[0] == 1:
            raise urllib.error.URLError("M1 unreachable")
        return _fake_ollama_cm("running on fumes")  # return full CM, not unwrapped

    # Local fallback is opt-in via PX_MIND_LOCAL_OLLAMA=1
    old_val = os.environ.get("PX_MIND_LOCAL_OLLAMA")
    os.environ["PX_MIND_LOCAL_OLLAMA"] = "1"
    try:
        with patch("subprocess.run", return_value=_fake_claude(1, stderr="offline")), \
             patch("urllib.request.urlopen", side_effect=urlopen_side):
            result = call_llm("prompt", "system", persona="spark")

        assert call_count[0] == 2, f"expected 2 urlopen calls, got {call_count[0]}"
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
