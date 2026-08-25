"""message_obi redaction is a property of the RECORD, not of one call site.

SPARK's `message_obi` action is a private DM to a child. The text must reach
exactly one place — the obi-chat delivery path — and no other. Before this file
existed the redaction was a *local variable* in `reflection()` used only for the
thoughts-file write, so `expression()` re-read the raw `thought["thought"]` and
wrote it into session history, from which it reached the voice-loop prompt
(GREMLIN/VIXEN included), the awareness conversation digest, the reflection
prompt, and `GET /api/v1/session`.

Every test here asserts against a sentinel string. If the sentinel appears in a
sink, that sink is leaking — there is nothing to interpret.

All tests are inert: no services, no live state, no real subprocesses. The
autouse fixtures in conftest.py isolate LOG_DIR, PX_SESSION_PATH, health and
the brain mailbox; this file additionally pins the clock to midday Hobart so
the 19:00–07:00 night gate cannot silently turn a leak test into a no-op.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pxh import mind, voice_loop


SENTINEL = "SENTINEL-PRIVATE-DM-9f3c1a-DO-NOT-LEAK"
PLACEHOLDER = mind.PRIVATE_DM_PLACEHOLDER

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def midday(tmp_path):
    """Stub awareness/battery/log files and pin the clock to 12:00 Hobart.

    expression() drops every action during night silence, so without a pinned
    clock this whole file would pass vacuously after 19:00 on the robot — the
    exact class of "green because nothing ran" failure CLAUDE.md warns about
    for the night-silence seam.
    """
    old_aw, old_bat, old_log = mind.AWARENESS_FILE, mind.BATTERY_FILE, mind.LOG_FILE
    aw = tmp_path / "awareness.json"
    bat = tmp_path / "battery.json"
    aw.write_text(json.dumps({"obi_mode": "calm"}), encoding="utf-8")
    bat.write_text(json.dumps({"pct": 80, "charging": False}), encoding="utf-8")
    mind.AWARENESS_FILE = aw
    mind.BATTERY_FILE = bat
    mind.LOG_FILE = tmp_path / "px-mind.log"

    noon = _dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=mind.HOBART_TZ)
    with patch("pxh.mind.dt") as mock_dt:
        mock_dt.datetime.now.return_value = noon
        mock_dt.datetime.fromisoformat = _dt.datetime.fromisoformat
        mock_dt.timezone = _dt.timezone
        mock_dt.timedelta = _dt.timedelta
        yield
    mind.AWARENESS_FILE, mind.BATTERY_FILE, mind.LOG_FILE = old_aw, old_bat, old_log


def _drive_reflection(monkeypatch, *, recent=(), salience=0.9, text=SENTINEL):
    """Run reflection() against a stubbed LLM emitting a message_obi thought.

    Returns (returned_thought, captured) where captured holds whatever the
    persistence helpers received.
    """
    captured: dict = {}
    monkeypatch.setattr(mind, "call_llm", lambda *a, **k: {"response": json.dumps(
        {"thought": text, "mood": "content", "action": "message_obi",
         "salience": salience})})
    monkeypatch.setattr(mind, "load_session", lambda: {"persona": ""})
    monkeypatch.setattr(mind, "load_recent_thoughts", lambda *a, **k: list(recent))
    monkeypatch.setattr(mind, "load_notes", lambda *a, **k: [])
    monkeypatch.setattr(mind, "append_thought",
                        lambda t, persona="": captured.__setitem__("appended", t))
    monkeypatch.setattr(mind, "auto_remember",
                        lambda t, persona="": captured.__setitem__("remembered", t))
    monkeypatch.setattr(mind, "atomic_write", lambda *a, **k: None)
    returned = mind.reflection({"persona": ""}, dry=False)
    return returned, captured


def _dispatch_message_obi(thought: dict):
    """Run expression() on a message_obi thought with delivery stubbed out.

    Returns (delivered_text_or_None, history_entry).
    """
    delivered: list = []
    with patch.object(mind, "_emit_message_obi", side_effect=delivered.append), \
         patch.object(mind, "update_session") as mock_us:
        mind.expression(thought, dry=True)
    entry = mock_us.call_args.kwargs["history_entry"] if mock_us.call_args else None
    return (delivered[0] if delivered else None), entry


# ---------------------------------------------------------------------------
# 1. The record itself
# ---------------------------------------------------------------------------


def test_reflection_returns_a_record_that_is_already_redacted(monkeypatch):
    """The thought handed to expression() must carry the placeholder, not the DM.

    This is the whole fix in one assertion: downstream code that reads
    `thought["thought"]` — which is most of it — is structurally safe rather
    than safe-if-it-remembers.
    """
    returned, _ = _drive_reflection(monkeypatch)
    assert returned is not None
    assert returned["thought"] == PLACEHOLDER
    assert returned.get("text") == ""
    # The raw text survives on exactly one field, for exactly one consumer.
    assert returned[mind.PRIVATE_DM_TEXT_KEY] == SENTINEL


def test_persisted_thought_record_never_contains_the_raw_dm(monkeypatch):
    """state/thoughts-{persona}.jsonl feeds /api/v1/public/thoughts, px-post
    (feed.json + Bluesky) and px-blog. Nothing raw may be written there —
    including the in-process delivery field."""
    _, captured = _drive_reflection(monkeypatch)
    blob = json.dumps(captured["appended"])
    assert SENTINEL not in blob
    assert mind.PRIVATE_DM_TEXT_KEY not in captured["appended"]
    assert captured["appended"]["thought"] == PLACEHOLDER


def test_auto_remembered_record_never_contains_the_raw_dm(monkeypatch):
    """High-salience thoughts are auto-remembered into notes/memories, which
    reflection later retrieves and injects back into the prompt."""
    _, captured = _drive_reflection(monkeypatch, salience=0.9)
    assert SENTINEL not in json.dumps(captured["remembered"])
    assert mind.PRIVATE_DM_TEXT_KEY not in captured["remembered"]


def test_redaction_survives_the_similarity_suppressor(monkeypatch):
    """A near-duplicate DM has its action flipped to "wait" by the anti-repetition
    check. Redaction happens before that flip, so the record cannot stop looking
    like a DM before it has been redacted."""
    _, captured = _drive_reflection(monkeypatch, recent=[{"thought": SENTINEL}])
    assert SENTINEL not in json.dumps(captured["appended"])
    assert captured["appended"]["thought"] == PLACEHOLDER


def test_repeated_dms_are_not_all_suppressed_as_duplicates(monkeypatch):
    """Regression guard on the fix itself: the anti-repetition check must still
    compare the *raw* DM text. Comparing placeholder-against-placeholder would
    score 1.0 and silently suppress every DM after the first."""
    returned, _ = _drive_reflection(
        monkeypatch, recent=[{"thought": PLACEHOLDER}], text="Obi, are you there?")
    assert returned["action"] == "message_obi"   # not flipped to "wait"


def test_redact_private_dm_is_idempotent():
    """expression() re-applies redaction defensively; a second pass must not
    overwrite the stashed raw text with the placeholder."""
    t = {"action": "message_obi", "thought": SENTINEL}
    mind.redact_private_dm(t)
    mind.redact_private_dm(t)
    assert t["thought"] == PLACEHOLDER
    assert t[mind.PRIVATE_DM_TEXT_KEY] == SENTINEL


def test_redaction_leaves_non_dm_thoughts_alone():
    t = {"action": "comment", "thought": "The afternoon is quiet."}
    mind.redact_private_dm(t)
    assert t["thought"] == "The afternoon is quiet."
    assert mind.PRIVATE_DM_TEXT_KEY not in t


def test_private_dm_text_never_returns_the_placeholder():
    """A record that somehow reached dispatch without its delivery field must
    send Obi nothing, not the string "[private message to Obi]"."""
    assert mind.private_dm_text({"action": "message_obi", "thought": PLACEHOLDER}) == ""


# ---------------------------------------------------------------------------
# 2. Delivery must still work
# ---------------------------------------------------------------------------


def test_delivery_path_still_carries_the_real_dm(midday, monkeypatch):
    """Redaction must not break the feature. Obi still receives the real words."""
    returned, _ = _drive_reflection(monkeypatch)
    delivered, _entry = _dispatch_message_obi(returned)
    assert delivered == SENTINEL


def test_emit_message_obi_writes_the_real_text_to_the_obi_mailbox(monkeypatch):
    """The obi-chat log is the delivery surface — authenticated, Obi-only — and
    is the one place the raw text is supposed to land."""
    written: list = []
    announced: list = []
    monkeypatch.setattr(mind, "_read_obi_chat_timestamps", lambda: (0.0, 0.0))
    monkeypatch.setattr(mind, "_read_obi_chat_meta", lambda: {})
    monkeypatch.setattr(mind, "_append_obi_chat", written.append)
    monkeypatch.setattr(mind, "_write_obi_chat_meta", lambda meta: None)
    monkeypatch.setattr(mind, "_dispatch_announce",
                        lambda text, private=False: announced.append((text, private)))

    mind._emit_message_obi(SENTINEL)
    assert written and written[0]["text"] == SENTINEL
    assert announced == [(SENTINEL, True)]


# ---------------------------------------------------------------------------
# 3. Sinks
# ---------------------------------------------------------------------------


def test_session_history_entry_has_no_raw_dm(midday, monkeypatch):
    """THE original leak. expression() writes a history entry read by the voice
    prompt, the awareness digest and GET /api/v1/session."""
    returned, _ = _drive_reflection(monkeypatch)
    _delivered, entry = _dispatch_message_obi(returned)
    assert entry is not None
    assert SENTINEL not in json.dumps(entry)
    assert entry["thought"] == PLACEHOLDER


def test_expression_redacts_a_record_it_did_not_build(midday):
    """The dispatch boundary must not depend on reflection() having redacted
    first — a replayed record, a future producer, or a test must all be safe,
    and delivery must still work for them."""
    raw = {"action": "message_obi", "thought": SENTINEL, "mood": "content",
           "salience": 0.9}
    delivered, entry = _dispatch_message_obi(raw)
    assert delivered == SENTINEL          # still delivered
    assert SENTINEL not in json.dumps(entry)
    assert raw["thought"] == PLACEHOLDER  # the caller's record is redacted in place


def test_awareness_conversation_digest_has_no_raw_dm(midday, monkeypatch):
    """awareness["recent_conversations"] is built from session history and IS in
    _REFLECTION_AWARENESS_KEYS, so it is injected straight into the reflection
    prompt (and served by GET /api/v1/awareness)."""
    returned, _ = _drive_reflection(monkeypatch)
    _delivered, entry = _dispatch_message_obi(returned)
    # Mirror mind.awareness_tick's digest extraction over the real entry.
    digest_text = entry.get("text", entry.get("thought", ""))[:150]
    assert SENTINEL not in digest_text
    assert "recent_conversations" in mind._REFLECTION_AWARENESS_KEYS  # still a sink


def test_reflection_prompt_has_no_raw_dm(midday, monkeypatch):
    """The next reflection reads session history as "Recent events". A leaked DM
    there would be laundered into a public thought by the very next tick."""
    returned, _ = _drive_reflection(monkeypatch)
    _delivered, entry = _dispatch_message_obi(returned)

    prompts: list = []

    def _capture(prompt, system, *a, **k):
        prompts.append(prompt)
        return {"response": json.dumps(
            {"thought": "quiet", "mood": "content", "action": "wait", "salience": 0.1})}

    monkeypatch.setattr(mind, "call_llm", _capture)
    monkeypatch.setattr(mind, "load_session",
                        lambda: {"persona": "", "history": [entry]})
    monkeypatch.setattr(mind, "load_recent_thoughts", lambda *a, **k: [])
    monkeypatch.setattr(mind, "load_notes", lambda *a, **k: [])
    monkeypatch.setattr(mind, "append_thought", lambda t, persona="": None)
    monkeypatch.setattr(mind, "auto_remember", lambda t, persona="": None)
    monkeypatch.setattr(mind, "atomic_write", lambda *a, **k: None)
    mind.reflection({"persona": "", "recent_conversations": [
        {"who": "robot", "text": entry.get("thought", ""), "minutes_ago": 1.0}]},
        dry=False)

    assert prompts, "reflection did not build a prompt"
    assert SENTINEL not in prompts[0]
    assert PLACEHOLDER in prompts[0]  # the entry really did reach the prompt


def test_voice_loop_prompt_has_no_raw_dm(midday, monkeypatch):
    """voice_loop.build_model_prompt injects the last 3 history entries. The
    persona swap replaces the system prompt wholesale, so GREMLIN/VIXEN see this
    context with none of SPARK's prose restraint around it."""
    returned, _ = _drive_reflection(monkeypatch)
    _delivered, entry = _dispatch_message_obi(returned)
    prompt = voice_loop.build_model_prompt(
        "system", {"persona": "gremlin", "history": [entry]}, "hello")
    assert SENTINEL not in prompt


def test_public_thoughts_endpoint_never_serves_the_raw_dm(monkeypatch, tmp_path):
    """The end of the public pipeline: /api/v1/public/thoughts feeds the site,
    the OG-rewrite worker and (via px-post) Bluesky."""
    from pxh import api

    _, captured = _drive_reflection(monkeypatch)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "thoughts-spark.jsonl").write_text(
        json.dumps({"ts": "2026-08-25T02:00:00Z", **captured["appended"]}) + "\n",
        encoding="utf-8")
    monkeypatch.setenv("PX_STATE_DIR", str(state_dir))

    body = asyncio.run(api.public_thoughts(limit=12))
    blob = json.dumps(body)
    assert SENTINEL not in blob
    assert PLACEHOLDER in blob  # the record really was served


def test_session_endpoint_never_serves_the_raw_dm(midday, monkeypatch, tmp_path):
    """GET /api/v1/session projects the last 10 history entries to the dashboard."""
    from pxh import api

    returned, _ = _drive_reflection(monkeypatch)
    _delivered, entry = _dispatch_message_obi(returned)

    monkeypatch.setattr(api, "load_session",
                        lambda: {"history": [entry], "mode": "idle"})
    body = asyncio.run(api.get_session())
    assert SENTINEL not in json.dumps(body)


def test_px_post_never_sees_the_raw_dm(monkeypatch):
    """px-post reads thoughts-spark.jsonl records verbatim into feed.json and the
    Bluesky post body. It is structurally safe because the record is redacted —
    pin that the record it would read carries no sentinel."""
    _, captured = _drive_reflection(monkeypatch)
    record = captured["appended"]
    # Fields px-post projects into feed.json / the skeet body.
    for field in ("thought", "mood", "salience", "action"):
        assert SENTINEL not in str(record.get(field, ""))


# ---------------------------------------------------------------------------
# 4. The one duplicated literal
# ---------------------------------------------------------------------------


def test_tool_announce_placeholder_matches_the_canonical_constant():
    """bin/tool-announce redacts its own session-history line and carries its own
    copy of the placeholder string (it must not import mind.py). If the two ever
    drift, one of the two surfaces stops matching what readers expect."""
    src = (ROOT / "bin" / "tool-announce").read_text(encoding="utf-8")
    assert f'"{PLACEHOLDER}"' in src, (
        "bin/tool-announce no longer spells the canonical placeholder "
        f"{PLACEHOLDER!r} — check its private-DM history redaction")
