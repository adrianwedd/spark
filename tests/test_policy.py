"""Ordinary coverage for pxh.policy and its two call-site classification tables.

This file is WHITELISTED for self-evolution. The constitutional assertions —
the ones that must survive an evolution PR — live in the blacklisted
tests/test_policy_invariants.py. Keep that split: broad or adaptive policy
coverage belongs here, pinned invariants belong there.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from pxh import policy

HOBART_TZ = ZoneInfo("Australia/Hobart")
NIGHT_TS = dt.datetime(2026, 1, 1, 22, 0, tzinfo=HOBART_TZ).timestamp()
DAY_TS = dt.datetime(2026, 1, 1, 12, 0, tzinfo=HOBART_TZ).timestamp()


# ---------------------------------------------------------------------------
# is_night_hour
# ---------------------------------------------------------------------------

def test_is_night_hour_matches_bounds():
    # NIGHT_SILENCE_START_H=19, NIGHT_SILENCE_END_H=7 (spark_config.py)
    assert policy.is_night_hour(19) is True
    assert policy.is_night_hour(23) is True
    assert policy.is_night_hour(0) is True
    assert policy.is_night_hour(6) is True
    assert policy.is_night_hour(7) is False
    assert policy.is_night_hour(18) is False


def test_is_night_hour_reads_config_at_call_time(monkeypatch):
    from pxh import spark_config
    monkeypatch.setattr(spark_config, "NIGHT_SILENCE_START_H", 21)
    monkeypatch.setattr(spark_config, "NIGHT_SILENCE_END_H", 5)
    assert policy.is_night_hour(20) is False
    assert policy.is_night_hour(21) is True
    assert policy.is_night_hour(5) is False


# ---------------------------------------------------------------------------
# evaluate()
# ---------------------------------------------------------------------------

def test_non_audio_effect_always_allowed():
    for effect in ("presence", "other"):
        verdict = policy.evaluate(
            "anything", {}, effect=effect, origin="interactive",
            session={"spark_quiet_mode": True}, awareness={}, now=NIGHT_TS,
        )
        assert verdict.allowed is True


def test_quiet_mode_blocks_audio_both_origins():
    for origin in ("interactive", "autonomous"):
        verdict = policy.evaluate(
            "tool_voice", {}, effect="audio", origin=origin,
            session={"spark_quiet_mode": True}, awareness={}, now=DAY_TS,
        )
        assert verdict.allowed is False
        assert verdict.reason == "quiet_mode"
        assert verdict.suggest_presence_substitute is True


def test_quiet_mode_requires_exactly_true():
    """Truthy-but-not-True values are data errors, not consent to speak —
    but they must not silently enable suppression either. `is True` is the
    contract; anything else reads as absent."""
    for value in (None, False, 0, ""):
        verdict = policy.evaluate(
            "tool_voice", {}, effect="audio", origin="interactive",
            session={"spark_quiet_mode": value}, awareness={}, now=DAY_TS,
        )
        assert verdict.allowed is True, value


def test_night_silence_blocks_audio_interactive_only():
    interactive = policy.evaluate(
        "tool_voice", {}, effect="audio", origin="interactive",
        session={}, awareness={}, now=NIGHT_TS,
    )
    assert interactive.allowed is False
    assert interactive.reason == "night_silence"
    assert interactive.suggest_presence_substitute is True

    autonomous = policy.evaluate(
        "greet", {}, effect="audio", origin="autonomous",
        session={}, awareness={}, now=NIGHT_TS,
    )
    # mind.py enforces this itself, not policy.py, in v1 — see spec "v1 rules".
    assert autonomous.allowed is True


def test_on_call_blocks_audio_interactive_only():
    for flag in ("adrian_on_call", "adrian_mic_active"):
        aw = {"ha_context": {flag: True}}

        interactive = policy.evaluate(
            "tool_voice", {}, effect="audio", origin="interactive",
            session={}, awareness=aw, now=DAY_TS,
        )
        assert interactive.allowed is False, flag
        assert interactive.reason == "on_call"

        autonomous = policy.evaluate(
            "greet", {}, effect="audio", origin="autonomous",
            session={}, awareness=aw, now=DAY_TS,
        )
        assert autonomous.allowed is True, flag


def test_missing_ha_context_does_not_raise():
    for aw in ({}, {"ha_context": None}, {"ha_context": {}}):
        verdict = policy.evaluate(
            "tool_voice", {}, effect="audio", origin="interactive",
            session={}, awareness=aw, now=DAY_TS,
        )
        assert verdict.allowed is True


def test_allowed_when_no_condition_holds():
    verdict = policy.evaluate(
        "tool_voice", {}, effect="audio", origin="interactive",
        session={}, awareness={}, now=DAY_TS,
    )
    assert verdict.allowed is True
    assert verdict.reason == "ok"


def test_every_block_carries_a_reason():
    blocked = [
        policy.evaluate("tool_voice", {}, effect="audio", origin="interactive",
                        session={"spark_quiet_mode": True}, awareness={}, now=DAY_TS),
        policy.evaluate("tool_voice", {}, effect="audio", origin="interactive",
                        session={}, awareness={}, now=NIGHT_TS),
        policy.evaluate("tool_voice", {}, effect="audio", origin="interactive",
                        session={}, awareness={"ha_context": {"adrian_on_call": True}},
                        now=DAY_TS),
    ]
    for verdict in blocked:
        assert verdict.allowed is False
        assert verdict.reason
        assert verdict.reason != "ok"


def test_substitute_reevaluation_cannot_recurse_past_depth_1():
    with pytest.raises(ValueError):
        policy.evaluate(
            "tool_emote", {}, effect="audio", origin="interactive",
            session={"spark_quiet_mode": True}, awareness={}, now=DAY_TS,
            _depth=1,
        )


def test_depth_1_is_fine_when_nothing_blocks():
    verdict = policy.evaluate(
        "tool_emote", {"name": "idle"}, effect="presence", origin="interactive",
        session={"spark_quiet_mode": True}, awareness={}, now=NIGHT_TS, _depth=1,
    )
    assert verdict.allowed is True


# ---------------------------------------------------------------------------
# mind.py effect classification (Task 2)
# ---------------------------------------------------------------------------

def test_mind_effect_table_is_exhaustive():
    from pxh import mind
    assert set(mind.MIND_EFFECT_TABLE.keys()) == mind.VALID_ACTIONS
    assert all(v in ("audio", "presence", "other") for v in mind.MIND_EFFECT_TABLE.values())


def test_mind_effect_table_classifies_known_audio_actions_as_audio():
    """Classified from real dispatch, not from names: scan/look_at/look_around
    each call _run_voice in expression()."""
    from pxh import mind
    for action in ("greet", "greet_arrival", "comment", "weather_comment",
                   "morning_fact", "scan", "look_at", "look_around",
                   "play_sound", "time_check", "calendar_check", "announce",
                   "message_obi"):
        assert mind.MIND_EFFECT_TABLE[action] == "audio", action


def test_is_night_silence_delegates_to_policy():
    from pxh import mind
    for hour in range(24):
        assert mind._is_night_silence(hour) == policy.is_night_hour(hour)


# ---------------------------------------------------------------------------
# voice_loop.py effect classification (Task 4)
# ---------------------------------------------------------------------------

def test_voice_effect_table_is_exhaustive():
    from pxh import voice_loop
    assert set(voice_loop.VOICE_EFFECT_TABLE.keys()) == voice_loop.ALLOWED_TOOLS
    assert all(v in ("audio", "presence", "other")
               for v in voice_loop.VOICE_EFFECT_TABLE.values())


def test_voice_effect_table_classifies_known_audio_tools_as_audio():
    from pxh import voice_loop
    for tool in ("tool_voice", "tool_announce", "tool_chat", "tool_chat_vixen",
                 "tool_play_sound", "tool_time", "tool_gws_calendar",
                 "tool_gws_sheets_log", "tool_qa", "tool_describe_scene",
                 "tool_timer", "tool_story", "tool_recall", "tool_routine",
                 "tool_checkin", "tool_celebrate", "tool_transition",
                 "tool_breathe", "tool_dopamine_menu", "tool_sensory_check",
                 "tool_perform", "tool_repair"):
        assert voice_loop.VOICE_EFFECT_TABLE[tool] == "audio", tool


def test_tool_quiet_effect_varies_by_action_param():
    from pxh import voice_loop
    assert set(voice_loop.VOICE_EFFECT_OVERRIDES) == {"tool_quiet"}
    assert voice_loop.classify_effect("tool_quiet", {"action": "start"}) == "audio"
    assert voice_loop.classify_effect("tool_quiet", {"action": "check"}) == "presence"
    assert voice_loop.classify_effect("tool_quiet", {"action": "end"}) == "other"
    # bin/tool-quiet defaults to "start" — the loudest branch — when unset.
    assert voice_loop.classify_effect("tool_quiet", {}) == "audio"


def test_no_non_audio_tool_can_reach_an_audio_sink():
    """A KNOWN audio sink may not be reachable from a non-audio-classified
    tool. That is the whole claim — not "audio can never bypass policy".

    This is a text scan over bin/: it catches the common, accidental case (a
    tool quietly growing a spoken confirmation) and misses runtime-assembled
    subprocess paths, deeper helper indirection, Python imports into audio
    helpers, and sinks that don't exist yet. A floor, not a proof.
    """
    from pxh import voice_loop

    sinks = re.compile(
        r"tool-voice\b|tool-voice-persona\b|tool-announce\b|tool-play-sound\b"
        r"|tool-chat\b|tool-chat-vixen\b|px-perform\b"
    )
    bin_dir = Path(__file__).resolve().parents[1] / "bin"
    offenders = []
    for tool in sorted(voice_loop.ALLOWED_TOOLS):
        script = bin_dir / tool.replace("_", "-")
        if not script.exists():
            continue
        if not sinks.search(script.read_text(encoding="utf-8")):
            continue
        if tool in voice_loop.VOICE_EFFECT_OVERRIDES:
            continue  # param-dependent; pinned by its own test above
        if voice_loop.VOICE_EFFECT_TABLE[tool] != "audio":
            offenders.append(tool)
    assert offenders == [], (
        f"tools reach a known audio sink but are not classified 'audio': {offenders}"
    )
