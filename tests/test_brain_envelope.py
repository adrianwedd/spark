"""The resident sessions' tool envelopes, pinned exactly.

The envelope is fixed at session launch and cannot be widened per request, so
it is the one place where a privilege decision is made once and inherited by
every kind. That makes accidental drift expensive and silent — hence an exact
assertion rather than a membership check: adding a tool must be a deliberate
edit against a failing test.

`Read` was added on 2026-08-19 so `describe_scene` could look at a photo with
Claude Code's own capability instead of a shim pretending to be an image model.
It materially broadens spark-brain: the session's cwd is the repository root,
so Read reaches `.env`, `state/` and `~/.claude` credentials. Before it, the
session could only *write*, through four named tools, and could read nothing.
The narrowing to photos/ is enforced in pxh.vision._within_photos, not by the
CLI rule — see the envelope comment in bin/px-claude-session for why.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = REPO_ROOT / "bin" / "px-claude-session"


def _default_tools() -> list[str]:
    body = LAUNCHER.read_text()
    block = re.search(r"DEFAULT_TOOLS=\((.*?)\n\)", body, re.S)
    assert block, "DEFAULT_TOOLS array not found in bin/px-claude-session"
    return re.findall(r'"([^"]+)"', block.group(1))


def test_spark_brain_envelope_is_exactly_this():
    """Five entries. Any sixth is a privilege change and must be argued for."""
    assert _default_tools() == [
        "Bash($TOOL_BRAIN_REPLY:*)",
        "Bash($PROJECT_ROOT/bin/tool-voice:*)",
        "Bash($PROJECT_ROOT/bin/tool-remember:*)",
        "Bash($PROJECT_ROOT/bin/tool-look:*)",
        "Read",
    ]


def test_read_is_the_only_non_bash_capability():
    """Write, Edit and shell access would each be a different conversation."""
    non_bash = [t for t in _default_tools() if not t.startswith("Bash(")]
    assert non_bash == ["Read"]


def test_the_io_session_gets_no_read():
    """spark-io chews text from strangers. The trust boundary is unchanged.

    Its envelope comes from PX_CLAUDE_ALLOWED_TOOLS, set by the supervisor, not
    from DEFAULT_TOOLS — this pins that the launcher still honours that split.
    """
    body = LAUNCHER.read_text()
    assert "PX_CLAUDE_ALLOWED_TOOLS" in body
    assert 'if [[ -n "${PX_CLAUDE_ALLOWED_TOOLS:-}" ]]; then' in body


def test_the_scope_narrowing_lives_in_code_not_the_cli_rule():
    """Read is granted unscoped, so something else must do the narrowing.

    If this ever becomes a scoped `Read(...)` rule, delete this test — but only
    after confirming a non-matching rule fails closed rather than raising a
    permission dialog into an unattended pane.
    """
    from pxh import vision
    assert hasattr(vision, "_within_photos")


# ── The narrowing itself ───────────────────────────────────────────────────

def test_a_path_outside_photos_never_reaches_the_session(monkeypatch, tmp_path):
    from pxh import vision

    def _never(*a, **k):
        raise AssertionError("a non-photo path was sent to the brain")

    import pxh.brain
    monkeypatch.setattr(pxh.brain, "ask_brain", _never)

    secret = tmp_path / ".env"
    secret.write_text("PX_API_TOKEN=hunter2")
    assert vision.describe_image(secret) == vision.FALLBACK_DESCRIPTION


def test_traversal_out_of_photos_is_refused():
    from pxh import vision
    root = vision._project_root()
    assert not vision._within_photos(root / "photos" / ".." / ".env")


def test_a_real_photo_path_is_accepted():
    from pxh import vision
    assert vision._within_photos(vision._project_root() / "photos" / "x.jpg")
