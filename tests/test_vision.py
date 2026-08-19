"""Vision on the resident brain: the guard, and what may leave the robot.

Two failures live here, and both are silent by construction — the tool returns
a plausible sentence either way, and `wander.py` stamps that sentence into
durable memory as an `observation` at confidence 1.0. Neither can be caught by
reading the robot's logs after the fact, so they are pinned here.

The privilege-drop tests that used to sit alongside them are gone with the
thing they guarded. `describe_image` no longer runs `claude -p` under
`runuser`, so there is no process to launch as the wrong user and no
credentials to reach for in the wrong home. What replaced them is narrower and
sharper: the session already exists, and the only question is what we hand it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import pxh.brain
from pxh import vision


@pytest.fixture
def photo(tmp_path, monkeypatch):
    """A file that passes the photos/ check, without writing to the real dir."""
    photos = tmp_path / "photos"
    photos.mkdir()
    img = photos / "20260819T184444.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0 not really a jpeg")
    monkeypatch.setattr(vision, "_project_root", lambda: tmp_path)
    return img


def _reply(monkeypatch, value):
    captured = {}

    def _ask(kind, payload, **kw):
        captured["kind"] = kind
        captured["payload"] = payload
        captured["kw"] = kw
        return value

    monkeypatch.setattr(pxh.brain, "ask_brain", _ask)
    return captured


# ── The two silent failures ────────────────────────────────────────────────

def test_an_unavailable_brain_never_becomes_a_description(photo, monkeypatch):
    """`ask_brain` returns None for every failure. None is not a sighting.

    Before this, the equivalent path spawned a second Claude to try again. A
    robot that says "I couldn't see anything right now" is telling the truth;
    one that adds load to avoid saying it is lying and slower.
    """
    _reply(monkeypatch, None)
    assert vision.describe_image(photo) == vision.FALLBACK_DESCRIPTION


def test_an_empty_reply_is_also_a_failure(photo, monkeypatch):
    """A blank answer with no error is the shape that used to get spoken."""
    _reply(monkeypatch, {"reply": {"description": "   "}})
    assert vision.describe_image(photo) == vision.FALLBACK_DESCRIPTION


def test_a_raising_brain_does_not_propagate(photo, monkeypatch):
    """The caller speaks this string aloud; it must never see a traceback."""
    def _boom(*a, **k):
        raise RuntimeError("tmux socket gone")

    monkeypatch.setattr(pxh.brain, "ask_brain", _boom)
    assert vision.describe_image(photo) == vision.FALLBACK_DESCRIPTION


def test_a_real_description_is_returned_unchanged(photo, monkeypatch):
    _reply(monkeypatch, {"reply": {"description": "A red ball sits on a wooden table."}})
    assert vision.describe_image(photo) == "A red ball sits on a wooden table."


def test_a_long_description_is_truncated(photo, monkeypatch):
    _reply(monkeypatch, {"reply": {"description": "x" * 900}})
    assert len(vision.describe_image(photo)) == vision.MAX_DESCRIPTION_CHARS


# ── What is handed to the session ──────────────────────────────────────────

def test_only_the_path_travels(photo, monkeypatch):
    """The image is never inlined into the payload.

    It would work — the session could decode a base64 blob — but it would put
    the photo through the mailbox, the log and any future outbox dump, for no
    gain over a path the session can already read.
    """
    captured = _reply(monkeypatch, {"reply": {"description": "ok"}})
    vision.describe_image(photo)

    assert captured["kind"] == "describe_scene"
    assert captured["payload"]["image_path"] == str(photo.resolve())
    assert not any(isinstance(v, (bytes, bytearray))
                   for v in captured["payload"].values())


def test_the_session_is_told_to_read_only_that_path(photo, monkeypatch):
    """The Read grant is wider than this request. The prompt says so too."""
    captured = _reply(monkeypatch, {"reply": {"description": "ok"}})
    vision.describe_image(photo)
    assert "only" in captured["payload"]["respond_with"].lower()


def test_the_deadline_fits_inside_wanders_budget(photo, monkeypatch):
    """wander kills the tool at DESCRIBE_SCENE_TIMEOUT; overrunning that
    charges for a call whose answer is thrown away."""
    from pxh import wander
    captured = _reply(monkeypatch, {"reply": {"description": "ok"}})
    vision.describe_image(photo)
    assert captured["kw"]["timeout_s"] == float(vision.CLAUDE_TIMEOUT)
    assert vision.CLAUDE_TIMEOUT < wander.DESCRIBE_SCENE_TIMEOUT


# ── The scope narrowing ────────────────────────────────────────────────────

def test_a_path_outside_photos_is_refused_before_the_brain(tmp_path, monkeypatch):
    """Enforced here because the CLI grant is unscoped. See test_brain_envelope."""
    def _never(*a, **k):
        raise AssertionError("a non-photo path was sent to the brain")

    monkeypatch.setattr(pxh.brain, "ask_brain", _never)
    monkeypatch.setattr(vision, "_project_root", lambda: tmp_path)
    (tmp_path / "photos").mkdir()
    secret = tmp_path / ".env"
    secret.write_text("PX_API_TOKEN=hunter2")
    assert vision.describe_image(secret) == vision.FALLBACK_DESCRIPTION


def test_a_missing_photo_is_refused_before_the_brain(photo, monkeypatch):
    def _never(*a, **k):
        raise AssertionError("a missing path was sent to the brain")

    monkeypatch.setattr(pxh.brain, "ask_brain", _never)
    assert vision.describe_image(photo.parent / "nope.jpg") == vision.FALLBACK_DESCRIPTION


def test_no_subprocess_remains_in_the_module():
    """The cold path is gone, not disabled."""
    src = Path(vision.__file__).read_text()
    assert "subprocess" not in src.replace("There is no subprocess", "")
