"""Ask the cognition tier to describe a photograph.

Extracted from `bin/tool-describe-scene` so the failure modes below can be
tested as behaviour rather than asserted against the script's source text. They
are silent by construction: the tool returns a plausible sentence either way,
and `wander.py` stamps that sentence into durable memory as an `observation` at
confidence 1.0.

1.  **An error is not a description.** A model that fails must not have its
    failure text spoken aloud and remembered as something SPARK saw.
2.  **Scope is enforced here, not asked for.** Only a file under `photos/`
    is ever sent. This used to bound what a resident session was *told to
    read*; now it bounds what leaves the robot at all, which is the stronger
    version of the same rule.

**One deliberate inversion in #317 Phase 3.** This used to send the *path* and
let Claude Code's `Read` tool open the file, with a comment explaining that
inlining the bytes "would put the photo through the mailbox, the log and any
future outbox dump". That reasoning is exactly why the bytes travel now: the
mailbox is what is being retired. The image goes in the one request, to the one
model that can see it, and nothing about it is written down here.

The privilege-drop tests that used to sit alongside these are long gone with
the thing they guarded: there is no `claude -p` to launch as the wrong user and
no credential store to reach for in the wrong home.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

# The kind this module serves. Named here rather than at the call site so the
# tier's meter, the session log and this file agree on one spelling.
VISION_KIND = "describe_scene"

FALLBACK_DESCRIPTION = "I couldn't see anything right now."

# Bounded because this runs inside wander's DESCRIBE_SCENE_TIMEOUT budget.
# Pinned by tests/test_wander.py::test_describe_scene_timeout_has_margin_over_describe_scene.
# 60, not 45: measured 2026-08-17, a cold `runuser -u pi -- claude` timed out at
# exactly 45s while the warm call took 22s. 45 was killing the first vision call
# of every boot. Raising this consumes wander's outer budget — raise
# wander.DESCRIBE_SCENE_TIMEOUT with it or the test above will say so.
#
# Renamed from CLAUDE_TIMEOUT in #317 Phase 3: the call no longer reaches
# Claude, and a constant whose name is a provider outlives the provider.
DESCRIBE_TIMEOUT_S = 60

MAX_DESCRIPTION_CHARS = 300

# Photo bytes are inlined into the request, so this is the one bound that keeps
# a description-sized call from becoming a bandwidth-sized one. Pi-camera JPEGs
# run 45-90 KB; the cap is generous for those and refuses anything else rather
# than sending it and hoping. `wander.py` captures the photo, so a caller that
# ever produces something larger is a bug worth seeing fail closed.
MAX_IMAGE_BYTES = 2_000_000

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


def _prompt_for() -> str:
    """No path in here — deliberately.

    The old prompt said "Read the image at <path>", which was an instruction a
    tool-bearing session could follow. The model holding an inlined image
    cannot read a path, and telling it to try is an invitation to describe a
    file it never saw. The image is simply "this photo".
    """
    return (
        "Describe what you see in this photo in 2 short, fun sentences that a "
        "7-year-old would enjoy. Use simple words. Focus on interesting things "
        "like colours, shapes, animals, or cool objects. Be enthusiastic but brief."
    )


def describe_image(image_path: Path | str) -> str:
    """Describe the image, or return FALLBACK_DESCRIPTION.

    One request to the cognition tier, with the photo inlined. No session, no
    mailbox, no request id, and nothing written anywhere but the model's answer.

    The scope check is the same function it always was and now guards more:
    `_within_photos` used to bound what a resident session was told to read,
    and now bounds what leaves the robot.

    Never raises and never returns the tier's own error text: the caller speaks
    this string aloud and writes it to memory as an observation.
    """
    path = Path(image_path)
    if not _within_photos(path) or not path.exists():
        return FALLBACK_DESCRIPTION

    try:
        raw = path.read_bytes()
    except OSError:
        return FALLBACK_DESCRIPTION
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        return FALLBACK_DESCRIPTION

    try:
        from pxh.m5 import ask_m5
        result = ask_m5(VISION_KIND, _prompt_for(), "",
                        timeout_s=float(DESCRIBE_TIMEOUT_S),
                        images=[base64.b64encode(raw).decode("ascii")])
    except Exception:  # noqa: BLE001 - the caller speaks whatever comes back
        return FALLBACK_DESCRIPTION

    if result.status != "available":
        # Defer, never escalate. A robot saying "I couldn't see anything right
        # now" is telling the truth; one that reaches for a second backend to
        # avoid saying it makes the Pi slower for everything else.
        return FALLBACK_DESCRIPTION

    text = _description_from(result.response)
    if not text:
        return FALLBACK_DESCRIPTION
    return text[:MAX_DESCRIPTION_CHARS]


def _description_from(raw: str) -> str:
    """The model's answer, whether it wrapped it in JSON or not.

    The prompt asks for prose. The resident path asked for
    `{"description": ...}` because the answer had to survive the mailbox; a
    direct call has no such need, so both shapes are accepted and neither is
    required — refusing good prose for want of a wrapper would be a silent
    regression in what SPARK can see.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except ValueError:
            return text
        if isinstance(obj, dict):
            for key in ("description", "text", "reply"):
                value = obj.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return text
