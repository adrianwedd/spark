"""Tests for night-silence helper and announce action in pxh.mind."""
import inspect
import json
import subprocess
import pytest
from unittest.mock import Mock, patch
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


def test_mind_remember_dispatch_types_the_note_as_sparks_own_narrative(monkeypatch):
    """px-mind's `remember` acts on SPARK's reflection, so it is narrative."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs.get("env") or {}
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    monkeypatch.setattr(mind.subprocess, "run", fake_run)
    monkeypatch.setattr(mind, "_is_night_silence", lambda h: False)
    mind.expression({"action": "remember", "text": "the house was quiet today",
                     "thought": "the house was quiet today", "mood": "calm",
                     "salience": 0.5}, dry=False)

    assert captured["env"]["PX_NOTE_KIND"] == "narrative"
    assert captured["env"]["PX_NOTE"] == "the house was quiet today"


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


def test_reflection_prompt_excludes_all_location_coordinates(monkeypatch):
    """awareness carries raw GPS twice — findmyhub tracker coords and
    ha_presence per-person lat/lon (the house, to 5 m). Neither may reach the
    reflection prompt: thoughts feed the public feed and Bluesky. The prose
    "Who's home" section (names + home/away) is the only presence allowed."""
    captured = {}
    _reflection_harness(monkeypatch, captured)
    awareness = {
        "persona": "", "sonar_cm": 42,
        "ha_presence": {"people": [
            {"name": "Adrian", "state": "home", "home": True,
             "lat": -43.13558, "lon": 147.11829, "gps_accuracy_m": 5.0}]},
        "findmyhub": {"obi-bag": {"lat": -42.88372, "lon": 147.32941,
                                  "place": "school", "age_min": 3}},
    }
    mind.reflection(awareness, dry=False)
    prompt = captured["prompt"]
    for leak in ("-43.13", "147.11", "-42.88", "147.32",
                 "gps_accuracy", "findmyhub", '"lat"', '"lon"'):
        assert leak not in prompt, f"location leak in reflection prompt: {leak}"
    assert "42" in prompt  # the rest of awareness still arrives


def test_reflection_awareness_json_is_allowlisted(monkeypatch):
    """New awareness keys must not reach the prompt until deliberately added —
    a denylist is how the GPS leak happened in the first place."""
    captured = {}
    _reflection_harness(monkeypatch, captured)
    mind.reflection({"persona": "", "sonar_cm": 42,
                     "some_future_key": "NOVEL-KEY-MARKER"}, dry=False)
    assert "NOVEL-KEY-MARKER" not in captured["prompt"]
    assert "42" in captured["prompt"]


# --- A3: the allowlist must fail closed, and must still hold against the -----
# --- awareness snapshot the robot is actually producing right now. -----------

# Top-level awareness keys that carry household location or per-person presence.
# Naming one here is a claim that it must NEVER reach the reflection prompt,
# because the prompt determines the thought text and the thought text is what
# /api/v1/public/thoughts, site/data/feed.json and Bluesky publish.
SENSITIVE_AWARENESS_KEYS = ("findmyhub", "ha_presence")

# Key-name segments that mark a value as a coordinate. Matching is on `_`-split
# segments, not substrings, so "translate"/"latency" do not trip it. A new
# innocent key called e.g. "accuracy" WILL fail this test — that is deliberate:
# widening this set should be a conscious review, not a silent default.
_COORD_SEGMENTS = frozenset({
    "lat", "latitude", "lon", "lng", "longitude", "gps", "coord", "coords",
    "coordinate", "coordinates", "altitude", "geo", "accuracy",
})

# The subset that is an actual position. Searching the prompt for a *value*
# only makes sense for these: an accuracy radius like 21 is not a location, and
# looking for "21" in a prompt containing "cpu_pct": 21.4 finds a phantom leak.
_POSITION_SEGMENTS = frozenset({"lat", "latitude", "lon", "lng", "longitude"})


def _is_coord_key(key: str) -> bool:
    return any(seg in _COORD_SEGMENTS for seg in str(key).lower().split("_"))


def _is_position_key(key: str) -> bool:
    return any(seg in _POSITION_SEGMENTS for seg in str(key).lower().split("_"))


def _coord_leaves(obj, path=()):
    """Yield (path, value) for every leaf whose key name marks a coordinate."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if _is_coord_key(k) and not isinstance(v, (dict, list)):
                yield path + (str(k),), v
            else:
                yield from _coord_leaves(v, path + (str(k),))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _coord_leaves(v, path + (f"[{i}]",))


def _live_awareness():
    """The robot's real state/awareness.json, or None when not on the robot."""
    import os
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    path = Path(os.environ.get("PX_STATE_DIR", str(root / "state"))) / "awareness.json"
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError, OSError):
        return None


@pytest.mark.parametrize("key", SENSITIVE_AWARENESS_KEYS)
def test_allowlist_omits_every_known_sensitive_key(key):
    """Fail closed by construction: the sensitive keys must be absent from the
    allowlist itself, not merely filtered somewhere downstream. Asserting on the
    frozenset catches a well-meaning "the prompt lacks context" edit that adds
    one back, which no prompt-output test would notice until it shipped."""
    assert key not in mind._REFLECTION_AWARENESS_KEYS


def test_no_coordinate_hides_under_an_allowlisted_key():
    """The allowlist filters TOP-LEVEL keys only, so it is sufficient only while
    every coordinate lives under a key it excludes. If Home Assistant ever nests
    a lat/lon inside an allowed key (ha_context, ha_routines, weather...), the
    filter silently stops protecting anything. This converts that from an
    invisible leak into a test failure."""
    awareness = _live_awareness() or _SYNTHETIC_AWARENESS
    offenders = [
        "/".join(path) for path, _ in _coord_leaves(awareness)
        if path and path[0] in mind._REFLECTION_AWARENESS_KEYS
    ]
    assert not offenders, (
        "coordinate-valued fields sit under allowlisted awareness keys and will "
        f"reach the reflection prompt: {offenders}")


def test_live_awareness_snapshot_leaks_no_coordinates_into_the_prompt(monkeypatch):
    """End-to-end against the snapshot this robot is producing right now, rather
    than a hand-written dict that can drift from reality. Every coordinate value
    is read out of the file itself, so the test cannot go stale by hardcoding
    the wrong numbers — and it strengthens automatically as awareness grows."""
    awareness = _live_awareness()
    if awareness is None:
        pytest.skip("no live state/awareness.json (not running on the robot)")
    positions = [(p, v) for p, v in _coord_leaves(awareness) if _is_position_key(p[-1])]
    assert positions, "live awareness carries no lat/lon — test proves nothing"

    captured = {}
    _reflection_harness(monkeypatch, captured)
    mind.reflection(dict(awareness, persona=""), dry=False)
    prompt = captured["prompt"]

    for path, value in positions:
        # Match on the significant prefix too: a rounded or reformatted render
        # of the same fix is still the house. 8 chars covers "-43.1355".
        for needle in {str(value), str(value)[:8]}:
            assert needle not in prompt, (
                f"live coordinate {'/'.join(path)}={value} reached the reflection prompt")
    # Match the JSON-key form, not the bare word: SPARK talks about its own
    # feeds, so "the findmyhub silence" can legitimately appear in a logged
    # conversation. `"findmyhub":` can only come from dumping the block.
    for key in SENSITIVE_AWARENESS_KEYS:
        assert f'"{key}":' not in prompt, (
            f"the {key!r} block was dumped into the reflection prompt")


# Shape-faithful stand-in used when state/awareness.json is absent (CI), so the
# nesting invariant is still exercised off-robot. Coordinates are fabricated.
_SYNTHETIC_AWARENESS = {
    "ts": "2026-08-15T06:00:00Z", "sonar_cm": 42, "battery_pct": 74,
    "ha_context": {"lights_on": 3}, "ha_routines": {"school_run": False},
    "weather": {"temp_c": 11.2}, "frigate": {"rooms_with_people": ["kitchen"]},
    "ha_presence": {"people": [
        {"name": "Adrian", "state": "home", "home": True,
         "lat": -43.13558, "lon": 147.11829, "gps_accuracy_m": 5.0}]},
    "findmyhub": {"adrian": {"lat": -42.88372, "lon": 147.32941, "accuracy_m": 12}},
    "health": {"overall": "ok"},
}


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


def test_expression_suppresses_audio_in_quiet_mode(monkeypatch):
    """Quiet mode is constitutional (pxh.policy rule 1) — it applies to the
    autonomous loop too, not just Obi-initiated turns."""
    _quiet_daytime(monkeypatch)
    monkeypatch.setattr(mind, "load_session", lambda: {"persona": "", "spark_quiet_mode": True})
    calls = []
    monkeypatch.setattr(mind, "_run_voice", lambda env, label="": calls.append(label))
    aw = {"obi_mode": "active", "calendar": {}, "ha_context": {}}
    result = mind.expression({"action": "greet", "thought": "hello"}, dry=True, awareness=aw)
    assert result is False
    assert calls == []


def test_expression_allows_presence_action_in_quiet_mode(monkeypatch):
    """Staying present is the point of the Three S's — only audio is blocked."""
    _quiet_daytime(monkeypatch)
    monkeypatch.setattr(mind, "load_session", lambda: {"persona": "", "spark_quiet_mode": True})
    mock_run = Mock(return_value=subprocess.CompletedProcess(
        args=[], returncode=0, stdout="{}", stderr=""))
    monkeypatch.setattr(mind.subprocess, "run", mock_run)
    aw = {"obi_mode": "active", "calendar": {}, "ha_context": {}}
    result = mind.expression({"action": "emote", "thought": "curious"}, dry=True, awareness=aw)
    assert result is True
    assert mock_run.called
