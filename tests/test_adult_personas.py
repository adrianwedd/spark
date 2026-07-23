from pathlib import Path

from pxh.adult_personas import wake_personas


ROOT = Path(__file__).resolve().parent.parent


def test_adult_wake_configuration_is_complete():
    personas = wake_personas(ROOT)
    assert set(personas) == {"vixen", "gremlin"}
    for config in personas.values():
        assert Path(config["prompt"]).is_file()
        assert Path(config["chat_tool"]).is_file()
        assert set(config["voice_env"]) == {
            "PX_VOICE_VARIANT",
            "PX_VOICE_PITCH",
            "PX_VOICE_RATE",
        }


def test_child_listener_does_not_embed_adult_configuration():
    listener = (ROOT / "bin" / "px-wake-listen").read_text(encoding="utf-8")
    assert "persona-vixen.md" not in listener
    assert "persona-gremlin.md" not in listener
    assert "tool-chat-vixen" not in listener
    assert "if adult_personas_enabled()" in listener
