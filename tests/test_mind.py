"""Tests for night-silence helper and announce action in pxh.mind."""
import json
import subprocess
from pxh import mind


_SECRET = "SECRET-DM-PAYLOAD-XYZ"


def _drive_reflection(monkeypatch, *, recent, salience):
    """Run reflection() with a stubbed LLM emitting a message_obi thought.

    Returns dict capturing what append_thought / auto_remember received.
    """
    captured = {}
    monkeypatch.setattr(mind, "call_llm", lambda *a, **k: {"response": json.dumps(
        {"thought": _SECRET, "mood": "content", "action": "message_obi", "salience": salience})})
    monkeypatch.setattr(mind, "load_session", lambda: {"persona": ""})
    monkeypatch.setattr(mind, "load_recent_thoughts", lambda *a, **k: recent)
    monkeypatch.setattr(mind, "load_notes", lambda *a, **k: [])
    monkeypatch.setattr(mind, "append_thought", lambda t, persona="": captured.__setitem__("appended", t))
    monkeypatch.setattr(mind, "auto_remember", lambda t, persona="": captured.__setitem__("remembered", t))
    monkeypatch.setattr(mind, "atomic_write", lambda *a, **k: None)
    mind.reflection({"persona": ""}, dry=False)
    return captured


def test_reflection_redacts_private_dm_when_persisted(monkeypatch):
    captured = _drive_reflection(monkeypatch, recent=[], salience=0.9)
    assert captured["appended"]["thought"] == "[private message to Obi]"
    assert _SECRET not in json.dumps(captured["appended"])
    # high-salience -> auto-remembered, but redacted there too
    assert _SECRET not in json.dumps(captured["remembered"])


def test_reflection_redacts_private_dm_even_when_similarity_suppressed(monkeypatch):
    # A near-duplicate DM is suppressed (action flipped to "wait"); the raw text
    # must still never reach the public thoughts log.
    captured = _drive_reflection(monkeypatch, recent=[{"thought": _SECRET}], salience=0.9)
    assert _SECRET not in json.dumps(captured["appended"])
    assert captured["appended"]["thought"] == "[private message to Obi]"


def test_is_night_silence_uses_config_bounds():
    assert mind._is_night_silence(19) is True
    assert mind._is_night_silence(23) is True
    assert mind._is_night_silence(6) is True
    assert mind._is_night_silence(7) is False
    assert mind._is_night_silence(12) is False
    assert mind._is_night_silence(18) is False


def test_announce_in_valid_actions():
    assert "announce" in mind.VALID_ACTIONS


def test_dispatch_announce_disabled_is_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(mind.spark_config, "ANNOUNCE_ENABLED", False)
    monkeypatch.setattr(mind.subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    mind._dispatch_announce("hello")
    assert calls == []


def test_dispatch_announce_enabled_fires_popen_nonblocking(monkeypatch):
    calls = []

    class _FakePopen:
        def __init__(self, *a, **k):
            calls.append((a, k))

    monkeypatch.setattr(mind.spark_config, "ANNOUNCE_ENABLED", True)
    monkeypatch.setattr(mind.subprocess, "Popen", _FakePopen)
    mind._dispatch_announce("hello", private=True)
    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["env"]["PX_ANNOUNCE_TEXT"] == "hello"
    assert kwargs["env"]["PX_ANNOUNCE_PRIVATE"] == "1"


def test_emit_message_obi_fires_private_announce(monkeypatch):
    fired = []
    monkeypatch.setattr(mind, "_dispatch_announce",
                        lambda text, private=False: fired.append((text, private)))
    # Stub the obi-chat IO so the helper reaches the "write entry" path (not suppressed).
    monkeypatch.setattr(mind, "_read_obi_chat_timestamps", lambda: (0.0, 0.0))
    monkeypatch.setattr(mind, "_read_obi_chat_meta", lambda: {})
    monkeypatch.setattr(mind, "_append_obi_chat", lambda entry: None)
    monkeypatch.setattr(mind, "_write_obi_chat_meta", lambda meta: None)

    mind._emit_message_obi("Obi, are you there?")
    assert fired == [("Obi, are you there?", True)]


def test_emit_message_obi_suppressed_no_announce(monkeypatch):
    fired = []
    monkeypatch.setattr(mind, "_dispatch_announce",
                        lambda text, private=False: fired.append((text, private)))
    # last_spark_ts > last_obi_ts and recent -> awaiting reply within backoff -> suppressed.
    import time as _t
    now = _t.time()
    monkeypatch.setattr(mind, "_read_obi_chat_timestamps", lambda: (now, 0.0))
    monkeypatch.setattr(mind, "_read_obi_chat_meta", lambda: {"backoff_s": 9999})
    monkeypatch.setattr(mind, "_append_obi_chat", lambda entry: None)
    monkeypatch.setattr(mind, "_write_obi_chat_meta", lambda meta: None)

    mind._emit_message_obi("still waiting")
    assert fired == []   # no announce when the nudge is backoff-suppressed
