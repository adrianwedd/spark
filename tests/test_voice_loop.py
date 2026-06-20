import pytest
from pxh.voice_loop import build_model_prompt, validate_action, VoiceLoopError


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


def test_validate_record_sound():
    assert "tool_record_sound" in ALLOWED_TOOLS
    tool, env = validate_action({"tool": "tool_record_sound",
                                 "params": {"name": "Obi Laugh", "seconds": 3}})
    assert tool == "tool_record_sound"
    assert env["PX_RECORD_NAME"] == "Obi Laugh"
    assert env["PX_RECORD_SECONDS"] == "3"


def test_validate_record_sound_clamps_seconds():
    _, env = validate_action({"tool": "tool_record_sound",
                              "params": {"name": "x", "seconds": 99}})
    assert env["PX_RECORD_SECONDS"] == "15"


def test_validate_play_sound_allows_recorded_name():
    _, env = validate_action({"tool": "tool_play_sound", "params": {"name": "obi-laugh"}})
    assert env["PX_SOUND"] == "obi-laugh"


def test_validate_play_sound_rejects_unsafe():
    for bad in ["../etc", "a/b", ""]:
        with pytest.raises(VoiceLoopError):
            validate_action({"tool": "tool_play_sound", "params": {"name": bad}})


def test_validate_record_sound_rejects_empty_name():
    with pytest.raises(VoiceLoopError):
        validate_action({"tool": "tool_record_sound", "params": {"name": "  ", "seconds": 5}})


def test_validate_record_sound_default_seconds():
    _, env = validate_action({"tool": "tool_record_sound", "params": {"name": "x"}})
    assert env["PX_RECORD_SECONDS"] == "5"


def test_validate_dopamine_add():
    tool, env = validate_action({"tool": "tool_dopamine_menu",
        "params": {"action": "add", "item": "magnetic tiles",
                   "energy": "high", "context": "free"}})
    assert tool == "tool_dopamine_menu"
    assert env["PX_DOPAMINE_ACTION"] == "add"
    assert env["PX_DOPAMINE_ITEM"] == "magnetic tiles"
    assert env["PX_DOPAMINE_ENERGY"] == "high"
    assert env["PX_DOPAMINE_CONTEXT"] == "free"


def test_validate_dopamine_add_requires_item():
    with pytest.raises(VoiceLoopError):
        validate_action({"tool": "tool_dopamine_menu",
                         "params": {"action": "add"}})


def test_validate_sleep():
    from pxh.voice_loop import validate_action, ALLOWED_TOOLS
    assert "tool_sleep" in ALLOWED_TOOLS
    tool, env = validate_action({"tool": "tool_sleep", "params": {"action": "start"}})
    assert tool == "tool_sleep"
    assert env["PX_SLEEP_ACTION"] == "start"


def test_validate_sleep_rejects_bad_action():
    with pytest.raises(VoiceLoopError):
        validate_action({"tool": "tool_sleep", "params": {"action": "bad"}})


def test_execute_tool_applies_session_voice(monkeypatch, tmp_path):
    import pxh.voice_loop as vl
    captured = {}
    monkeypatch.setattr(vl, "load_session", lambda: {
        "voice_variant": "en+m1", "voice_pitch": "60", "voice_rate": "120"})

    class _R:  returncode, stdout, stderr = 0, "{}", ""
    def _fake_run(cmd, **kw):
        captured.update(kw.get("env", {}))
        return _R()
    monkeypatch.setattr(vl.subprocess, "run", _fake_run)
    monkeypatch.setitem(vl.TOOL_COMMANDS, "tool_status", vl.BIN_DIR / "tool-status")
    vl.execute_tool("tool_status", {}, dry_mode=True)
    assert captured["PX_VOICE_VARIANT"] == "en+m1"
    assert captured["PX_VOICE_PITCH"] == "60"
    assert captured["PX_VOICE_RATE"] == "120"
