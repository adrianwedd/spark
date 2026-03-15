"""Tests for px-mind three-tier LLM fallback: Claude -> M1 Ollama -> local Ollama."""
from __future__ import annotations

import os, sys, types, json
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).parent.parent


def _load_mind():
    src = (PROJECT_ROOT / "bin" / "px-mind").read_text()
    start = src.index("<<'PY'\n") + len("<<'PY'\n")
    end   = src.rindex("\nPY\n")
    py_src = src[start:end]

    import datetime as _dt
    stub_keys = ("pxh", "pxh.state", "pxh.logging", "pxh.time", "pxh.token_log",
                  "pxh.voice_loop")
    saved = {k: sys.modules.get(k) for k in stub_keys}

    stub_pxh   = types.ModuleType("pxh")
    stub_state = types.ModuleType("pxh.state")
    stub_state.load_session   = lambda: {}
    stub_state.update_session = lambda **kw: None
    stub_state.save_session   = lambda s: None
    stub_log  = types.ModuleType("pxh.logging")
    stub_log.log_event = lambda *a, **kw: None
    stub_time = types.ModuleType("pxh.time")
    stub_time.utc_timestamp = lambda: _dt.datetime.now(_dt.timezone.utc).isoformat()
    stub_token_log = types.ModuleType("pxh.token_log")
    stub_token_log.log_usage = lambda *a, **kw: None
    stub_voice_loop = types.ModuleType("pxh.voice_loop")
    stub_voice_loop.PERSONA_VOICE_ENV = {
        "vixen": {"PX_PERSONA": "vixen", "PX_VOICE_VARIANT": "en+f4",
                  "PX_VOICE_PITCH": "72", "PX_VOICE_RATE": "135"},
        "gremlin": {"PX_PERSONA": "gremlin", "PX_VOICE_VARIANT": "en+croak",
                    "PX_VOICE_PITCH": "20", "PX_VOICE_RATE": "180"},
        "spark": {"PX_PERSONA": "spark", "PX_VOICE_VARIANT": "en-gb",
                  "PX_VOICE_PITCH": "95", "PX_VOICE_RATE": "100"},
    }

    for k, m in [("pxh", stub_pxh), ("pxh.state", stub_state),
                 ("pxh.logging", stub_log), ("pxh.time", stub_time),
                 ("pxh.token_log", stub_token_log),
                 ("pxh.voice_loop", stub_voice_loop)]:
        sys.modules[k] = m

    env_patch = {
        "PROJECT_ROOT": str(PROJECT_ROOT),
        "LOG_DIR":      str(PROJECT_ROOT / "logs"),
        "PX_STATE_DIR": str(PROJECT_ROOT / "state"),
        "PX_OLLAMA_HOST":             "http://M1.local:11434",
        "PX_MIND_LOCAL_OLLAMA_HOST":  "http://localhost:11434",
        "PX_MIND_LOCAL_MODEL":        "deepseek-r1:1.5b",
    }
    old_env = {k: os.environ.get(k) for k in env_patch}
    os.environ.update(env_patch)

    globs: dict = {"__file__": str(PROJECT_ROOT / "bin" / "px-mind")}
    try:
        exec(compile(py_src, "bin/px-mind", "exec"), globs)  # noqa: S102
    finally:
        for k, old_mod in saved.items():
            if old_mod is None: sys.modules.pop(k, None)
            else:               sys.modules[k] = old_mod
        for k, old_v in old_env.items():
            if old_v is None: os.environ.pop(k, None)
            else:             os.environ[k] = old_v

    return globs


_MIND = _load_mind()


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
    call_llm = _MIND["call_llm"]
    with patch("subprocess.run", return_value=_fake_claude(1, stderr="auth error")), \
         patch("urllib.request.urlopen", return_value=_fake_ollama_cm("quantum foam")):
        result = call_llm("prompt", "system", persona="spark")
    assert "error" not in result
    assert "quantum foam" in result["response"]


# ── Tier-3 fallback: Claude + M1 fail → local Ollama succeeds ──────

def test_falls_back_to_local_ollama_when_m1_fails():
    import urllib.error
    call_llm  = _MIND["call_llm"]
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
    import urllib.error
    call_llm = _MIND["call_llm"]

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
    import urllib.error
    call_llm = _MIND["call_llm"]
    with patch("subprocess.run", return_value=_fake_claude(1, stderr="offline")), \
         patch("urllib.request.urlopen",
               side_effect=urllib.error.URLError("all down")):
        result = call_llm("prompt", "system", persona="spark")
    assert "error" in result
