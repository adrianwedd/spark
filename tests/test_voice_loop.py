import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from pxh import voice_loop
from pxh.voice_loop import build_model_prompt, validate_action, VoiceLoopError

HOBART_TZ = ZoneInfo("Australia/Hobart")
DAY_TS = dt.datetime(2026, 1, 1, 12, 0, tzinfo=HOBART_TZ).timestamp()
NIGHT_TS = dt.datetime(2026, 1, 1, 22, 0, tzinfo=HOBART_TZ).timestamp()


@pytest.fixture(autouse=True)
def _daytime_no_suppression(monkeypatch):
    """Neutralize behavioural policy (#174) for tests that aren't about it.

    validate_action() now consults pxh.policy, which reads the wall clock and
    the live session. Without this, every audio-tool test in this file would
    pass by day and fail after 19:00 Hobart. Policy tests below override these
    deliberately.
    """
    monkeypatch.setattr(voice_loop, "_policy_now", lambda: DAY_TS)
    monkeypatch.setattr(voice_loop, "load_session", lambda: {})
    monkeypatch.setattr(voice_loop, "_load_awareness_for_policy", lambda: {})


def test_build_model_prompt_includes_highlights():
    system_prompt = "SYSTEM"
    state = {
        "mode": "live",
        "confirm_motion_allowed": True,
        "wheels_on_blocks": True,
        "battery_pct": 72,
        "battery_ok": True,
        "last_motion": "px-circle",
        "last_action": "tool_circle",
        "last_weather": {
            "summary": "At Grove, it's 12 degrees."},
        "history": [
            {"ts": "t1", "event": "status"},
            {"ts": "t2", "event": "circle"},
            {"ts": "t3", "event": "weather"},
            {"ts": "t4", "event": "voice"},
        ],
    }
    prompt = build_model_prompt(system_prompt, state, "Weather now")
    assert "Current highlights:" in prompt
    assert '"mode": "live"' in prompt
    assert 'last_weather_summary' in prompt
    assert 'Recent events:' in prompt
    assert '"event": "weather"' in prompt
    assert 'User transcript: Weather now' in prompt


def test_validate_action_rejects_non_numeric_params():
    """Malformed numeric params should raise VoiceLoopError, not ValueError."""
    with pytest.raises(VoiceLoopError, match="invalid numeric"):
        validate_action({"tool": "tool_circle", "params": {"speed": "fast"}})
    with pytest.raises(VoiceLoopError, match="invalid numeric"):
        validate_action({"tool": "tool_drive", "params": {"speed": None, "direction": "forward"}})
    with pytest.raises(VoiceLoopError, match="invalid numeric"):
        validate_action({"tool": "tool_look", "params": {"pan": "left"}})


def test_validate_action_accepts_string_numbers():
    """LLMs sometimes send numbers as strings — should still work."""
    tool, env = validate_action({"tool": "tool_circle", "params": {"speed": "30", "duration": "6"}})
    assert tool == "tool_circle"
    assert env["PX_SPEED"] == "30"


def test_validate_action_rejects_unknown_tool():
    with pytest.raises(VoiceLoopError, match="unsupported tool"):
        validate_action({"tool": "tool_hack_nasa", "params": {}})


def test_validate_action_wander_mode():
    """mode param sanitised to avoid/explore."""
    _, env = validate_action({"tool": "tool_wander", "params": {"steps": 5, "mode": "explore"}})
    assert env["PX_WANDER_MODE"] == "explore"
    _, env2 = validate_action({"tool": "tool_wander", "params": {"steps": 5, "mode": "invalid"}})
    assert env2["PX_WANDER_MODE"] == "avoid"
    _, env3 = validate_action({"tool": "tool_wander", "params": {"steps": 5}})
    assert env3["PX_WANDER_MODE"] == "avoid"


def test_validate_action_wander_duration():
    """duration clamped to 30-300."""
    _, env = validate_action({"tool": "tool_wander", "params": {"mode": "explore", "duration": 500}})
    assert env["PX_WANDER_DURATION_S"] == "300"
    _, env2 = validate_action({"tool": "tool_wander", "params": {"mode": "explore", "duration": 10}})
    assert env2["PX_WANDER_DURATION_S"] == "30"
    _, env3 = validate_action({"tool": "tool_wander", "params": {"mode": "explore", "duration": 180}})
    assert env3["PX_WANDER_DURATION_S"] == "180"
    # avoid mode should not set duration
    _, env4 = validate_action({"tool": "tool_wander", "params": {"mode": "avoid", "duration": 180}})
    assert "PX_WANDER_DURATION_S" not in env4


from pxh.voice_loop import ALLOWED_TOOLS, TOOL_COMMANDS


def test_tool_announce_registered():
    assert "tool_announce" in ALLOWED_TOOLS
    assert "tool_announce" in TOOL_COMMANDS


def test_validate_announce_clamps_text():
    tool, env = validate_action({"tool": "tool_announce", "params": {"text": "x" * 500}})
    assert tool == "tool_announce"
    assert len(env["PX_ANNOUNCE_TEXT"]) == 200  # ANNOUNCE_MAX_CHARS


def test_validate_announce_requires_text():
    with pytest.raises(VoiceLoopError):
        validate_action({"tool": "tool_announce", "params": {"text": "   "}})


def test_validate_announce_rejects_any_disallowed_target():
    # Mixed good+bad must RAISE, not silently drop the bad one.
    with pytest.raises(VoiceLoopError):
        validate_action({"tool": "tool_announce", "params": {
            "text": "hi", "targets": ["media_player.nest_hub_max", "media_player.evil"]}})


def test_validate_announce_rejects_all_bad_targets():
    with pytest.raises(VoiceLoopError):
        validate_action({"tool": "tool_announce", "params": {
            "text": "hi", "targets": ["media_player.evil"]}})


def test_validate_announce_single_target_from_allowed_list():
    # Multiple ALLOWED targets -> v1 takes exactly one (single-target).
    _, env = validate_action({"tool": "tool_announce", "params": {
        "text": "hi", "targets": ["media_player.nest_hub_max", "media_player.nest_mini"]}})
    assert env["PX_ANNOUNCE_TARGETS"] == "media_player.nest_hub_max"


def test_remember_from_the_voice_loop_is_typed_as_a_human_report():
    """A note taken while a person talks to SPARK records their report."""
    _, env = validate_action({"tool": "tool_remember",
                              "params": {"text": "Obi is nine on Saturday"}})
    assert env["PX_NOTE_KIND"] == "report"


def test_the_model_cannot_choose_the_provenance_of_its_own_note():
    _, env = validate_action({"tool": "tool_remember",
                              "params": {"text": "the hallway is empty",
                                         "kind": "observation",
                                         "confidence": 1.0,
                                         "provenance": {"kind": "verification"},
                                         "supersedes": ["some-id"]}})
    assert env["PX_NOTE_KIND"] == "report"
    assert not any(k.lower().endswith(("confidence", "supersedes")) for k in env)


# ── behavioural policy (#174) ────────────────────────────────────────────────

def _no_policy_blockers(monkeypatch, **session):
    """Daytime, no on-call, no quiet mode unless the test asks for it."""
    monkeypatch.setattr(voice_loop, "load_session", lambda: session)


def test_validate_action_downgrades_tool_voice_in_quiet_mode(monkeypatch):
    _no_policy_blockers(monkeypatch, spark_quiet_mode=True)
    tool, env = voice_loop.validate_action({"tool": "tool_voice", "params": {"text": "hi"}})
    assert tool == "tool_emote"
    assert env["PX_EMOTE"] == "idle"


def test_validate_action_allows_tool_voice_when_not_quiet(monkeypatch):
    _no_policy_blockers(monkeypatch, spark_quiet_mode=False)
    tool, env = voice_loop.validate_action({"tool": "tool_voice", "params": {"text": "hi"}})
    assert tool == "tool_voice"


def test_validate_action_allows_tool_quiet_end_during_quiet_mode(monkeypatch):
    """The one permitted exit — permitted because it emits nothing after the
    Task 3 refactor, not because it is exempt."""
    _no_policy_blockers(monkeypatch, spark_quiet_mode=True)
    tool, env = voice_loop.validate_action({"tool": "tool_quiet", "params": {"action": "end"}})
    assert tool == "tool_quiet"


def test_validate_action_downgrades_tool_repair_in_quiet_mode(monkeypatch):
    _no_policy_blockers(monkeypatch, spark_quiet_mode=True)
    tool, env = voice_loop.validate_action({"tool": "tool_repair", "params": {}})
    assert tool == "tool_emote"


def test_validate_action_downgrades_tool_quiet_start_when_already_quiet(monkeypatch):
    _no_policy_blockers(monkeypatch, spark_quiet_mode=True)
    tool, env = voice_loop.validate_action({"tool": "tool_quiet", "params": {"action": "start"}})
    assert tool == "tool_emote"


def test_validate_action_blocks_audio_at_night(monkeypatch):
    monkeypatch.setattr(voice_loop, "_policy_now", lambda: NIGHT_TS)
    tool, env = voice_loop.validate_action({"tool": "tool_voice", "params": {"text": "hi"}})
    assert tool == "tool_emote"


def test_validate_action_blocks_audio_when_adrian_on_call(monkeypatch):
    monkeypatch.setattr(voice_loop, "_load_awareness_for_policy",
                        lambda: {"ha_context": {"adrian_on_call": True}})
    tool, env = voice_loop.validate_action({"tool": "tool_voice", "params": {"text": "hi"}})
    assert tool == "tool_emote"


def test_validate_action_leaves_presence_tools_alone_in_quiet_mode(monkeypatch):
    _no_policy_blockers(monkeypatch, spark_quiet_mode=True)
    tool, env = voice_loop.validate_action({"tool": "tool_look", "params": {}})
    assert tool == "tool_look"
