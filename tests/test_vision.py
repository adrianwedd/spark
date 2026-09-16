"""Vision on the cognition tier: the guard, and what may leave the robot.

Two failures live here, and both are silent by construction — the tool returns
a plausible sentence either way, and `wander.py` stamps that sentence into
durable memory as an `observation` at confidence 1.0. Neither can be caught by
reading the robot's logs after the fact, so they are pinned here.

**What this file used to assert, and why it inverted (#317 Phase 3).** The
image used to travel as a *path*, and a resident Claude session opened it with
its own Read tool. The old test said: "the image is never inlined into the
payload — it would put the photo through the mailbox, the log and any future
outbox dump." That was correct about the mailbox and is the reason the bytes
travel now: the mailbox is what is being retired. What has not changed is the
scope rule, and it now guards something larger than before — `_within_photos`
used to bound what a session was *told to read*; it bounds what leaves the
robot at all.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import pxh.m5
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


def _tier(monkeypatch, status="available", response='{"description": "ok"}', error=""):
    captured = {}

    def _ask(kind, prompt, system, **kw):
        captured["kind"] = kind
        captured["prompt"] = prompt
        captured["system"] = system
        captured["kw"] = kw
        return pxh.m5.M5Result(status=status, response=response, error=error)

    monkeypatch.setattr(pxh.m5, "ask_m5", _ask)
    return captured


def _never(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("the tier was called for an image that must not travel")

    monkeypatch.setattr(pxh.m5, "ask_m5", _boom)


# ── The two silent failures ────────────────────────────────────────────────

def test_an_unavailable_tier_never_becomes_a_description(photo, monkeypatch):
    """`None` is not a description. The caller speaks this string aloud."""
    _tier(monkeypatch, status="timeout", response="", error="no answer")
    assert vision.describe_image(photo) == vision.FALLBACK_DESCRIPTION


def test_an_empty_reply_is_also_a_failure(photo, monkeypatch):
    _tier(monkeypatch, response="   ")
    assert vision.describe_image(photo) == vision.FALLBACK_DESCRIPTION


def test_a_raising_tier_does_not_propagate(photo, monkeypatch):
    """The caller is the voice loop and wander; neither may traceback."""
    def _boom(*a, **k):
        raise RuntimeError("socket gone")

    monkeypatch.setattr(pxh.m5, "ask_m5", _boom)
    assert vision.describe_image(photo) == vision.FALLBACK_DESCRIPTION


def test_a_real_description_is_returned_unchanged(photo, monkeypatch):
    _tier(monkeypatch, response='{"description": "A red ball sits on a wooden table."}')
    assert vision.describe_image(photo) == "A red ball sits on a wooden table."


def test_plain_prose_is_accepted_too(photo, monkeypatch):
    """The prompt asks for prose. The resident path needed a JSON wrapper
    because the answer had to survive the mailbox; a direct call does not, and
    refusing good prose for want of a wrapper would be a silent regression in
    what SPARK can see."""
    _tier(monkeypatch, response="A red ball sits on a wooden table.")
    assert vision.describe_image(photo) == "A red ball sits on a wooden table."


def test_a_long_description_is_truncated(photo, monkeypatch):
    _tier(monkeypatch, response="x" * (vision.MAX_DESCRIPTION_CHARS + 500))
    assert len(vision.describe_image(photo)) == vision.MAX_DESCRIPTION_CHARS


# ── What leaves the robot ──────────────────────────────────────────────────

def test_the_image_bytes_are_what_travel(photo, monkeypatch):
    """The inversion #317 Phase 3 makes deliberately, pinned so it is a
    decision on the record rather than a drift: the photo is inlined, because
    the mailbox that made a path the safer choice is being retired."""
    captured = _tier(monkeypatch)
    vision.describe_image(photo)

    assert captured["kind"] == vision.VISION_KIND
    images = captured["kw"]["images"]
    assert len(images) == 1
    import base64
    assert base64.b64decode(images[0]) == photo.read_bytes()
    assert str(photo.resolve()) not in captured["prompt"]


def test_the_prompt_never_names_a_path_the_model_cannot_read(photo, monkeypatch):
    """The old prompt said "Read the image at <path>" — an instruction only a
    tool-bearing session could follow. Told that with an inlined image, a model
    is being invited to describe a file it never saw."""
    captured = _tier(monkeypatch)
    vision.describe_image(photo)

    assert "read the image at" not in captured["prompt"].lower()
    assert "photos/" not in captured["prompt"]
    assert "7-year-old" in captured["prompt"]


def test_an_oversized_image_is_refused_before_the_tier(photo, monkeypatch):
    """The bytes are inlined, so this is the bound that keeps a
    description-sized request from becoming a bandwidth-sized one."""
    _never(monkeypatch)
    photo.write_bytes(b"x" * (vision.MAX_IMAGE_BYTES + 1))
    assert vision.describe_image(photo) == vision.FALLBACK_DESCRIPTION


def test_the_deadline_fits_inside_wanders_budget(photo, monkeypatch):
    """wander kills the tool at DESCRIBE_SCENE_TIMEOUT; overrunning that
    charges for a call whose answer is thrown away."""
    from pxh import wander
    captured = _tier(monkeypatch)
    vision.describe_image(photo)
    assert captured["kw"]["timeout_s"] == float(vision.DESCRIBE_TIMEOUT_S)
    assert vision.DESCRIBE_TIMEOUT_S < wander.DESCRIBE_SCENE_TIMEOUT


# ── The scope narrowing ────────────────────────────────────────────────────

def test_a_path_outside_photos_is_refused_before_the_tier(tmp_path, monkeypatch):
    """This is now an upload guard, not a read guard."""
    _never(monkeypatch)
    monkeypatch.setattr(vision, "_project_root", lambda: tmp_path)
    (tmp_path / "photos").mkdir()
    secret = tmp_path / ".env"
    secret.write_text("PX_API_TOKEN=hunter2")
    assert vision.describe_image(secret) == vision.FALLBACK_DESCRIPTION


def test_a_missing_photo_is_refused_before_the_tier(photo, monkeypatch):
    _never(monkeypatch)
    photo.unlink()
    assert vision.describe_image(photo) == vision.FALLBACK_DESCRIPTION
