"""bin/px-wake-listen must load .env, same as px-mind/px-api-server/px-blog/
px-evolve.

Regression: px-wake-listen spawns voice_loop.py per turn with its own
inherited environment, and voice_loop dispatches tool-announce (and other
secret-consuming tools) from that same environment. Without .env loaded here,
every announce triggered from a real "Hey Spark" conversation ran with no
ANNOUNCE_RELAY_TOKEN/PX_HA_TOKEN and 401'd against the relay silently — the
token itself was never wrong, the process just never had it.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = REPO_ROOT / "bin" / "px-wake-listen"


def test_px_wake_listen_sources_dot_env_after_px_env():
    text = LAUNCHER.read_text(encoding="utf-8")
    px_env_idx = text.index('source "$SCRIPT_DIR/px-env"')
    dot_env_idx = text.index('ENV_FILE="$PROJECT_ROOT/.env"')
    assert dot_env_idx > px_env_idx, (
        "px-wake-listen must load .env after px-env sets PROJECT_ROOT, "
        "and before it hands its environment down to voice_loop.py"
    )
    exec_idx = text.index("exec \"$PROJECT_ROOT/.venv/bin/python3\"")
    assert dot_env_idx < exec_idx, ".env must be loaded before the listener starts"


def test_every_grant_refresh_goes_through_the_helper_that_survives_a_failed_write():
    """#323: `refresh_grant` raises when the window cannot be written.

    A refresh that returns False is a *policy decision* (wrong conversation,
    expired window, past the ceiling); a refresh that cannot be persisted is a
    different event — the window closes under a conversation that is still
    talking. The listener may not let that propagate: a process that dies to a
    full disk stops hearing the wake word entirely, which is worse than a
    window closing early. So every turn path goes through one helper that logs
    it, and this pins both halves: the call sites, and the catch.
    """
    text = LAUNCHER.read_text(encoding="utf-8")

    assert text.count("wake_grant.refresh_grant(") == 1, (
        "only _refresh_wake_grant may call refresh_grant directly — a second "
        "call site is a second place a failed write can escape uncaught"
    )
    assert "except wake_grant.GrantWriteFailed" in text
    # The four turn paths that extend the window: persona turn, persona
    # end-phrase turn, conversation turn, conversation end-phrase turn.
    assert text.count("_refresh_wake_grant(grant_id)") == 4, (
        "every turn path must extend the window for its own conversation"
    )
