"""Claude vision invocation: the guard, and the privilege drop.

Two failures live here, and both are silent by construction — the tool returns
a plausible sentence either way, and `wander.py` stamps that sentence into
durable memory as an `observation` at confidence 1.0. Neither can be caught by
reading the robot's logs after the fact, so they are pinned here instead.
"""
from __future__ import annotations

import shlex

import pytest

from pxh import vision


def _fake_claude(tmp_path, stdout: str, returncode: int = 0):
    """A real executable standing in for the claude CLI.

    Deliberately a script on disk rather than a mock: the behaviour under test
    is how `describe_image` reads a *process* result, so the process has to be
    real for the test to mean anything.
    """
    path = tmp_path / "fake-claude"
    path.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s' {shlex.quote(stdout)}\n"
        f"exit {returncode}\n"
    )
    path.chmod(0o755)
    return path


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "scene.jpg"
    path.write_bytes(b"\xff\xd8\xff\xd9")
    return path


def test_cli_error_text_never_becomes_a_description(tmp_path, monkeypatch, image):
    """A non-zero exit is a failure even when the CLI printed to stdout.

    This is the exact signature of the root-credential bug: `claude` prints
    "Not logged in - Please run /login" to **stdout** and exits 1. A guard that
    checked only for empty stdout would hand that string back, SPARK would say
    it aloud, and it would be written to memory as something the robot saw.
    """
    fake = _fake_claude(tmp_path, "Not logged in - Please run /login", returncode=1)
    monkeypatch.setenv("PX_CLAUDE_BIN", str(fake))

    result = vision.describe_image(image)

    assert result == vision.FALLBACK_DESCRIPTION
    assert "Not logged in" not in result


def test_empty_stdout_with_clean_exit_is_also_a_failure(tmp_path, monkeypatch, image):
    """The other half of the guard: exit 0 but nothing said."""
    fake = _fake_claude(tmp_path, "", returncode=0)
    monkeypatch.setenv("PX_CLAUDE_BIN", str(fake))

    assert vision.describe_image(image) == vision.FALLBACK_DESCRIPTION


def test_a_real_description_is_returned_unchanged(tmp_path, monkeypatch, image):
    fake = _fake_claude(tmp_path, "A red ball sits on a wooden table.\n")
    monkeypatch.setenv("PX_CLAUDE_BIN", str(fake))

    assert vision.describe_image(image) == "A red ball sits on a wooden table."


def test_as_pi_the_cli_is_invoked_directly(monkeypatch):
    monkeypatch.setattr(vision.os, "geteuid", lambda: 1000)
    monkeypatch.setenv("PX_CLAUDE_BIN", "/usr/bin/claude")

    assert vision.vision_command("prompt")[0] == "/usr/bin/claude"


def test_as_root_the_cli_is_dropped_back_to_the_owning_user(monkeypatch):
    """Under sudo the CLI must not run as root, or it reads root's empty
    credential store and silently returns the fallback.

    `runuser` rather than a HOME override: pointing root's HOME at /home/pi
    does authenticate, but it leaves root-owned files in the pi user's
    ~/.claude on every run — the same cross-UID trap health.py was redesigned
    around.
    """
    monkeypatch.setattr(vision.os, "geteuid", lambda: 0)
    monkeypatch.setenv("PX_CLAUDE_BIN", "/usr/bin/claude")

    cmd = vision.vision_command("prompt")

    assert cmd[:4] == ["runuser", "-u", "pi", "--"]
    assert cmd[4] == "/usr/bin/claude"


def test_the_drop_target_user_is_configurable(monkeypatch):
    monkeypatch.setattr(vision.os, "geteuid", lambda: 0)
    monkeypatch.setenv("PX_CLAUDE_USER", "spark")

    assert vision.vision_command("prompt")[:3] == ["runuser", "-u", "spark"]
