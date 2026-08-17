"""Ask Claude to describe a photograph.

Extracted from `bin/tool-describe-scene` so the two failure modes below can be
tested as behaviour rather than asserted against the script's source text. Both
are silent: the tool returns a plausible sentence either way, and `wander.py`
stamps that sentence into durable memory as an `observation` at confidence 1.0.

1.  **A CLI error is not a description.** `claude` prints "Not logged in -
    Please run /login" to *stdout* and exits 1, so a guard that only checked
    for empty stdout would hand the error text back as what SPARK saw.
2.  **Root has no credentials.** Anything downstream of `bin/tool-wander`'s
    `sudo` runs as root, whose credential store is empty — which is failure 1's
    trigger, and why every voice-triggered and autonomous wander has been blind
    since March. The CLI is therefore dropped back to the owning user.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

FALLBACK_DESCRIPTION = "I couldn't see anything right now."

# Bounded because this runs inside wander's DESCRIBE_SCENE_TIMEOUT budget.
# Pinned by tests/test_wander.py::test_describe_scene_timeout_has_margin_over_claude.
# 60, not 45: measured 2026-08-17, a cold `runuser -u pi -- claude` timed out at
# exactly 45s while the warm call took 22s. 45 was killing the first vision call
# of every boot. Raising this consumes wander's outer budget — raise
# wander.DESCRIBE_SCENE_TIMEOUT with it or the test above will say so.
CLAUDE_TIMEOUT = 60

MAX_DESCRIPTION_CHARS = 300

# Who owns the Claude credentials. Not root, and not necessarily the caller.
CLAUDE_USER = os.environ.get("PX_CLAUDE_USER", "pi")


def claude_bin() -> str:
    """Locate the CLI. Not on PATH under systemd, nor on root's PATH at all."""
    return (os.environ.get("PX_CLAUDE_BIN")
            or shutil.which("claude")
            or f"/home/{CLAUDE_USER}/.local/bin/claude")


def vision_command(prompt: str) -> list[str]:
    """The argv for one vision call, dropping privileges if we hold them.

    `runuser` rather than a `HOME=/home/pi` override on the existing sudo env:
    the override does authenticate, but it runs the CLI *as root inside the pi
    user's home*, leaving root-owned files in ~/.claude on every call. Once a
    token refresh lands there root-owned, the pi user can no longer write its
    own credentials — the same cross-UID trap health.py was redesigned around.
    Dropping privileges keeps the blast radius to this one subprocess.
    """
    argv = [
        claude_bin(), "-p", prompt,
        "--allowedTools", "Read",
        "--output-format", "text",
    ]
    user = os.environ.get("PX_CLAUDE_USER", CLAUDE_USER)
    if os.geteuid() == 0:
        return ["runuser", "-u", user, "--", *argv]
    return argv


def _prompt_for(image_path: Path | str) -> str:
    return (
        f"Read the image at {image_path} and describe what you see "
        f"in 2 short, fun sentences that a 7-year-old would enjoy. "
        f"Use simple words. Focus on interesting things like colours, "
        f"shapes, animals, or cool objects. Be enthusiastic but brief."
    )


def describe_image(image_path: Path | str) -> str:
    """Describe the image, or return FALLBACK_DESCRIPTION.

    Never raises and never returns the CLI's own error output: the caller
    speaks this string aloud and writes it to memory.
    """
    # Run as plain `claude`, not in Claude Code mode, when called from a session.
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)

    try:
        result = subprocess.run(
            vision_command(_prompt_for(image_path)),
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=CLAUDE_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError, subprocess.SubprocessError):
        return FALLBACK_DESCRIPTION

    # Both halves matter. Exit code alone misses a clean-but-silent run; stdout
    # alone turns "Not logged in" into something SPARK claims to have seen.
    if result.returncode != 0 or not result.stdout.strip():
        return FALLBACK_DESCRIPTION

    return result.stdout.strip()[:MAX_DESCRIPTION_CHARS]
