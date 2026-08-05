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
        "text": "hi", "targets": ["media_player.nest_hub_max", "media_player.office_mini"]}})
    assert env["PX_ANNOUNCE_TARGETS"] == "media_player.nest_hub_max"


# ---------------------------------------------------------------------------
# execute_tool: PX_VOICE_NO_ROUTE is an execute_tool-level invariant (Task 6
# review fix) — every tool launched by the voice loop is interactive (human
# at the robot), so nothing it runs, directly or transitively, may route
# speech to a Nest. Covers both the weather-summary tool_voice call that
# bypasses validate_action (voice_loop.py ~1056) and secondary tools
# (tool-checkin, tool-timer, tool-story, ...) that shell to tool-voice on
# their own with os.environ.copy().
# ---------------------------------------------------------------------------

def test_execute_tool_always_sets_no_route(monkeypatch):
    import subprocess as _subprocess
    from pxh import voice_loop as _vl

    captured = {}

    def _fake_run(cmd, capture_output, text, check, env, timeout):
        captured["env"] = env
        return _subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    monkeypatch.setattr(_vl.subprocess, "run", _fake_run)
    monkeypatch.setattr(_vl, "load_session", lambda: {"persona": "spark"})
    monkeypatch.setattr(_vl, "_last_tool_execution", 0.0)

    _vl.execute_tool("tool_voice", {"PX_TEXT": "hi"}, dry_mode=True)
    assert captured["env"]["PX_VOICE_NO_ROUTE"] == "1"


def test_execute_tool_no_route_survives_persona_injection(monkeypatch):
    """A persona-active session (PERSONA_VOICE_ENV injection happens after
    the NO_ROUTE write) must not clobber PX_VOICE_NO_ROUTE back off."""
    import subprocess as _subprocess
    from pxh import voice_loop as _vl

    captured = {}

    def _fake_run(cmd, capture_output, text, check, env, timeout):
        captured["env"] = env
        return _subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    monkeypatch.setattr(_vl.subprocess, "run", _fake_run)
    monkeypatch.setattr(_vl, "load_session", lambda: {"persona": "gremlin"})
    monkeypatch.setattr(_vl, "_last_tool_execution", 0.0)

    _vl.execute_tool("tool_voice", {"PX_TEXT": "hi"}, dry_mode=True)
    assert captured["env"]["PX_VOICE_NO_ROUTE"] == "1"


# --- token accounting --------------------------------------------------------


def test_backend_label_names_each_launcher_tier(monkeypatch):
    """Every voice-loop call used to land in the `unknown` bucket, which mixes
    paid Claude with free Ollama and so cannot answer "what am I spending"."""
    from pxh.voice_loop import backend_label

    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    assert backend_label("/home/pi/picar-x-hacking/bin/claude-voice-bridge") == "claude"
    assert backend_label("codex exec --full-auto -") == "codex"
    assert backend_label("") == "unknown"


def test_backend_label_routes_codex_ollama_by_host(monkeypatch):
    """bin/codex-ollama matches both 'codex' and 'ollama'; the ollama branch
    must win, and OLLAMA_HOST decides local vs M5."""
    from pxh.voice_loop import backend_label

    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    assert backend_label("/home/pi/picar-x-hacking/bin/codex-ollama") == "ollama-local"

    monkeypatch.setenv("OLLAMA_HOST", "http://M5:11434")
    assert backend_label("/home/pi/picar-x-hacking/bin/codex-ollama") == "ollama-m5"


def test_backend_labels_match_the_tiers_mind_already_reports():
    """Both writers accumulate into the same by_backend keys, or the split is
    useless: mind.call_llm tags its calls with these exact strings."""
    from pxh.voice_loop import backend_label

    known = {"claude", "ollama-m5", "ollama-local", "ollama-cloud", "codex", "unknown"}
    for spec in ("claude-voice-bridge", "codex-ollama", "codex exec", ""):
        assert backend_label(spec) in known
