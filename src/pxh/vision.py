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

PHOTOS_DIRNAME = "photos"


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _within_photos(image_path: Path) -> bool:
    """Is this a photo SPARK took, rather than an arbitrary file?

    The resident session holds an unscoped `Read` (see bin/px-claude-session),
    so this is where the intended scope is actually enforced: a path outside
    photos/ is refused here and never reaches the session. The envelope grants
    more than this function will ever ask for, deliberately and visibly.
    """
    try:
        photos = (_project_root() / PHOTOS_DIRNAME).resolve()
        return image_path.resolve().is_relative_to(photos)
    except (OSError, ValueError):
        return False


def _prompt_for(image_path: Path | str) -> str:
    return (
        f"Read the image at {image_path} and describe what you see "
        f"in 2 short, fun sentences that a 7-year-old would enjoy. "
        f"Use simple words. Focus on interesting things like colours, "
        f"shapes, animals, or cool objects. Be enthusiastic but brief."
    )


def describe_image(image_path: Path | str) -> str:
    """Describe the image, or return FALLBACK_DESCRIPTION.

    Runs on the resident spark-brain session, which reads the file with Claude
    Code's own Read tool. There is no subprocess: this used to be a cold
    `claude -p --allowedTools Read`, launched under `runuser` because wander
    reaches it through sudo — a fresh Claude per photo, on a Pi that might be
    in the middle of a conversation.

    Only the path travels. The image is never inlined into the payload, and the
    path must live under photos/ — the session's Read grant is wider than that,
    so the narrowing is enforced here rather than assumed there.

    Never raises and never returns the session's own error text: the caller
    speaks this string aloud and writes it to memory.
    """
    path = Path(image_path)
    if not _within_photos(path) or not path.exists():
        return FALLBACK_DESCRIPTION

    try:
        from pxh.brain import ask_brain
        reply = ask_brain("describe_scene", {
            "image_path": str(path.resolve()),
            "instruction": _prompt_for(path.resolve()),
            "respond_with": (
                'a single JSON object {"description": "..."}. Read only the '
                "image at image_path. Do not speak, move or remember anything "
                "for this request — the caller says it aloud."
            ),
        }, timeout_s=float(CLAUDE_TIMEOUT))
    except Exception:  # noqa: BLE001 - the caller speaks whatever comes back
        return FALLBACK_DESCRIPTION

    if reply is None:
        # Defer, never escalate. A robot saying "I couldn't see anything right
        # now" is telling the truth; one that spawns a second Claude to avoid
        # saying it makes the Pi slower for everything else.
        return FALLBACK_DESCRIPTION

    answer = reply.get("reply")
    if isinstance(answer, dict):
        answer = answer.get("description") or answer.get("text") or ""
    text = str(answer or "").strip()
    if not text:
        return FALLBACK_DESCRIPTION
    return text[:MAX_DESCRIPTION_CHARS]
