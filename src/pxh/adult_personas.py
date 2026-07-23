"""Adult-only wake-listener configuration.

The child-facing listener imports this module only when
PX_ENABLE_ADULT_PERSONAS is explicitly enabled.
"""

from pathlib import Path


def wake_personas(project_root: Path) -> dict[str, dict]:
    return {
        "vixen": {
            "prompt": str(project_root / "docs" / "prompts" / "persona-vixen.md"),
            "chat_tool": str(project_root / "bin" / "tool-chat-vixen"),
            "voice_env": {
                "PX_VOICE_VARIANT": "en+f4",
                "PX_VOICE_PITCH": "72",
                "PX_VOICE_RATE": "135",
            },
        },
        "gremlin": {
            "prompt": str(project_root / "docs" / "prompts" / "persona-gremlin.md"),
            "chat_tool": str(project_root / "bin" / "tool-chat"),
            "voice_env": {
                "PX_VOICE_VARIANT": "en+croak",
                "PX_VOICE_PITCH": "20",
                "PX_VOICE_RATE": "180",
            },
        },
    }
