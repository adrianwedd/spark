"""Tests for the rolling per-persona conversation buffer (issue #161).

SPARK should remember the last few turns of a conversation so it can coach
a routine without relying entirely on file-injected session state. The buffer
is per-persona (so GREMLIN/VIXEN/Spark histories never bleed into each other)
and trimmed to a fixed window.
"""

import pytest

from pxh import voice_loop


@pytest.fixture
def conv_state_dir(tmp_path, monkeypatch):
    """Redirect the conversation buffer to an isolated state dir in-process."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("PX_STATE_DIR", str(state_dir))
    return state_dir


def test_record_and_read_single_turn(conv_state_dir):
    voice_loop.record_conversation_turn("spark", "where are my shoes", "By the front door.")
    turns = voice_loop.recent_conversation("spark")
    assert turns == [{"user": "where are my shoes", "spark": "By the front door."}]


def test_buffer_trims_to_max_turns(conv_state_dir):
    for i in range(15):
        voice_loop.record_conversation_turn("spark", f"u{i}", f"s{i}", max_turns=10)
    turns = voice_loop.recent_conversation("spark")
    assert len(turns) == 10
    # oldest five dropped; newest kept in order
    assert turns[0]["user"] == "u5"
    assert turns[-1]["user"] == "u14"


def test_buffer_is_per_persona(conv_state_dir):
    voice_loop.record_conversation_turn("spark", "spark question", "spark answer")
    voice_loop.record_conversation_turn("gremlin", "gremlin question", "gremlin answer")
    spark_turns = voice_loop.recent_conversation("spark")
    gremlin_turns = voice_loop.recent_conversation("gremlin")
    assert spark_turns == [{"user": "spark question", "spark": "spark answer"}]
    assert gremlin_turns == [{"user": "gremlin question", "spark": "gremlin answer"}]


def test_recent_conversation_empty_when_no_history(conv_state_dir):
    assert voice_loop.recent_conversation("spark") == []


def test_build_prompt_includes_recent_conversation(conv_state_dir):
    voice_loop.record_conversation_turn("spark", "I like octopuses", "Three hearts, right?")
    state = {"persona": "spark", "history": []}
    prompt = voice_loop.build_model_prompt("SYS", state, "tell me more")
    assert "Recent conversation" in prompt
    assert "I like octopuses" in prompt
    assert "Three hearts, right?" in prompt


def test_build_prompt_omits_section_when_no_history(conv_state_dir):
    state = {"persona": "spark", "history": []}
    prompt = voice_loop.build_model_prompt("SYS", state, "hello")
    assert "Recent conversation" not in prompt


def test_spark_utterance_uses_params_text():
    action = {"tool": "tool_voice", "params": {"text": "Hi Obi, nice one."}}
    assert voice_loop.conversation_spark_text(action, "tool_voice") == "Hi Obi, nice one."


def test_spark_utterance_falls_back_to_tool_name_when_no_text():
    action = {"tool": "tool_forward", "params": {"speed": 30}}
    assert voice_loop.conversation_spark_text(action, "tool_forward") == "(tool_forward)"


def test_spark_utterance_handles_missing_params():
    assert voice_loop.conversation_spark_text({"tool": "tool_stop"}, "tool_stop") == "(tool_stop)"
