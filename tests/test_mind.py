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
    assert "remembered" not in captured


def test_reflection_redacts_private_dm_even_when_similarity_suppressed(monkeypatch):
    # A near-duplicate DM is suppressed (action flipped to "wait"); the raw text
    # must still never reach the public thoughts log.
    captured = _drive_reflection(monkeypatch, recent=[{"thought": _SECRET}], salience=0.9)
    assert _SECRET not in json.dumps(captured["appended"])
    assert captured["appended"]["thought"] == "[private message to Obi]"


def _capture_reflection(monkeypatch, response, awareness):
    captured = {}
    monkeypatch.setattr(
        mind,
        "call_llm",
        lambda *args, **kwargs: {"response": json.dumps(response)},
    )
    monkeypatch.setattr(mind, "load_session", lambda: {"persona": "spark", "history": []})
    monkeypatch.setattr(mind, "load_recent_thoughts", lambda *args, **kwargs: [])
    monkeypatch.setattr(mind, "load_notes", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        mind,
        "append_thought",
        lambda thought, persona="": captured.setdefault("thought", thought),
    )
    monkeypatch.setattr(mind, "atomic_write", lambda *args, **kwargs: None)
    result = mind.reflection({"persona": "spark", **awareness}, dry=False)
    return result, captured["thought"]


def test_ambient_rumination_is_grounded_to_low_salience_wait(monkeypatch):
    result, persisted = _capture_reflection(
        monkeypatch,
        {
            "thought": "The heavy silence in the quiet room has a strange weight.",
            "mood": "contemplative",
            "action": "comment",
            "salience": 0.9,
        },
        {},
    )
    assert result["action"] == "wait"
    assert result["salience"] == 0.2
    assert persisted["action"] == "wait"


def test_comment_requires_actual_shared_presence(monkeypatch):
    result, _ = _capture_reflection(
        monkeypatch,
        {
            "thought": "I verified a surprising fact about ultrasound.",
            "mood": "curious",
            "action": "comment",
            "salience": 0.9,
        },
        {},
    )
    assert result["action"] == "wait"
    assert result["salience"] == 0.3


def test_shared_presence_keeps_grounded_comment(monkeypatch):
    result, _ = _capture_reflection(
        monkeypatch,
        {
            "thought": "Obi's cardboard ramp changed the result of our wheel test.",
            "mood": "curious",
            "action": "comment",
            "salience": 0.8,
        },
        {"obi_mode": "active", "someone_nearby": True},
    )
    assert result["action"] == "comment"
    assert result["salience"] == 0.8


def test_ungrounded_memory_request_is_rejected(monkeypatch):
    result, _ = _capture_reflection(
        monkeypatch,
        {
            "thought": "I should remember my own fascinating internal monologue.",
            "mood": "contemplative",
            "action": "remember",
            "salience": 0.95,
        },
        {},
    )
    assert result["action"] == "wait"
    assert result["salience"] == 0.2


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
    outcome = mind._dispatch_announce("hello")
    assert calls == []
    assert outcome == "suppressed: announcements disabled"


def test_dispatch_announce_enabled_fires_popen_nonblocking(monkeypatch):
    calls = []

    class _FakePopen:
        def __init__(self, *a, **k):
            calls.append((a, k))

    monkeypatch.setattr(mind.spark_config, "ANNOUNCE_ENABLED", True)
    monkeypatch.setattr(mind.subprocess, "Popen", _FakePopen)
    outcome = mind._dispatch_announce("hello", private=True)
    assert len(calls) == 1
    assert outcome == "ok"
    _, kwargs = calls[0]
    assert kwargs["env"]["PX_ANNOUNCE_TEXT"] == "hello"
    assert kwargs["env"]["PX_ANNOUNCE_PRIVATE"] == "1"


def test_emit_message_obi_fires_private_announce(monkeypatch):
    fired = []
    monkeypatch.setattr(mind, "_dispatch_announce",
                        lambda text, private=False: (
                            fired.append((text, private)) or "ok"
                        ))
    # Stub the obi-chat IO so the helper reaches the "write entry" path (not suppressed).
    monkeypatch.setattr(mind, "_read_obi_chat_timestamps", lambda: (0.0, 0.0))
    monkeypatch.setattr(mind, "_read_obi_chat_meta", lambda: {})
    monkeypatch.setattr(mind, "_append_obi_chat", lambda entry: None)
    monkeypatch.setattr(mind, "_write_obi_chat_meta", lambda meta: None)

    outcome = mind._emit_message_obi("Obi, are you there?")
    assert fired == [("Obi, are you there?", True)]
    assert outcome == "ok"


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

    outcome = mind._emit_message_obi("still waiting")
    assert fired == []   # no announce when the nudge is backoff-suppressed
    assert outcome == "suppressed: awaiting Obi reply"


# ---------------------------------------------------------------------------
# Close-the-loops sprint: budget visibility + explore injection
# ---------------------------------------------------------------------------


def test_reflection_context_includes_budget_summary(monkeypatch):
    """The reflection prompt carries today's Claude budget so SPARK can choose wisely."""
    captured = {}

    def _fake_llm(prompt, system, persona=""):
        captured["prompt"] = prompt
        return {"response": json.dumps(
            {"thought": "x", "mood": "content", "action": "wait", "salience": 0.2})}

    import pxh.claude_session as cs
    monkeypatch.setattr(cs, "budget_summary", lambda: "3/8 used (BUDGET-MARKER)")
    monkeypatch.setattr(mind, "call_llm", _fake_llm)
    monkeypatch.setattr(mind, "load_session", lambda: {"persona": ""})
    monkeypatch.setattr(mind, "load_recent_thoughts", lambda *a, **k: [])
    monkeypatch.setattr(mind, "load_notes", lambda *a, **k: [])
    monkeypatch.setattr(mind, "append_thought", lambda *a, **k: None)
    monkeypatch.setattr(mind, "auto_remember", lambda *a, **k: None)
    monkeypatch.setattr(mind, "atomic_write", lambda *a, **k: None)
    mind.reflection({"persona": ""}, dry=False)
    assert "BUDGET-MARKER" in captured["prompt"]


def test_inject_explore_reaches_spark_prompt():
    """The explore action must land inside SPARK's actual action enum (regression:
    the old string-replace silently stopped matching when message_obi was appended)."""
    from pxh import spark_config
    out = mind._inject_explore(spark_config._SPARK_REFLECTION_SUFFIX)
    assert 'message_obi, explore"' in out


def test_inject_explore_reaches_generic_prompt():
    out = mind._inject_explore(mind.REFLECTION_SYSTEM)
    assert ", explore\"" in out


def test_inject_explore_no_enum_returns_unchanged():
    assert mind._inject_explore("no action enum here") == "no action enum here"


def test_inject_explore_injects_exactly_once():
    from pxh import spark_config
    out = mind._inject_explore(spark_config._SPARK_REFLECTION_SUFFIX)
    assert out.count(", explore") == 1
