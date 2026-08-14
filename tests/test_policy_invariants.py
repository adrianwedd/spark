"""Protected constitutional suite for #174.

BLACKLISTED from px-evolve (see pxh.claude_session.BLACKLIST_FILES), together
with src/pxh/policy.py. Ordinary, evolvable policy coverage lives in the
whitelisted tests/test_policy.py — keep that split.

Every assertion here pins BOTH a policy rule AND that a real chokepoint
(voice_loop.validate_action / mind.expression) actually invokes it. That
distinction is the whole point: a direct-only suite against policy.evaluate()
would not catch an evolution PR that deletes the call site from voice_loop.py
(whitelisted) and adjusts tests/test_voice_loop.py (whitelisted) to match. Both
mind.py and voice_loop.py remain legitimately evolvable; it is these
assertions, not file protection on those modules, that catch a call-site
deletion — pytest must pass before px-evolve opens a PR.

No prompt text appears anywhere in this file. That is the issue's acceptance
criterion: load-bearing traits have observable tests that do not depend on
exact prompt wording.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from pxh import mind, policy, voice_loop

HOBART_TZ = ZoneInfo("Australia/Hobart")
NIGHT_TS = dt.datetime(2026, 1, 1, 22, 0, tzinfo=HOBART_TZ).timestamp()
DAY_TS = dt.datetime(2026, 1, 1, 12, 0, tzinfo=HOBART_TZ).timestamp()


@pytest.fixture(autouse=True)
def _daytime(monkeypatch):
    """Pin the clock to midday unless a test says otherwise, so these
    invariants don't quietly change meaning depending on when CI runs."""
    monkeypatch.setattr(voice_loop, "_policy_now", lambda: DAY_TS)
    monkeypatch.setattr(voice_loop, "_load_awareness_for_policy", lambda: {})
    monkeypatch.setattr(voice_loop, "load_session", lambda: {})


# ---------------------------------------------------------------------------
# Quiet mode blocks audio at the real chokepoint, on both origins.
# ---------------------------------------------------------------------------

def test_voice_loop_quiet_mode_blocks_tool_voice(monkeypatch):
    monkeypatch.setattr(voice_loop, "load_session", lambda: {"spark_quiet_mode": True})
    tool, env = voice_loop.validate_action({"tool": "tool_voice", "params": {"text": "hi"}})
    assert tool == "tool_emote"
    assert env.get("PX_EMOTE") == "idle"


def test_mind_expression_quiet_mode_blocks_greet(monkeypatch):
    monkeypatch.setattr(mind, "_is_night_silence", lambda h: False)
    monkeypatch.setattr(mind, "load_session",
                        lambda: {"persona": "", "spark_quiet_mode": True})
    monkeypatch.setattr(mind, "update_session", lambda **k: None)
    dispatched = []
    monkeypatch.setattr(mind, "_run_voice", lambda env, label="": dispatched.append(label))
    result = mind.expression(
        {"action": "greet", "thought": "hi"}, dry=True,
        awareness={"obi_mode": "active", "calendar": {}, "ha_context": {}},
    )
    assert result is False
    assert dispatched == []


# ---------------------------------------------------------------------------
# Interactive-only: night silence and on-call suppress the voice loop. The
# autonomous rule stays in mind.py with its own tests — one invariant, one
# enforcement site.
# ---------------------------------------------------------------------------

def test_voice_loop_night_silence_blocks_tool_voice(monkeypatch):
    monkeypatch.setattr(voice_loop, "_policy_now", lambda: NIGHT_TS)
    tool, env = voice_loop.validate_action({"tool": "tool_voice", "params": {"text": "hi"}})
    assert tool == "tool_emote"


def test_voice_loop_on_call_blocks_tool_voice(monkeypatch):
    monkeypatch.setattr(voice_loop, "_load_awareness_for_policy",
                        lambda: {"ha_context": {"adrian_on_call": True}})
    tool, env = voice_loop.validate_action({"tool": "tool_voice", "params": {"text": "hi"}})
    assert tool == "tool_emote"


def test_voice_loop_hot_mic_blocks_tool_voice(monkeypatch):
    monkeypatch.setattr(voice_loop, "_load_awareness_for_policy",
                        lambda: {"ha_context": {"adrian_mic_active": True}})
    tool, env = voice_loop.validate_action({"tool": "tool_voice", "params": {"text": "hi"}})
    assert tool == "tool_emote"


def test_mind_night_silence_still_delegates_to_the_shared_clock():
    """The autonomous rule keeps its own location, but not its own clock."""
    for hour in range(24):
        assert mind._is_night_silence(hour) == policy.is_night_hour(hour)


# ---------------------------------------------------------------------------
# Personas cannot bypass any rule. voice_loop.py replaces the entire system
# prompt when a persona is active, so any prompt-only safety behaviour
# silently vanishes — but the dispatcher is shared and does not branch on
# session["persona"] at all.
# ---------------------------------------------------------------------------

def test_persona_active_does_not_bypass_quiet_mode(monkeypatch):
    monkeypatch.setattr(voice_loop, "load_session",
                        lambda: {"spark_quiet_mode": True, "persona": "gremlin"})
    tool, env = voice_loop.validate_action({"tool": "tool_chat", "params": {"text": "hi"}})
    assert tool == "tool_emote"


def test_persona_chat_tools_are_audio():
    for tool in ("tool_chat", "tool_chat_vixen"):
        assert voice_loop.VOICE_EFFECT_TABLE[tool] == "audio", tool


# ---------------------------------------------------------------------------
# Substitution safety: a substitute is re-evaluated and can never recurse or
# come back less restricted.
# ---------------------------------------------------------------------------

def test_presence_substitute_is_not_itself_blocked():
    verdict = policy.evaluate(
        "tool_emote", {"name": "idle"}, effect="presence", origin="interactive",
        session={"spark_quiet_mode": True}, awareness={}, now=NIGHT_TS,
    )
    assert verdict.allowed is True


def test_recursion_guard_raises_if_a_presence_effect_were_ever_blocked():
    with pytest.raises(ValueError):
        policy.evaluate(
            "tool_emote", {}, effect="audio", origin="interactive",
            session={"spark_quiet_mode": True}, awareness={}, now=NIGHT_TS,
            _depth=1,
        )


# ---------------------------------------------------------------------------
# Quiet mode has exactly one exit, and that exit is silent. No tool holds an
# audio carve-out — the escape hatch is a state transition that emits nothing,
# not a speaking tool that policy has been told to ignore.
# ---------------------------------------------------------------------------

def test_tool_quiet_end_is_the_one_exit_and_is_not_audio(monkeypatch):
    monkeypatch.setattr(voice_loop, "load_session", lambda: {"spark_quiet_mode": True})
    assert voice_loop.classify_effect("tool_quiet", {"action": "end"}) != "audio"
    tool, env = voice_loop.validate_action(
        {"tool": "tool_quiet", "params": {"action": "end"}})
    assert tool == "tool_quiet"


def test_tool_repair_is_suppressed_during_quiet_mode(monkeypatch):
    """Repair speaks (bin/tool-repair), so it obeys quiet mode like anything
    else that speaks. It is not a quiet-mode exit."""
    monkeypatch.setattr(voice_loop, "load_session", lambda: {"spark_quiet_mode": True})
    tool, env = voice_loop.validate_action({"tool": "tool_repair", "params": {}})
    assert tool == "tool_emote"


def test_no_tool_can_speak_under_an_innocent_name():
    """An outer tool name never buys a KNOWN audio sink a bypass.

    Bounded exactly as the whitelisted copy in tests/test_policy.py is: this
    is a text scan, so it catches the common accidental case and cannot see
    runtime-assembled subprocess paths, deeper helper indirection, Python
    imports into audio helpers, or sinks that don't exist yet. Duplicated
    deliberately — that file is evolvable and this one is not.
    """
    sinks = re.compile(
        r"tool-voice\b|tool-voice-persona\b|tool-announce\b|tool-play-sound\b"
        r"|tool-chat\b|tool-chat-vixen\b|px-perform\b"
    )
    bin_dir = Path(__file__).resolve().parents[1] / "bin"
    for tool in sorted(voice_loop.ALLOWED_TOOLS):
        script = bin_dir / tool.replace("_", "-")
        if not script.exists() or not sinks.search(script.read_text(encoding="utf-8")):
            continue
        if tool in voice_loop.VOICE_EFFECT_OVERRIDES:
            # Param-dependent: the default branch must still be audio, so an
            # omitted param cannot slip audio through as something quieter.
            assert voice_loop.classify_effect(tool, {}) == "audio", tool
            continue
        assert voice_loop.VOICE_EFFECT_TABLE[tool] == "audio", tool


# ---------------------------------------------------------------------------
# Classification stays exhaustive. A new tool or autonomous action must be
# classified deliberately, not default into unsuppressed audio.
# ---------------------------------------------------------------------------

def test_effect_tables_cover_their_whole_vocabulary():
    assert set(voice_loop.VOICE_EFFECT_TABLE) == voice_loop.ALLOWED_TOOLS
    assert set(mind.MIND_EFFECT_TABLE) == mind.VALID_ACTIONS


def test_override_mechanism_stays_narrow():
    """One member, because bin/tool-quiet is three programs under one name. A
    second one means splitting that tool, not describing it more cleverly."""
    assert set(voice_loop.VOICE_EFFECT_OVERRIDES) == {"tool_quiet"}
