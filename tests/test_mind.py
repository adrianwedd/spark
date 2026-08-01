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


# ---------------------------------------------------------------------------
# Re-roll instead of persisting junk (Phase 1.2 / 1.3)
# ---------------------------------------------------------------------------

_VALID = {"thought": "The afternoon light is doing something new on the wall.",
          "mood": "curious", "action": "comment", "salience": 0.6}
_NOVEL = {"thought": "Someone left a mug on the bench and it sat there all day.",
          "mood": "amused", "action": "comment", "salience": 0.6}
_EMPTY = {"thought": "", "mood": "curious", "action": "wait", "salience": 0.2}


def _drive_reflection_seq(monkeypatch, payloads, *, recent=(), backend="ollama-m5",
                          last_spoken=""):
    """Run reflection() against a SEQUENCE of stubbed LLM responses.

    The final payload repeats if reflection asks more times than provided, so
    a `len(calls) == N` assertion pins the retry count rather than merely
    tolerating it. A payload given as a plain `str` is returned as the raw
    response verbatim — that is how the no-JSON cases are driven, since a dict
    payload can only ever produce well-formed JSON. Returns (captured, calls).
    """
    captured = {}
    calls = []

    def _call(*a, **k):
        payload = payloads[min(len(calls), len(payloads) - 1)]
        calls.append(payload)
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        return {"response": raw, "backend": backend}

    monkeypatch.setattr(mind, "call_llm", _call)
    monkeypatch.setattr(mind, "load_session", lambda: {"persona": ""})
    monkeypatch.setattr(mind, "load_recent_thoughts", lambda *a, **k: list(recent))
    monkeypatch.setattr(mind, "load_notes", lambda *a, **k: [])
    monkeypatch.setattr(mind, "append_thought",
                        lambda t, persona="": captured.__setitem__("appended", t))
    monkeypatch.setattr(mind, "auto_remember",
                        lambda t, persona="": captured.__setitem__("remembered", t))
    monkeypatch.setattr(mind, "atomic_write", lambda *a, **k: None)
    monkeypatch.setattr(mind, "_last_spoken_text", last_spoken, raising=False)
    captured["returned"] = mind.reflection({"persona": ""}, dry=False)
    return captured, calls


def test_empty_thought_triggers_reroll(monkeypatch):
    captured, calls = _drive_reflection_seq(monkeypatch, [_EMPTY, _VALID])
    assert len(calls) == 2, "an empty thought must be re-rolled once"
    assert captured["appended"]["thought"] == _VALID["thought"]


def test_empty_thought_after_reroll_is_dropped(monkeypatch):
    """Never persist blank text — a blank thought reaches the public endpoint."""
    captured, calls = _drive_reflection_seq(monkeypatch, [_EMPTY, _EMPTY])
    assert len(calls) == 2, "exactly one re-roll, then give up"
    assert "appended" not in captured
    assert captured["returned"] is None


def test_whitespace_only_thought_is_treated_as_empty(monkeypatch):
    blank = {**_EMPTY, "thought": "   \n\t "}
    captured, calls = _drive_reflection_seq(monkeypatch, [blank, _VALID])
    assert len(calls) == 2
    assert captured["appended"]["thought"] == _VALID["thought"]


def test_missing_thought_key_is_treated_as_empty(monkeypatch):
    captured, calls = _drive_reflection_seq(
        monkeypatch, [{"mood": "curious", "action": "wait", "salience": 0.2}, _VALID])
    assert len(calls) == 2
    assert captured["appended"]["thought"] == _VALID["thought"]


def test_similar_thought_triggers_reroll(monkeypatch):
    """A near-duplicate gets one more chance rather than collapsing to wait."""
    captured, calls = _drive_reflection_seq(
        monkeypatch, [_VALID, _NOVEL], recent=[{"thought": _VALID["thought"]}])
    assert len(calls) == 2
    assert captured["appended"]["thought"] == _NOVEL["thought"]
    assert captured["appended"]["action"] == "comment", \
        "a successful re-roll must not be suppressed"
    assert captured["appended"]["salience"] > 0.0


def test_similar_after_reroll_falls_back_to_suppression(monkeypatch):
    """If the re-roll is also a duplicate, the existing safety net still applies.

    Suppression is kept rather than dropped so the private-DM redaction path
    below it stays reachable — see
    test_reflection_redacts_private_dm_even_when_similarity_suppressed.
    """
    captured, calls = _drive_reflection_seq(
        monkeypatch, [_VALID, _VALID], recent=[{"thought": _VALID["thought"]}])
    assert len(calls) == 2
    assert captured["appended"]["action"] == "wait"
    assert captured["appended"]["salience"] == 0.0


def test_no_reroll_when_paid_tier_served(monkeypatch):
    """Re-rolling on the metered tier doubles spend that already leaks budget.

    Reflection's Claude tier bypasses the session budget entirely, and it only
    serves when Ollama is already down — exactly when adding a second call is
    worst on both cost and load.
    """
    captured, calls = _drive_reflection_seq(
        monkeypatch, [_EMPTY, _VALID], backend="claude")
    assert len(calls) == 1, "must not re-roll on the paid backend"
    assert "appended" not in captured
    assert captured["returned"] is None


def test_null_thought_is_treated_as_empty(monkeypatch):
    """`{"thought": null}` must not become the literal string "None".

    `str(parsed.get("thought", ""))` yields "None" for a JSON null — a truthy
    4-character string that sails past an emptiness check and lands on the
    public thoughts endpoint. Small models emit null as readily as "".
    """
    captured, calls = _drive_reflection_seq(
        monkeypatch, [{**_EMPTY, "thought": None}, _VALID])
    assert len(calls) == 2, "a null thought must be re-rolled like an empty one"
    assert captured["appended"]["thought"] == _VALID["thought"]


def test_null_thought_after_reroll_is_dropped(monkeypatch):
    """The terminal null case must drop, not persist "None"."""
    null = {**_EMPTY, "thought": None}
    captured, calls = _drive_reflection_seq(monkeypatch, [null, null])
    assert len(calls) == 2
    assert "appended" not in captured, "must never persist a null thought"
    assert captured["returned"] is None


def test_reroll_triggered_by_similarity_to_last_spoken(monkeypatch):
    """The `last_spoken` branch of _reroll_reason had no coverage.

    Every other similarity test drives the `recent_thoughts` loop; this one
    pins the second source, which the docstring claims cannot disagree with
    the suppressor.
    """
    captured, calls = _drive_reflection_seq(
        monkeypatch, [_VALID, _NOVEL], last_spoken=_VALID["thought"])
    assert len(calls) == 2, "similarity to spoken text must re-roll"
    assert captured["appended"]["thought"] == _NOVEL["thought"]


def test_no_reroll_on_ollama_cloud(monkeypatch):
    """Ollama Cloud is a paid API — re-rolling there doubles cloud spend."""
    captured, calls = _drive_reflection_seq(
        monkeypatch, [_EMPTY, _VALID], backend="ollama-cloud")
    assert len(calls) == 1, "must not re-roll on the paid cloud backend"
    assert captured["returned"] is None


def test_no_reroll_on_unknown_backend_label(monkeypatch):
    """The cost guard must fail CLOSED on a label it does not recognise.

    `backend not in {"claude"}` is True for "" and for any future or renamed
    label, so an unrecognised tier silently earns a second billable call.
    """
    captured, calls = _drive_reflection_seq(
        monkeypatch, [_EMPTY, _VALID], backend="")
    assert len(calls) == 1, "unknown backend must not be re-rolled"


def test_reroll_prompt_is_not_byte_identical(monkeypatch):
    """The retry must tell the model what was wrong with attempt 1.

    Re-sending the identical context relies purely on sampling luck; naming
    the fault is what makes the second attempt better than a coin flip.
    """
    prompts = []

    def _call(context, system, persona=""):
        prompts.append(context)
        payload = _EMPTY if len(prompts) == 1 else _VALID
        return {"response": json.dumps(payload), "backend": "ollama-m5"}

    monkeypatch.setattr(mind, "call_llm", _call)
    monkeypatch.setattr(mind, "load_session", lambda: {"persona": ""})
    monkeypatch.setattr(mind, "load_recent_thoughts", lambda *a, **k: [])
    monkeypatch.setattr(mind, "load_notes", lambda *a, **k: [])
    monkeypatch.setattr(mind, "append_thought", lambda t, persona="": None)
    monkeypatch.setattr(mind, "auto_remember", lambda t, persona="": None)
    monkeypatch.setattr(mind, "atomic_write", lambda *a, **k: None)
    monkeypatch.setattr(mind, "_last_spoken_text", "", raising=False)
    mind.reflection({"persona": ""}, dry=False)

    assert len(prompts) == 2
    assert prompts[1] != prompts[0], "the retry must carry a corrective hint"


# ---------------------------------------------------------------------------
# Re-roll on a malformed (no-JSON) response
#
# This is the failure the live logs actually show — "reflection: no JSON in
# response" — and it was the one shape the re-roll did not cover: the first
# malformed answer returned None and burned the whole cycle.
# ---------------------------------------------------------------------------

_PROSE = "Sure! Here's a thought for you: the light is nice today."


def _capture_health(monkeypatch):
    """Record every px-mind-reflection failure so double-counting is visible."""
    failures = []
    monkeypatch.setattr(
        mind.health_mod, "record_failure",
        lambda component, reason, **k: failures.append((component, reason)))
    monkeypatch.setattr(mind.health_mod, "record_success", lambda *a, **k: None)
    return failures


def test_no_json_triggers_reroll(monkeypatch):
    """A malformed response gets the same second chance as an empty one."""
    captured, calls = _drive_reflection_seq(monkeypatch, [_PROSE, _VALID])
    assert len(calls) == 2, "a response with no JSON must be re-rolled once"
    assert captured["appended"]["thought"] == _VALID["thought"]


def test_no_json_after_reroll_is_dropped(monkeypatch):
    captured, calls = _drive_reflection_seq(monkeypatch, [_PROSE, _PROSE])
    assert len(calls) == 2, "exactly one re-roll, then give up"
    assert "appended" not in captured
    assert captured["returned"] is None


def test_no_json_records_one_failure_per_cycle(monkeypatch):
    """Two malformed attempts are one failed cycle, not two.

    Health escalates to `failing` at 3 consecutive failures, so counting both
    attempts of a single cycle would trip the alarm in half the real time.
    """
    failures = _capture_health(monkeypatch)
    _drive_reflection_seq(monkeypatch, [_PROSE, _PROSE])
    reflection_failures = [f for f in failures if f[0] == "px-mind-reflection"]
    assert len(reflection_failures) == 1, reflection_failures
    assert reflection_failures[0][1] == "no JSON in response"


def test_no_reroll_on_no_json_when_paid_tier_served(monkeypatch):
    """The cost guard applies to malformed responses too."""
    captured, calls = _drive_reflection_seq(
        monkeypatch, [_PROSE, _VALID], backend="claude")
    assert len(calls) == 1, "must not re-roll on the paid backend"
    assert captured["returned"] is None


def test_no_json_on_retry_keeps_the_first_parse(monkeypatch):
    """A malformed attempt 2 must not throw away a usable attempt 1.

    Attempt 1 here is merely *similar* — still a real thought that records the
    mind ran. Dropping the cycle because the retry came back as prose would be
    strictly worse than the suppression path that already exists for it.
    """
    captured, calls = _drive_reflection_seq(
        monkeypatch, [_VALID, _PROSE], recent=[{"thought": _VALID["thought"]}])
    assert len(calls) == 2
    assert captured["returned"] is not None
    assert captured["appended"]["thought"] == _VALID["thought"]
    assert captured["appended"]["action"] == "wait", "still suppressed as a duplicate"


def test_empty_on_retry_keeps_the_first_parse(monkeypatch):
    """A blank attempt 2 must not throw away a usable attempt 1.

    Same contract as the no-JSON case above: attempt 1 is merely *similar* —
    a real thought that records the mind ran, persisted via the suppression
    path. Live trace 2026-08-01T11:30: re-rolling (similar) → dropped as
    "empty thought", because the blank retry overwrote the held parse.
    """
    captured, calls = _drive_reflection_seq(
        monkeypatch, [_VALID, _EMPTY], recent=[{"thought": _VALID["thought"]}])
    assert len(calls) == 2
    assert captured["returned"] is not None
    assert captured["appended"]["thought"] == _VALID["thought"]
    assert captured["appended"]["action"] == "wait", "still suppressed as a duplicate"


def test_empty_on_retry_of_similar_records_no_failure(monkeypatch):
    """Keeping attempt 1 means the cycle succeeded — no health failure."""
    failures = _capture_health(monkeypatch)
    _drive_reflection_seq(
        monkeypatch, [_VALID, _EMPTY], recent=[{"thought": _VALID["thought"]}])
    assert [f for f in failures if f[0] == "px-mind-reflection"] == []


def test_similar_retry_replaces_an_empty_first_attempt(monkeypatch):
    """The retry IS adopted when it is strictly better than what is held.

    empty → similar is an improvement: a real (if duplicate) thought beats a
    blank one, so the suppression path persists it instead of the cycle
    dropping as "empty thought".
    """
    captured, calls = _drive_reflection_seq(
        monkeypatch, [_EMPTY, _VALID], recent=[{"thought": _VALID["thought"]}])
    assert len(calls) == 2
    assert captured["returned"] is not None
    assert captured["appended"]["thought"] == _VALID["thought"]
    assert captured["appended"]["action"] == "wait"


def test_no_json_reroll_prompt_carries_a_hint(monkeypatch):
    """The retry must name the fault — here, that the output was not JSON."""
    prompts = []

    def _call(context, system, persona=""):
        prompts.append(context)
        if len(prompts) == 1:
            return {"response": _PROSE, "backend": "ollama-m5"}
        return {"response": json.dumps(_VALID), "backend": "ollama-m5"}

    monkeypatch.setattr(mind, "call_llm", _call)
    monkeypatch.setattr(mind, "load_session", lambda: {"persona": ""})
    monkeypatch.setattr(mind, "load_recent_thoughts", lambda *a, **k: [])
    monkeypatch.setattr(mind, "load_notes", lambda *a, **k: [])
    monkeypatch.setattr(mind, "append_thought", lambda t, persona="": None)
    monkeypatch.setattr(mind, "auto_remember", lambda t, persona="": None)
    monkeypatch.setattr(mind, "atomic_write", lambda *a, **k: None)
    monkeypatch.setattr(mind, "_last_spoken_text", "", raising=False)
    mind.reflection({"persona": ""}, dry=False)

    assert len(prompts) == 2
    assert prompts[1] != prompts[0]
    assert "JSON" in prompts[1][len(prompts[0]):], \
        "the hint must actually mention the JSON requirement"


def test_every_reroll_reason_has_a_hint():
    """A reason without a hint is a KeyError on the retry path, not a re-roll."""
    assert set(mind._REROLL_HINTS) >= {"empty", "similar", "no_json"}


def _reflection_prompt(monkeypatch, awareness):
    """Run reflection() against a stubbed LLM and return the prompt it was sent."""
    prompts = []

    def _call(context, system, persona=""):
        prompts.append(context)
        return {"response": json.dumps(_VALID), "backend": "ollama-m5"}

    monkeypatch.setattr(mind, "call_llm", _call)
    monkeypatch.setattr(mind, "load_session", lambda: {"persona": ""})
    monkeypatch.setattr(mind, "load_recent_thoughts", lambda *a, **k: [])
    monkeypatch.setattr(mind, "load_notes", lambda *a, **k: [])
    monkeypatch.setattr(mind, "append_thought", lambda t, persona="": None)
    monkeypatch.setattr(mind, "auto_remember", lambda t, persona="": None)
    monkeypatch.setattr(mind, "atomic_write", lambda *a, **k: None)
    monkeypatch.setattr(mind, "_last_spoken_text", "", raising=False)
    mind.reflection(awareness, dry=False)
    return prompts[0]


def test_vitals_line_carries_no_temperature_numeral(monkeypatch):
    """The always-on vitals line must not hand the model a bare temperature.

    Confabulation mechanism seen live 2026-08-01: 'temperature 66°C' lands
    adjacent to 'Office light is on' in the joined prompt, and the model
    re-binds the numeral to the lamp — then auto-remembers the false fact.
    A quantity the model is never handed cannot be misattributed.
    """
    ctx = _reflection_prompt(monkeypatch, {
        "persona": "",
        "system": {"cpu_pct": 19, "ram_pct": 55, "cpu_temp_c": 66.4,
                   "disk_pct": 55},
    })
    vitals = [l for l in ctx.splitlines() if "Your system vitals" in l]
    assert vitals, "the vitals line itself must survive"
    assert "°C" not in vitals[0]
    assert "66" not in vitals[0]


def test_temperature_symptom_branches_survive(monkeypatch):
    """C1 drops only the unconditional numeral — the >=70/>=80 branches
    phrase temperature as a bodily symptom bound to 'CPU' and stay."""
    ctx = _reflection_prompt(monkeypatch, {
        "persona": "",
        "system": {"cpu_pct": 19, "ram_pct": 55, "cpu_temp_c": 84.0,
                   "disk_pct": 55},
    })
    assert "YOUR CPU TEMPERATURE IS 84.0°C" in ctx


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


def test_silent_actions_bypass_expression_cooldown():
    """The expression budget is a SPEECH budget. Silent cognitive actions
    (the night-allowed set) produce no audio, so queueing them behind small
    talk gates nothing worth gating — live case: a salience-1.0 self_debug
    suppressed by the 30-min cooldown at 13:29."""
    C = mind.EXPRESSION_COOLDOWN_S
    for action in ("self_debug", "remember", "introspect", "set_goal"):
        assert mind._should_express(action, [], now=C - 1.0,
                                    last_expression_mono=0.0,
                                    last_greet_arrival_mono=0.0) is True, action
    # spoken actions still wait out the cooldown
    assert mind._should_express("comment", [], now=C - 1.0,
                                last_expression_mono=0.0,
                                last_greet_arrival_mono=0.0) is False


def test_silent_actions_is_night_set_minus_wait():
    """One vetted no-audio-no-motion list, not two drifting ones."""
    assert mind.SILENT_ACTIONS == mind.NIGHT_ALLOWED_ACTIONS - {"wait"}


def test_silent_actions_do_not_charge_the_speech_budget():
    """A silent dispatch must not push back the next spoken expression."""
    src = inspect.getsource(mind.mind_loop)
    assert "SILENT_ACTIONS" in src


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


# ── Daemon health in the reflection context ─────────────────────────

def _reflection_harness(monkeypatch, captured):
    """Common stubs so reflection() can run without touching disk or a model."""
    def _fake_llm(prompt, system, persona=""):
        captured["prompt"] = prompt
        return {"response": json.dumps(
            {"thought": "x", "mood": "content", "action": "wait", "salience": 0.2}),
            "backend": "ollama-m5"}

    monkeypatch.setattr(mind, "call_llm", _fake_llm)
    monkeypatch.setattr(mind, "load_session", lambda: {"persona": ""})
    monkeypatch.setattr(mind, "load_recent_thoughts", lambda *a, **k: [])
    monkeypatch.setattr(mind, "load_notes", lambda *a, **k: [])
    monkeypatch.setattr(mind, "append_thought", lambda *a, **k: None)
    monkeypatch.setattr(mind, "auto_remember", lambda *a, **k: None)
    monkeypatch.setattr(mind, "atomic_write", lambda *a, **k: None)


def test_reflection_context_reports_unhealthy_daemons(monkeypatch):
    """A broken daemon must reach SPARK's inner monologue — that is the whole
    point of the spine: failures stop being invisible."""
    captured = {}
    _reflection_harness(monkeypatch, captured)
    monkeypatch.setattr(mind.health_mod, "summarize",
                        lambda *a, **k: "px-post: failing after 3 failures (HEALTH-MARKER)")
    mind.reflection({"persona": "", "health": {"overall": "failing"}}, dry=False)
    assert "HEALTH-MARKER" in captured["prompt"]


def test_reflection_context_silent_when_everything_is_well(monkeypatch):
    """summarize() returns "" on a healthy system so good days add no noise."""
    captured = {}
    _reflection_harness(monkeypatch, captured)
    monkeypatch.setattr(mind.health_mod, "summarize", lambda *a, **k: "")
    mind.reflection({"persona": "", "health": {"overall": "ok"}}, dry=False)
    assert "isn't working right" not in captured["prompt"]


def test_reflection_prompt_excludes_the_health_block(monkeypatch):
    """The structured block is for the dashboard. Putting it in the prompt would
    add hundreds of tokens per reflection to say what summarize() says in one
    line — and reflection can fall through to a paid tier."""
    captured = {}
    _reflection_harness(monkeypatch, captured)
    monkeypatch.setattr(mind.health_mod, "summarize", lambda *a, **k: "")
    awareness = {"persona": "", "sonar_cm": 42,
                 "health": {"overall": "ok",
                            "components": {"px-post": {"status": "ok",
                                                       "UNIQUE-BLOCK-MARKER": 1}}}}
    mind.reflection(awareness, dry=False)
    assert "UNIQUE-BLOCK-MARKER" not in captured["prompt"]
    assert "42" in captured["prompt"]      # the rest of awareness still arrives


def test_reflection_records_the_serving_backend(monkeypatch, tmp_path):
    """Health carries which tier answered, so paid-tier drift is measurable."""
    captured = {}
    _reflection_harness(monkeypatch, captured)
    recorded = {}
    monkeypatch.setattr(mind.health_mod, "record_success",
                        lambda comp, detail=None, **k: recorded.update({comp: detail}))
    mind.reflection({"persona": ""}, dry=False)
    assert recorded["px-mind-reflection"]["backend"] == "ollama-m5"


def test_reflection_records_failure_when_no_json_returned(monkeypatch):
    """A tier that answers with junk is distinct from all tiers being down —
    both used to surface to the caller as a bare None."""
    recorded = {}
    monkeypatch.setattr(mind, "call_llm",
                        lambda *a, **k: {"response": "not json at all",
                                         "backend": "ollama-m5"})
    monkeypatch.setattr(mind, "load_session", lambda: {"persona": ""})
    monkeypatch.setattr(mind, "load_recent_thoughts", lambda *a, **k: [])
    monkeypatch.setattr(mind, "load_notes", lambda *a, **k: [])
    monkeypatch.setattr(mind.health_mod, "record_failure",
                        lambda comp, err, detail=None, **k: recorded.update({comp: err}))
    assert mind.reflection({"persona": ""}, dry=False) is None
    assert recorded["px-mind-reflection"] == "no JSON in response"


# ---------------------------------------------------------------------------
# extract_json — unit tests for the LLM-quirk repair passes
# ---------------------------------------------------------------------------

def test_extract_json_handles_comma_inside_closing_quote():
    """Ollama sometimes puts the field-separator comma inside the string value
    instead of after the closing quote.  Reproduces the live failure logged at
    2026-07-30T19:09:01+10:00."""
    raw = (
        '{\n'
        '  "thought": "I\'m thinking about Adrian\'s \'huh\' when he says goodbye,"\n'
        '  "mood": "lonely",\n'
        '  "action": "wait",\n'
        '  "salience": 0.5,\n'
        '  "reflection_status": "healthy"\n'
        '}'
    )
    result = mind.extract_json(raw)
    assert result is not None, "extract_json should recover from comma-inside-string"
    assert result["mood"] == "lonely"
    assert result["action"] == "wait"
    assert "goodbye" in result["thought"]


def test_extract_json_valid_json_unchanged():
    """Well-formed JSON must still parse correctly after the repair pass."""
    raw = '{"thought": "A quiet house.", "mood": "calm", "action": "wait", "salience": 0.3}'
    result = mind.extract_json(raw)
    assert result == {"thought": "A quiet house.", "mood": "calm", "action": "wait", "salience": 0.3}


def test_extract_json_string_legitimately_ending_with_comma():
    """A string value that genuinely ends with a comma (valid JSON) must not be mangled."""
    raw = '{"thought": "One, two,", "mood": "happy", "salience": 0.4}'
    result = mind.extract_json(raw)
    assert result is not None
    assert result["thought"] == "One, two,"
