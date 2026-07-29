"""Tests for night-silence helper and announce action in pxh.mind."""
import inspect
import json
import subprocess
from unittest.mock import patch
from filelock import Timeout as FileLockTimeout
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
    assert 'complete_goal, explore"' in out


def test_inject_explore_reaches_generic_prompt():
    out = mind._inject_explore(mind.REFLECTION_SYSTEM)
    assert ", explore\"" in out


def test_inject_explore_no_enum_returns_unchanged():
    assert mind._inject_explore("no action enum here") == "no action enum here"


def test_inject_explore_injects_exactly_once():
    from pxh import spark_config
    out = mind._inject_explore(spark_config._SPARK_REFLECTION_SUFFIX)
    assert out.count(", explore") == 1


# ---------------------------------------------------------------------------
# Session lock contention — mind_loop must survive FileLockTimeout (#crash)
# ---------------------------------------------------------------------------


def test_awareness_tick_propagates_lock_timeout(monkeypatch):
    """awareness_tick() re-raises FileLockTimeout so mind_loop can skip the tick."""
    monkeypatch.setattr(mind, "load_session", lambda: (_ for _ in ()).throw(FileLockTimeout("fake.lock")))
    monkeypatch.setattr(mind, "read_sonar", lambda dry: None)
    monkeypatch.setattr(mind, "_fetch_frigate_presence", lambda dry: {})
    try:
        mind.awareness_tick({}, dry=True)
        raised = False
    except FileLockTimeout:
        raised = True
    assert raised, "awareness_tick should propagate FileLockTimeout to its caller"


def test_expression_survives_lock_timeout_on_load(monkeypatch, tmp_path):
    """expression() falls back to empty session when load_session raises FileLockTimeout."""
    import os
    monkeypatch.setattr(mind, "load_session", lambda: (_ for _ in ()).throw(FileLockTimeout("fake.lock")))
    monkeypatch.setattr(mind, "update_session", lambda **kw: None)
    monkeypatch.setattr(mind, "_run_voice", lambda *a, **kw: None)
    monkeypatch.setattr(mind, "_last_spoken_text", "")
    monkeypatch.setenv("PX_DRY", "1")
    # Should not raise — missing persona falls back to no persona-voice injection
    thought = {"action": "comment", "thought": "test", "mood": "content", "salience": 0.5}
    mind.expression(thought, dry=True, awareness={})


def test_expression_survives_lock_timeout_on_update(monkeypatch):
    """expression() logs and continues when update_session raises FileLockTimeout."""
    monkeypatch.setattr(mind, "load_session", lambda: {"persona": "spark"})
    monkeypatch.setattr(mind, "update_session",
                        lambda **kw: (_ for _ in ()).throw(FileLockTimeout("fake.lock")))
    monkeypatch.setattr(mind, "_run_voice", lambda *a, **kw: None)
    monkeypatch.setattr(mind, "_last_spoken_text", "")
    monkeypatch.setenv("PX_DRY", "1")
    thought = {"action": "comment", "thought": "test", "mood": "content", "salience": 0.5}
    mind.expression(thought, dry=True, awareness={})


# ---------------------------------------------------------------------------
# expression() executed/suppressed return contract
# ---------------------------------------------------------------------------


def _quiet_daytime(monkeypatch):
    """Neutralize gates unrelated to the behavior under test."""
    monkeypatch.setattr(mind, "_is_night_silence", lambda h: False)
    monkeypatch.setattr(mind, "load_session", lambda: {"persona": ""})
    monkeypatch.setattr(mind, "update_session", lambda **k: None)


def test_expression_returns_true_when_dispatched(monkeypatch):
    _quiet_daytime(monkeypatch)
    calls = []
    monkeypatch.setattr(mind, "_run_voice", lambda env, label="": calls.append(label))
    aw = {"obi_mode": "active", "calendar": {}, "ha_context": {}}
    result = mind.expression({"action": "greet", "thought": "hello"}, dry=True, awareness=aw)
    assert result is True
    assert calls == ["greet"]


def test_expression_returns_false_when_gated(monkeypatch):
    _quiet_daytime(monkeypatch)
    calls = []
    monkeypatch.setattr(mind, "_run_voice", lambda env, label="": calls.append(label))
    aw = {"obi_mode": "absent", "calendar": {}, "ha_context": {}}
    result = mind.expression({"action": "greet", "thought": "hello"}, dry=True, awareness=aw)
    assert result is False
    assert calls == []


def test_expression_returns_false_for_wait():
    assert mind.expression({"action": "wait"}, dry=True, awareness={}) is False


def test_reactive_mechanism_removed():
    """Tripwire: the template-based reactive path is gone; transitions go
    through reflection instead."""
    assert not hasattr(mind, "REACTIVE_TEMPLATES")
    assert not hasattr(mind, "reactive_response")
    assert not hasattr(mind, "REACTIVE_COOLDOWN_S")
    assert not hasattr(mind, "_last_reactive_phrases")
    src = inspect.getsource(mind.mind_loop)
    assert "reactive" not in src.lower()
    assert "reacted" not in src


# ---------------------------------------------------------------------------
# greet_arrival: SPARK prompt exposure + gating semantics
# ---------------------------------------------------------------------------


def test_spark_prompt_exposes_greet_arrival():
    from pxh import spark_config
    suffix = spark_config._SPARK_REFLECTION_SUFFIX
    # once in the rules bullet, once in the JSON action enumeration
    assert suffix.count("greet_arrival") >= 2
    assert "person_arrived_home" in suffix


def test_greet_arrival_suppressed_during_decompress(monkeypatch):
    _quiet_daytime(monkeypatch)
    calls = []
    monkeypatch.setattr(mind, "_run_voice", lambda env, label="": calls.append(label))
    aw = {"obi_mode": "active",
          "calendar": {"current_event": "After School Decompress"},
          "ha_context": {}}
    result = mind.expression({"action": "greet_arrival", "thought": "hi Dad"},
                             dry=True, awareness=aw)
    assert result is False
    assert calls == []


def test_greet_arrival_not_gated_by_absence_modes(monkeypatch):
    """Arrivals invalidate the absence heuristic — at-mums/absent must NOT
    suppress an arrival greeting."""
    _quiet_daytime(monkeypatch)
    calls = []
    monkeypatch.setattr(mind, "_run_voice", lambda env, label="": calls.append(label))
    for mode in ("absent", "at-mums", "at-school"):
        aw = {"obi_mode": mode, "calendar": {}, "ha_context": {}}
        result = mind.expression({"action": "greet_arrival", "thought": "hi"},
                                 dry=True, awareness=aw)
        assert result is True, mode
    assert calls == ["greet_arrival"] * 3


def test_greet_arrival_respects_night_silence(monkeypatch):
    _quiet_daytime(monkeypatch)
    monkeypatch.setattr(mind, "_is_night_silence", lambda h: True)
    calls = []
    monkeypatch.setattr(mind, "_run_voice", lambda env, label="": calls.append(label))
    result = mind.expression({"action": "greet_arrival", "thought": "hi"},
                             dry=True, awareness={"obi_mode": "active",
                                                  "calendar": {}, "ha_context": {}})
    assert result is False
    assert calls == []
    assert "greet_arrival" not in mind.NIGHT_ALLOWED_ACTIONS


def test_should_express_cooldown_matrix():
    C = mind.EXPRESSION_COOLDOWN_S
    A = mind.GREET_ARRIVAL_COOLDOWN_S
    arrival = ["person_arrived_home:adrian_chipolo"]

    # normal action: pure global-cooldown behavior
    assert mind._should_express("comment", [], now=C + 1.0,
                                last_expression_mono=0.0,
                                last_greet_arrival_mono=0.0) is True
    assert mind._should_express("comment", arrival, now=C - 1.0,
                                last_expression_mono=0.0,
                                last_greet_arrival_mono=0.0) is False

    # greet_arrival + arrival transition bypasses the global cooldown
    assert mind._should_express("greet_arrival", arrival, now=C - 1.0,
                                last_expression_mono=0.0,
                                last_greet_arrival_mono=0.0) is True

    # ...but not within the anti-flap window
    assert mind._should_express("greet_arrival", arrival, now=A - 1.0,
                                last_expression_mono=0.0,
                                last_greet_arrival_mono=0.0) is False

    # greet_arrival WITHOUT an arrival transition gets no bypass
    assert mind._should_express("greet_arrival", [], now=C - 1.0,
                                last_expression_mono=0.0,
                                last_greet_arrival_mono=0.0) is False


def test_mind_loop_uses_should_express():
    src = inspect.getsource(mind.mind_loop)
    assert "_should_express(" in src
    assert "last_greet_arrival_mono" in src


# ---------------------------------------------------------------------------
# Task 8: px-wander is the single writer of exploration_meta
# ---------------------------------------------------------------------------


def test_mind_dispatch_does_not_write_cooldown(tmp_path, monkeypatch):
    """expression("explore") must not write exploration_meta.json.

    px-wander's start-of-run write is the single cooldown writer; a second
    writer in mind.py made the cooldown non-atomic with the run itself.
    """
    monkeypatch.setenv("LOG_DIR", str(tmp_path))       # no px-alive.pid -> no 5s wait
    monkeypatch.setattr(mind, "STATE_DIR", tmp_path)
    monkeypatch.setattr(mind, "AWARENESS_FILE", tmp_path / "awareness.json")
    monkeypatch.setattr(mind, "BATTERY_FILE", tmp_path / "battery.json")
    monkeypatch.setattr(mind, "_is_night_silence", lambda hour: False)
    monkeypatch.setattr(mind, "load_session", lambda: {"persona": ""})
    monkeypatch.setattr(mind, "_can_explore", lambda session, awareness: True)
    monkeypatch.setattr(mind, "append_thought", lambda *a, **k: None)
    monkeypatch.setattr(mind, "time", type("_T", (), {"sleep": staticmethod(lambda s: None)}))
    monkeypatch.setattr(mind.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "active", ""))

    launched = []

    class _FakeProc:
        returncode = 0
        def communicate(self, timeout=None):
            return (json.dumps({"status": "ok", "observations": 0}), "")

    def _fake_popen(*a, **k):
        launched.append((a, k))
        return _FakeProc()

    monkeypatch.setattr(mind.subprocess, "Popen", _fake_popen)

    mind.expression({"action": "explore", "thought": "time to roam"}, dry=False)

    # the wander subprocess still gets launched...
    assert len(launched) == 1
    assert launched[0][1]["env"]["PX_WANDER_MODE"] == "explore"
    # ...but mind never establishes the cooldown itself
    assert not (tmp_path / "exploration_meta.json").exists()
