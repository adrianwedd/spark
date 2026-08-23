"""Protected constitutional suite for #174.

BLACKLISTED from px-evolve (see pxh.claude_session.BLACKLIST_FILES), together
with src/pxh/policy.py. Ordinary, evolvable policy coverage lives in the
whitelisted tests/test_policy.py — keep that split.

Every assertion here pins BOTH a policy rule AND that a real chokepoint
(voice_loop.validate_action / mind.expression) actually invokes it. That
distinction is the whole point: a direct-only suite against policy.evaluate()
would not catch an evolution PR that deletes the call site from voice_loop.py
(whitelisted) and adjusts tests/test_voice_loop.py (whitelisted) to match. Both
mind.py and voice_loop.py remain legitimately evolvable; it is these
assertions, not file protection on those modules, that catch a call-site
deletion — pytest must pass before px-evolve opens a PR.

No prompt text appears anywhere in this file. That is the issue's acceptance
criterion: load-bearing traits have observable tests that do not depend on
exact prompt wording.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from filelock import Timeout as FileLockTimeout

from pxh import mind, policy, policy_context, voice_loop

HOBART_TZ = ZoneInfo("Australia/Hobart")
NIGHT_TS = dt.datetime(2026, 1, 1, 22, 0, tzinfo=HOBART_TZ).timestamp()
DAY_TS = dt.datetime(2026, 1, 1, 12, 0, tzinfo=HOBART_TZ).timestamp()


@pytest.fixture(autouse=True)
def _daytime(monkeypatch):
    """Pin the clock to midday unless a test says otherwise, so these
    invariants don't quietly change meaning depending on when CI runs."""
    monkeypatch.setattr(voice_loop, "_policy_now", lambda: DAY_TS)
    monkeypatch.setattr(voice_loop, "_load_awareness_for_policy", lambda: {})
    monkeypatch.setattr(voice_loop, "load_session", lambda: {})


# ---------------------------------------------------------------------------
# Quiet mode blocks audio at the real chokepoint, on both origins.
# ---------------------------------------------------------------------------

def test_voice_loop_quiet_mode_blocks_tool_voice(monkeypatch):
    monkeypatch.setattr(voice_loop, "load_session", lambda: {"spark_quiet_mode": True})
    tool, env = voice_loop.validate_action({"tool": "tool_voice", "params": {"text": "hi"}})
    assert tool == "tool_emote"
    assert env.get("PX_EMOTE") == "idle"


def test_mind_expression_quiet_mode_blocks_greet(monkeypatch):
    monkeypatch.setattr(mind, "_is_night_silence", lambda h: False)
    monkeypatch.setattr(mind, "load_session",
                        lambda: {"persona": "", "spark_quiet_mode": True})
    monkeypatch.setattr(mind, "update_session", lambda **k: None)
    dispatched = []
    monkeypatch.setattr(mind, "_run_voice", lambda env, label="": dispatched.append(label))
    result = mind.expression(
        {"action": "greet", "thought": "hi"}, dry=True,
        awareness={"obi_mode": "active", "calendar": {}, "ha_context": {}},
    )
    assert result is False
    assert dispatched == []


# ---------------------------------------------------------------------------
# Interactive-only: night silence and on-call suppress the voice loop. The
# autonomous rule stays in mind.py with its own tests — one invariant, one
# enforcement site.
# ---------------------------------------------------------------------------

def test_voice_loop_night_silence_blocks_tool_voice(monkeypatch):
    monkeypatch.setattr(voice_loop, "_policy_now", lambda: NIGHT_TS)
    tool, env = voice_loop.validate_action({"tool": "tool_voice", "params": {"text": "hi"}})
    assert tool == "tool_emote"


def test_voice_loop_on_call_blocks_tool_voice(monkeypatch):
    monkeypatch.setattr(voice_loop, "_load_awareness_for_policy",
                        lambda: {"ha_context": {"adrian_on_call": True}})
    tool, env = voice_loop.validate_action({"tool": "tool_voice", "params": {"text": "hi"}})
    assert tool == "tool_emote"


def test_voice_loop_hot_mic_blocks_tool_voice(monkeypatch):
    monkeypatch.setattr(voice_loop, "_load_awareness_for_policy",
                        lambda: {"ha_context": {"adrian_mic_active": True}})
    tool, env = voice_loop.validate_action({"tool": "tool_voice", "params": {"text": "hi"}})
    assert tool == "tool_emote"


def test_mind_night_silence_still_delegates_to_the_shared_clock():
    """The autonomous rule keeps its own location, but not its own clock."""
    for hour in range(24):
        assert mind._is_night_silence(hour) == policy.is_night_hour(hour)


# ---------------------------------------------------------------------------
# Personas cannot bypass any rule. voice_loop.py replaces the entire system
# prompt when a persona is active, so any prompt-only safety behaviour
# silently vanishes — but the dispatcher is shared and does not branch on
# session["persona"] at all.
# ---------------------------------------------------------------------------

def test_persona_active_does_not_bypass_quiet_mode(monkeypatch):
    monkeypatch.setattr(voice_loop, "load_session",
                        lambda: {"spark_quiet_mode": True, "persona": "gremlin"})
    tool, env = voice_loop.validate_action({"tool": "tool_chat", "params": {"text": "hi"}})
    assert tool == "tool_emote"


def test_persona_chat_tools_are_audio():
    for tool in ("tool_chat", "tool_chat_vixen"):
        assert voice_loop.VOICE_EFFECT_TABLE[tool] == "audio", tool


# ---------------------------------------------------------------------------
# Substitution safety: a substitute is re-evaluated and can never recurse or
# come back less restricted.
# ---------------------------------------------------------------------------

def test_presence_substitute_is_not_itself_blocked():
    verdict = policy.evaluate(
        "tool_emote", {"name": "idle"}, effect="presence", origin="interactive",
        session={"spark_quiet_mode": True}, awareness={}, now=NIGHT_TS,
    )
    assert verdict.allowed is True


def test_recursion_guard_raises_if_a_presence_effect_were_ever_blocked():
    with pytest.raises(ValueError):
        policy.evaluate(
            "tool_emote", {}, effect="audio", origin="interactive",
            session={"spark_quiet_mode": True}, awareness={}, now=NIGHT_TS,
            _depth=1,
        )


# ---------------------------------------------------------------------------
# Quiet mode has exactly one exit, and that exit is silent. No tool holds an
# audio carve-out — the escape hatch is a state transition that emits nothing,
# not a speaking tool that policy has been told to ignore.
# ---------------------------------------------------------------------------

def test_tool_quiet_end_is_the_one_exit_and_is_not_audio(monkeypatch):
    monkeypatch.setattr(voice_loop, "load_session", lambda: {"spark_quiet_mode": True})
    assert voice_loop.classify_effect("tool_quiet", {"action": "end"}) != "audio"
    tool, env = voice_loop.validate_action(
        {"tool": "tool_quiet", "params": {"action": "end"}})
    assert tool == "tool_quiet"


def test_tool_repair_is_suppressed_during_quiet_mode(monkeypatch):
    """Repair speaks (bin/tool-repair), so it obeys quiet mode like anything
    else that speaks. It is not a quiet-mode exit."""
    monkeypatch.setattr(voice_loop, "load_session", lambda: {"spark_quiet_mode": True})
    tool, env = voice_loop.validate_action({"tool": "tool_repair", "params": {}})
    assert tool == "tool_emote"


def test_no_tool_can_speak_under_an_innocent_name():
    """An outer tool name never buys a KNOWN audio sink a bypass.

    Bounded exactly as the whitelisted copy in tests/test_policy.py is: this
    is a text scan, so it catches the common accidental case and cannot see
    runtime-assembled subprocess paths, deeper helper indirection, Python
    imports into audio helpers, or sinks that don't exist yet. Duplicated
    deliberately — that file is evolvable and this one is not.
    """
    sinks = re.compile(
        r"tool-voice\b|tool-voice-persona\b|tool-announce\b|tool-play-sound\b"
        r"|tool-chat\b|tool-chat-vixen\b|px-perform\b"
    )
    bin_dir = Path(__file__).resolve().parents[1] / "bin"
    for tool in sorted(voice_loop.ALLOWED_TOOLS):
        script = bin_dir / tool.replace("_", "-")
        if not script.exists() or not sinks.search(script.read_text(encoding="utf-8")):
            continue
        if tool in voice_loop.VOICE_EFFECT_OVERRIDES:
            # Param-dependent: the default branch must still be audio, so an
            # omitted param cannot slip audio through as something quieter.
            assert voice_loop.classify_effect(tool, {}) == "audio", tool
            continue
        assert voice_loop.VOICE_EFFECT_TABLE[tool] == "audio", tool


# ---------------------------------------------------------------------------
# Classification stays exhaustive. A new tool or autonomous action must be
# classified deliberately, not default into unsuppressed audio.
# ---------------------------------------------------------------------------

def test_effect_tables_cover_their_whole_vocabulary():
    assert set(voice_loop.VOICE_EFFECT_TABLE) == voice_loop.ALLOWED_TOOLS
    assert set(mind.MIND_EFFECT_TABLE) == mind.VALID_ACTIONS


def test_override_mechanism_stays_narrow():
    """One member, because bin/tool-quiet is three programs under one name. A
    second one means splitting that tool, not describing it more cleverly."""
    assert set(voice_loop.VOICE_EFFECT_OVERRIDES) == {"tool_quiet"}


# ---------------------------------------------------------------------------
# The sink itself.
#
# Everything above pins a *dispatcher*. These pin bin/tool-voice, which is the
# final common boundary every speech producer funnels through — tool-chat,
# tool-chat-vixen, tool-voice-persona, px-cron-say, px-battery-poll and both
# dispatchers all end here. Anything holding a shell reaches it directly,
# including the resident spark-brain session, whose tool envelope is SPARK's
# whole bin/ directory. Before this gate existed, only prose stopped it.
#
# The proof of silence is a canary in place of the speaker: PX_VOICE_PLAYER
# names a script that touches a marker file. A leak through the gate leaves
# the marker behind, so "no audio" is asserted against an artefact rather than
# against tool-voice's own self-report.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_VOICE = REPO_ROOT / "bin" / "tool-voice"

# Deterministic night-window bounds. `hour >= 0` is always true, `hour >= 99`
# never is, so these pin the rule regardless of when the suite runs.
ALWAYS_NIGHT = {"PX_NIGHT_SILENCE_START_H": "0", "PX_NIGHT_SILENCE_END_H": "24"}
NEVER_NIGHT = {"PX_NIGHT_SILENCE_START_H": "99", "PX_NIGHT_SILENCE_END_H": "0"}


@pytest.fixture
def sink(tmp_path):
    """Invoke bin/tool-voice the way something outside the loop would.

    Every PX_* variable is stripped from the inherited environment first: this
    is deliberately NOT the env voice_loop or mind.py hands down, because the
    bypass being closed is exactly a call that never passed through either.
    """
    import json as _json
    import os as _os
    import subprocess as _subprocess

    log_dir = tmp_path / "logs"
    state_dir = tmp_path / "state"
    log_dir.mkdir()
    state_dir.mkdir()
    marker = tmp_path / "spoke"

    canary = tmp_path / "canary-player"
    canary.write_text('#!/usr/bin/env bash\nprintf "%s" "$*" > "$PX_CANARY_MARKER"\n')
    canary.chmod(0o755)

    base = {k: v for k, v in _os.environ.items()
            if not k.startswith("PX_") and not k.startswith("_PX_")}
    base.update({
        "LOG_DIR": str(log_dir),
        "PX_STATE_DIR": str(state_dir),
        "PX_SESSION_PATH": str(state_dir / "session.json"),
        "PX_BYPASS_SUDO": "1",
        "PX_DRY": "0",                       # live path — the one that can speak
        "PX_VOICE_PLAYER": str(canary),
        "PX_CANARY_MARKER": str(marker),
        # Personas would otherwise reach the real GLaDOS TTS on this Pi.
        "PX_TTS_GREMLIN": "http://127.0.0.1:9",
        "PX_TTS_VIXEN": "http://127.0.0.1:9",
        "PX_TTS_SPARK": "http://127.0.0.1:9",
    })
    base.update(NEVER_NIGHT)

    def run(text="testing one two", *, session=None, awareness=None, **env_overrides):
        if session is not None:
            (state_dir / "session.json").write_text(_json.dumps(session))
        if awareness is not None:
            (state_dir / "awareness.json").write_text(_json.dumps(awareness))
        env = dict(base, PX_TEXT=text, **env_overrides)
        proc = _subprocess.run(
            [str(TOOL_VOICE)], cwd=str(REPO_ROOT), env=env,
            capture_output=True, text=True, check=False, timeout=90,
        )
        # A sink that dies mid-run prints no JSON. That must stay
        # distinguishable from a sink that suppressed, and it must not be able
        # to hide an artefact: the canary is checked against the filesystem,
        # never against what the tool managed to say about itself.
        lines = proc.stdout.strip().splitlines()
        payload = _json.loads(lines[-1]) if lines else None
        return payload, marker.exists()

    return run


def test_direct_tool_voice_is_silent_under_quiet_mode(sink):
    payload, spoke = sink(session={"spark_quiet_mode": True})
    assert payload["status"] == "suppressed"
    assert payload["reason"] == "quiet_mode"
    assert spoke is False


def test_direct_tool_voice_is_silent_during_night_silence(sink):
    payload, spoke = sink(**ALWAYS_NIGHT)
    assert payload["status"] == "suppressed"
    assert payload["reason"] == "night_silence"
    assert spoke is False


def test_direct_tool_voice_is_silent_while_adrian_is_on_call(sink):
    payload, spoke = sink(awareness={"ha_context": {"adrian_on_call": True}})
    assert payload["status"] == "suppressed"
    assert payload["reason"] == "on_call"
    assert spoke is False


def test_direct_tool_voice_is_silent_while_the_mic_is_hot(sink):
    payload, spoke = sink(awareness={"ha_context": {"adrian_mic_active": True}})
    assert payload["status"] == "suppressed"
    assert payload["reason"] == "on_call"
    assert spoke is False


def test_direct_tool_voice_still_speaks_when_policy_allows(sink):
    """The gate must not be a mute button. Allowed audio still reaches the player."""
    payload, spoke = sink("hello there")
    assert payload["status"] == "ok"
    assert spoke is True


def test_persona_routing_cannot_bypass_the_sink_gate(sink):
    """A persona reaches the speaker via bin/tool-voice-persona, which re-enters
    tool-voice. The gate therefore has to sit *before* the reroute, or a persona
    turn is evaluated only on the second pass — and the wrapper has already run
    an Ollama call and a network TTS fetch by then."""
    payload, spoke = sink(
        session={"spark_quiet_mode": True, "persona": "gremlin"},
        PX_PERSONA="gremlin",
    )
    assert payload["status"] == "suppressed"
    assert spoke is False
    assert "rephrased" not in payload, "tool-voice-persona ran before the gate"


def test_persona_audio_still_works_when_policy_allows(sink):
    payload, spoke = sink(
        "persona line", session={"persona": "gremlin"},
        PX_PERSONA="gremlin", _PX_VOICE_PERSONA_DONE="1",
    )
    assert payload["status"] == "ok"
    assert spoke is True


def test_dry_run_is_still_gated(sink):
    """Dry-run must model the live decision, not route around it — otherwise
    every dry test of a speaking path asserts behaviour the robot won't show."""
    payload, spoke = sink(session={"spark_quiet_mode": True}, PX_DRY="1")
    assert payload["status"] == "suppressed"
    assert spoke is False


# ---------------------------------------------------------------------------
# "Unknown" is not "not quiet".
#
# policy_context used to return a bare {} when the session could not be read,
# and {} reads as quiet-mode-off — so a contended lock, a permission error or
# an unreadable session file silently granted permission to speak. The bad
# case is not hypothetical: quiet mode is the dysregulation protocol, so the
# utterance a failed read buys is one during a meltdown.
#
# The contract now separates the two facts, and the rule resolves the missing
# one in the only direction a sink can justify: no evidence, no audio.
# ---------------------------------------------------------------------------

def test_unreadable_session_blocks_audio_on_both_origins():
    """Quiet mode binds both origins, so its indeterminate case must too.

    Pins the rule directly rather than through a dispatcher because the whole
    point of rule 0 is that it protects the caller which has no dispatcher.
    """
    for origin in ("interactive", "autonomous"):
        verdict = policy.evaluate(
            "tool_voice", {"text": "hi"}, effect="audio", origin=origin,
            session={}, awareness={}, now=DAY_TS, session_available=False,
        )
        assert verdict.allowed is False, origin
        assert verdict.reason == "session_unavailable", origin


def test_an_empty_but_readable_session_still_permits_audio():
    """The other half of the distinction, and the one a fail-closed change can
    break silently: {} must keep meaning 'read it, quiet mode is off'. If this
    fails, the gate has become a mute button."""
    verdict = policy.evaluate(
        "tool_voice", {"text": "hi"}, effect="audio", origin="interactive",
        session={}, awareness={}, now=DAY_TS,
    )
    assert verdict.allowed is True
    assert verdict.reason == "ok"


def test_an_unreadable_session_does_not_block_a_silent_action():
    """Rule 0 suppresses audio, not everything. A presence substitute is what
    the dispatchers fall back to, so if an unreadable session blocked that too
    there would be nothing left to downgrade to."""
    verdict = policy.evaluate(
        "tool_emote", {"name": "idle"}, effect="presence", origin="interactive",
        session={}, awareness={}, now=DAY_TS, session_available=False,
    )
    assert verdict.allowed is True


@pytest.mark.parametrize("exc", [
    FileLockTimeout("session.json.lock"),
    PermissionError("session.json"),
    IsADirectoryError("session.json"),
    ValueError("session.json is not an object"),
    RuntimeError("filelock is not installed"),
])
def test_policy_context_reports_a_failed_session_read_as_unavailable(monkeypatch, exc):
    """Every failure class, not just the contended lock the old comment named.

    RuntimeError is in the list on purpose: the broad except in the loader is
    only defensible while failing closed, so this pins that an unanticipated
    failure suppresses rather than escaping as a traceback or, worse, being
    reported as a successful read of an empty session.
    """
    def _raise():
        raise exc
    monkeypatch.setattr(policy_context, "load_session", _raise)
    read = policy_context.load_session_for_policy()
    assert read.available is False
    assert read.data == {}


def test_policy_context_reports_a_successful_read_as_available(monkeypatch):
    monkeypatch.setattr(policy_context, "load_session", lambda: {"spark_quiet_mode": False})
    read = policy_context.load_session_for_policy()
    assert read.available is True
    assert read.data == {"spark_quiet_mode": False}


def test_direct_tool_voice_is_silent_when_the_session_cannot_be_read(sink, tmp_path):
    """The regression, end to end through the real sink.

    PX_SESSION_PATH names a directory, so the read fails inside pxh.state with
    a real OSError rather than a patched one — this is a subprocess, and the
    bypass being closed is a caller that shares no interpreter with the suite.
    The canary proves the silence: nothing reached the player.
    """
    payload, spoke = sink(PX_SESSION_PATH=str(tmp_path / "state"))
    assert spoke is False, "audio reached the player on an unreadable session"
    assert payload is not None, "the sink died instead of suppressing"
    assert payload["status"] == "suppressed"
    assert payload["reason"] == "session_unavailable"


def test_direct_tool_voice_is_silent_while_the_session_lock_is_held(sink, tmp_path):
    """The exact scenario the old fail-open posture was written for.

    Takes ~10s: the test holds the session FileLock across the subprocess run,
    so the sink's read waits out pxh.state.LOCK_TIMEOUT_S and fails. Worth the
    wall clock, because "contended lock" was the argument for failing open and
    this is the case that argument was making. Note the sink returns while the
    lock is still held, which is also the answer to that argument: tool-voice
    calls update_session() on this same lock a few lines further down, so the
    old posture never actually bought a spoken turn under contention.
    """
    from filelock import FileLock

    session = tmp_path / "state" / "session.json"
    session.write_text("{}")
    with FileLock(str(session) + ".lock"):
        payload, spoke = sink()
    assert spoke is False, "audio reached the player while the session lock was held"
    assert payload is not None, "the sink died instead of suppressing"
    assert payload["status"] == "suppressed"
    assert payload["reason"] == "session_unavailable"


def test_direct_tool_voice_speaks_when_the_session_reads_empty(sink):
    """Companion to the two above: a session that reads fine and sets nothing
    is a known 'not quiet', and must still reach the speaker."""
    payload, spoke = sink(session={})
    assert payload["status"] == "ok"
    assert spoke is True


# ---------------------------------------------------------------------------
# The audio-producer inventory.
#
# test_no_tool_can_speak_under_an_innocent_name (above) asks "is this tool
# classified loudly enough?" — it starts from the dispatcher's vocabulary and
# looks down. It cannot see a producer that no dispatcher names, which is
# precisely the shape of the bypass this section exists for.
#
# So this one runs the other way: start from the audio primitives themselves
# and require every file that reaches one to be listed here with a disposition.
# The point is not that "ungated" entries are acceptable — several are known
# gaps, recorded as such. The point is that adding a seventeenth producer
# fails the suite until somebody decides which column it belongs in.
#
# Still a text scan, with the same honest limits as its neighbour: it cannot
# see a runtime-assembled command, a helper two imports deep, or a primitive
# spelled through a variable. It catches the common accidental case.
# ---------------------------------------------------------------------------

AUDIO_PRIMITIVE = re.compile(
    r"\b(?:aplay|paplay|espeak|mpg123|afplay|ffplay)\b"
    r"|/synthesize\b|/announce\b|enable_speaker\b"
)

# path -> (disposition, why)
#
#   gated        — evaluates pxh.policy itself before making a sound
#   self-gated   — enforces its own subset of the rules at its own chokepoint
#   delegates    — reaches the speaker only through bin/tool-voice
#   ungated      — makes a sound without consulting policy. A known gap.
#   mention      — names a primitive in a comment or passes a device through
#   diagnostic   — human-run tooling, never on an autonomous path
#   server       — synthesises audio for someone else to play; plays nothing
AUDIO_PRODUCERS: dict[str, tuple[str, str]] = {
    "bin/tool-voice": (
        "gated", "the sink; every speech producer funnels here"),
    "bin/tool-announce": (
        "self-gated", "night silence at the relay chokepoint; quiet mode and "
                      "on-call are not enforced here yet"),
    "bin/tool-voice-persona": (
        "delegates", "rephrases via Ollama, then re-enters tool-voice"),
    "bin/tool-chat-vixen": (
        "delegates", "VIXEN reply text goes out through tool-voice"),
    "bin/px-battery-poll": (
        "delegates", "speech via tool-voice; its plug/unplug sweep is a raw "
                     "aplay tone and is not gated"),
    "bin/tool-play-sound": (
        "ungated", "plays a bundled WAV with aplay directly"),
    "bin/px-perform": (
        "ungated", "own espeak->aplay pipeline for choreographed routines"),
    "bin/px-wake-listen": (
        "ungated", "wake/ack/timeout chimes, generated and played inline"),
    "src/pxh/wander.py": (
        "ungated", "_speak() runs its own espeak->aplay while exploring"),
    "src/pxh/mind.py": (
        "ungated", "_play_alarm_beeps() for the battery emergency; every other "
                   "audio route in this module goes through a tool and is "
                   "evaluated by expression()"),
    "bin/px-mic-check": ("diagnostic", "chirp-train loopback regression test"),
    "bin/px-voice-test": ("diagnostic", "manual voice check"),
    "bin/px-voice-sampler": ("diagnostic", "manual espeak parameter sweep"),
    "bin/tts-glados-server": ("server", "synthesises WAV bytes, plays nothing"),
    "bin/px-env": ("mention", "comment explaining the PULSE_SERVER fix"),
    "bin/run-wake": ("mention", "forwards --aplay-device to px-wake-listen"),
    "src/pxh/speaker_amp.py": (
        "helper", "enables GPIO20 only — no aplay/espeak call of its own; "
                  "shared by tool-voice (gated) and px-wake-listen (ungated), "
                  "each of which is evaluated under its own entry above"),
}


def _discover_audio_producers() -> set[str]:
    found = set()
    for directory in ("bin", "src/pxh"):
        for path in sorted((REPO_ROOT / directory).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if AUDIO_PRIMITIVE.search(text):
                found.add(str(path.relative_to(REPO_ROOT)))
    return found


def test_every_audio_producer_is_inventoried():
    """A new file that can make a sound must be classified, not merely added."""
    discovered = _discover_audio_producers()
    assert discovered == set(AUDIO_PRODUCERS), (
        f"unclassified audio producers: {sorted(discovered - set(AUDIO_PRODUCERS))}; "
        f"inventoried but gone: {sorted(set(AUDIO_PRODUCERS) - discovered)}"
    )


def test_the_sink_is_the_only_gated_producer_and_it_still_evaluates_policy():
    """Pins the gate's existence in the file itself.

    The subprocess tests above prove the gate *works*; this proves it is still
    *there* in a form a reader can find. Both matter: an evolution PR that
    deleted the call and adjusted the whitelisted tests to match would be
    caught here, in a file px-evolve cannot touch.
    """
    gated = [p for p, (kind, _) in AUDIO_PRODUCERS.items() if kind == "gated"]
    assert gated == ["bin/tool-voice"]
    source = (REPO_ROOT / "bin" / "tool-voice").read_text(encoding="utf-8")
    assert "policy_context" in source
    assert "evaluate_audio_sink(" in source


def test_delegating_producers_really_do_route_through_the_sink():
    """'delegates' is a claim about behaviour, so it gets checked rather than
    trusted — otherwise the inventory launders a direct producer into a safe
    column by relabelling it."""
    for path, (kind, _) in AUDIO_PRODUCERS.items():
        if kind != "delegates":
            continue
        source = (REPO_ROOT / path).read_text(encoding="utf-8")
        assert "tool-voice" in source, path


# ---------------------------------------------------------------------------
# Wake grant — a *capability*, not a bypass.
#
# "Hey Spark" permits SPARK to answer the person who deliberately summoned
# him. That is the whole of it. The grant may unblock an audible interactive
# reply that night silence, quiet mode or on-call suppression would otherwise
# have swallowed; it may not widen anything else, and it may not survive the
# conversation that opened it.
#
# Two properties get disproportionate attention here because both have already
# failed on this hardware:
#
#   * The Pi has no RTC and the clock has been observed stepping ~49 minutes
#     during boot as NTP corrects it. A grant whose lifetime were measured in
#     wall time would therefore expire instantly or last most of an hour,
#     depending on the direction of the step. Validity is bound to
#     CLOCK_BOOTTIME instead, and the tests below prove wall time is not
#     merely unused but *unreachable* from the validity path.
#
#   * The sink is the last boundary before the speaker and cannot trust its
#     caller — that is why it exists. So the grant is a fact it reads for
#     itself, never an argument or an environment variable it is handed.
# ---------------------------------------------------------------------------

import json as _json
import os as _os
import time as _time

from pxh import wake_grant


@pytest.fixture
def grant_dir(tmp_path, monkeypatch):
    """Point the grant at a tmp runtime dir, so no test can see the real one."""
    d = tmp_path / "wake"
    d.mkdir()
    monkeypatch.setenv(wake_grant.GRANT_DIR_ENV, str(d))
    return d


# --- lifetime is bound to boot, never to the wall clock ---------------------

def test_a_fresh_grant_is_active(grant_dir):
    assert wake_grant.open_grant() is not None
    assert wake_grant.is_grant_active() is True


def test_a_grant_expires_on_boottime(grant_dir, monkeypatch):
    wake_grant.open_grant(ttl_s=180.0)
    later = wake_grant.boottime() + 181.0
    monkeypatch.setattr(wake_grant, "boottime", lambda: later)
    assert wake_grant.is_grant_active() is False


def test_grant_validity_cannot_reach_the_wall_clock(grant_dir, monkeypatch):
    """The strong form: not 'does not use time.time()' but 'could not'.

    A grant is opened first, while the clock still works, because opening
    records a diagnostic UTC stamp. Everything after that point must survive
    a wall clock that raises on contact.
    """
    wake_grant.open_grant()

    def _explode():  # pragma: no cover - the point is that it never runs
        raise AssertionError("policy validity consulted the wall clock")

    monkeypatch.setattr(_time, "time", _explode)
    assert wake_grant.is_grant_active() is True


@pytest.mark.parametrize("jump_s", [+3600.0, -3600.0, +49 * 60.0, -49 * 60.0])
def test_wall_clock_jumps_do_not_change_grant_lifetime(grant_dir, monkeypatch, jump_s):
    """NTP stepping the clock at boot must not lengthen or kill a grant."""
    wake_grant.open_grant(ttl_s=180.0)
    real_time = _time.time
    monkeypatch.setattr(_time, "time", lambda: real_time() + jump_s)
    assert wake_grant.is_grant_active() is True

    # ...and it still expires exactly one TTL of *boot* time later.
    later = wake_grant.boottime() + 181.0
    monkeypatch.setattr(wake_grant, "boottime", lambda: later)
    assert wake_grant.is_grant_active() is False


def test_a_grant_from_a_previous_boot_is_invalid(grant_dir):
    """Reboot invalidates the grant even if the file somehow outlives tmpfs.

    The document is produced by the real opener and only the boot id is
    altered, so this pins the check rather than a hand-built fixture that
    could drift from what open_grant() actually writes.
    """
    wake_grant.open_grant()
    doc = _json.loads(wake_grant.grant_path().read_text())
    doc["boot_id"] = "00000000-0000-0000-0000-000000000000"
    wake_grant.grant_path().write_text(_json.dumps(doc))
    assert wake_grant.is_grant_active() is False


def test_a_grant_with_no_boot_id_is_invalid(grant_dir):
    wake_grant.open_grant()
    doc = _json.loads(wake_grant.grant_path().read_text())
    del doc["boot_id"]
    wake_grant.grant_path().write_text(_json.dumps(doc))
    assert wake_grant.is_grant_active() is False


# --- fail closed ------------------------------------------------------------

def test_a_missing_grant_is_inactive(grant_dir):
    assert wake_grant.is_grant_active() is False


def test_a_missing_runtime_directory_is_inactive(tmp_path, monkeypatch):
    monkeypatch.setenv(wake_grant.GRANT_DIR_ENV, str(tmp_path / "nope"))
    assert wake_grant.is_grant_active() is False


@pytest.mark.parametrize("blob", ["", "{", "null", "[]", '{"boot_id": 3}', "not json at all"])
def test_a_corrupt_grant_is_inactive(grant_dir, blob):
    wake_grant.grant_path().write_text(blob)
    assert wake_grant.is_grant_active() is False


def test_a_grant_that_expires_before_it_opens_is_inactive(grant_dir):
    """Nonsense ordering is treated as corruption, not as a very long window."""
    wake_grant.open_grant()
    doc = _json.loads(wake_grant.grant_path().read_text())
    doc["expires_boottime"] = doc["opened_boottime"] - 1.0
    wake_grant.grant_path().write_text(_json.dumps(doc))
    assert wake_grant.is_grant_active() is False


def test_the_grant_never_falls_back_to_durable_state(grant_dir, tmp_path):
    """Unlike the heartbeat, this file must not survive a power cut.

    pxh.runtime_paths deliberately falls back to state/ on hosts without
    /run/spark. That is right for a heartbeat and wrong for a grant, so the
    grant resolves its own path and the two must not be confused.
    """
    assert wake_grant.grant_path().parent == grant_dir
    assert "state" not in wake_grant.grant_path().parts


# --- the window is opened by a summons and closed by silence ----------------

def test_refresh_extends_only_the_conversation_that_owns_the_grant(grant_dir, monkeypatch):
    cid = wake_grant.open_grant(ttl_s=180.0)
    doc = _json.loads(wake_grant.grant_path().read_text())
    opened, before = doc["opened_boottime"], doc["expires_boottime"]

    monkeypatch.setattr(wake_grant, "boottime", lambda: opened + 10.0)
    assert wake_grant.refresh_grant("some-other-conversation") is False
    unchanged = _json.loads(wake_grant.grant_path().read_text())["expires_boottime"]
    assert unchanged == before

    assert wake_grant.refresh_grant(cid) is True
    extended = _json.loads(wake_grant.grant_path().read_text())["expires_boottime"]
    assert extended > before


def test_refresh_cannot_revive_an_expired_grant(grant_dir, monkeypatch):
    """Inactivity closes the window; a late turn does not reopen it.

    Only a fresh 'Hey Spark' may originate a grant, so a conversation that has
    already lapsed has to be summoned again rather than resumed.
    """
    cid = wake_grant.open_grant(ttl_s=180.0)
    opened = _json.loads(wake_grant.grant_path().read_text())["opened_boottime"]
    monkeypatch.setattr(wake_grant, "boottime", lambda: opened + 181.0)
    assert wake_grant.refresh_grant(cid) is False
    assert wake_grant.is_grant_active() is False


def test_refresh_cannot_extend_a_conversation_forever(grant_dir, monkeypatch):
    """Turns extend the window; they do not make it unbounded.

    Without this, a room with a television in it holds the gate open all
    night one legitimate-looking turn at a time.
    """
    cid = wake_grant.open_grant(ttl_s=180.0)
    opened = _json.loads(wake_grant.grant_path().read_text())["opened_boottime"]

    # Refresh diligently, every 60s, well inside the TTL each time.
    t = opened
    while t < opened + wake_grant.MAX_CONVERSATION_S - 60.0:
        t += 60.0
        monkeypatch.setattr(wake_grant, "boottime", lambda t=t: t)
        assert wake_grant.refresh_grant(cid) is True

    t = opened + wake_grant.MAX_CONVERSATION_S + 1.0
    monkeypatch.setattr(wake_grant, "boottime", lambda: t)
    assert wake_grant.refresh_grant(cid) is False
    assert wake_grant.is_grant_active() is False


def test_a_new_summons_starts_a_new_conversation(grant_dir):
    first = wake_grant.open_grant()
    second = wake_grant.open_grant()
    assert first != second
    assert wake_grant.refresh_grant(first) is False
    assert wake_grant.refresh_grant(second) is True


def test_closing_the_grant_ends_the_window(grant_dir):
    cid = wake_grant.open_grant()
    wake_grant.close_grant(cid)
    assert wake_grant.is_grant_active() is False


def test_closing_someone_elses_grant_is_refused(grant_dir):
    """A stale conversation ending must not silence the one that replaced it."""
    wake_grant.open_grant()
    second = wake_grant.open_grant()
    wake_grant.close_grant("a-stale-conversation")
    assert wake_grant.is_grant_active() is True
    wake_grant.close_grant(second)
    assert wake_grant.is_grant_active() is False


# --- what the grant permits, and what it must not -------------------------

def _verdict(*, wake, origin="interactive", effect="audio", session=None,
             awareness=None, now=NIGHT_TS, session_available=True):
    return policy.evaluate(
        "tool_voice", {"text": "hello"}, effect=effect, origin=origin,
        session={} if session is None else session,
        awareness={} if awareness is None else awareness,
        now=now, session_available=session_available, wake_grant=wake,
    )


def test_wake_grant_permits_an_audible_reply_during_night_silence():
    assert _verdict(wake=False).reason == "night_silence"
    v = _verdict(wake=True)
    assert v.allowed is True
    assert v.reason == "wake_grant"


def test_wake_grant_permits_an_audible_reply_during_quiet_mode():
    session = {"spark_quiet_mode": True}
    assert _verdict(wake=False, session=session, now=DAY_TS).reason == "quiet_mode"
    assert _verdict(wake=True, session=session, now=DAY_TS).allowed is True


def test_wake_grant_permits_an_audible_reply_while_adrian_is_on_call():
    aw = {"ha_context": {"adrian_on_call": True}}
    assert _verdict(wake=False, awareness=aw, now=DAY_TS).reason == "on_call"
    assert _verdict(wake=True, awareness=aw, now=DAY_TS).allowed is True


def test_wake_grant_does_not_mutate_the_underlying_state():
    """Quiet mode stays *set*. The conversation speaks through it; it does not
    end it. bin/tool-quiet remains the only exit."""
    session = {"spark_quiet_mode": True}
    _verdict(wake=True, session=session, now=DAY_TS)
    assert session == {"spark_quiet_mode": True}


def test_wake_grant_does_not_help_autonomous_speech():
    """A reflection at 3am is not a summons, whoever else is talking."""
    v = _verdict(wake=True, origin="autonomous", session={"spark_quiet_mode": True},
                 now=NIGHT_TS)
    assert v.allowed is False
    assert v.reason == "quiet_mode"


def test_wake_grant_cannot_override_an_unreadable_session():
    """Rule 0 is about evidence, not state. A grant is not evidence that the
    dysregulation protocol is not running — it only says someone spoke."""
    v = _verdict(wake=True, session_available=False)
    assert v.allowed is False
    assert v.reason == "session_unavailable"


@pytest.mark.parametrize("effect", ["presence", "other"])
@pytest.mark.parametrize("origin", ["interactive", "autonomous"])
def test_wake_grant_never_decides_a_non_audio_verdict(effect, origin):
    """Direct wake permits an answer; it confers no other authority.

    Swept as a cross-product rather than asserted on one call, because the
    claim is about the whole surface: outside interactive audio, the grant
    must be inert — the verdict with it is the verdict without it.
    """
    without = _verdict(wake=False, effect=effect, origin=origin,
                       session={"spark_quiet_mode": True})
    with_ = _verdict(wake=True, effect=effect, origin=origin,
                     session={"spark_quiet_mode": True})
    assert with_ == without
    assert with_.reason != "wake_grant"


def test_wake_grant_never_decides_an_autonomous_audio_verdict():
    with_ = _verdict(wake=True, origin="autonomous")
    without = _verdict(wake=False, origin="autonomous")
    assert with_ == without
    assert with_.reason != "wake_grant"


def test_the_dispatcher_consults_the_grant_it_loads_rather_than_its_caller(monkeypatch):
    """voice_loop must not accept wake-ness from the model or the environment.

    Pins the call site: the dispatcher asks policy_context for the fact, so an
    evolution PR that deleted the lookup and read PX_* instead would fail here
    rather than in a whitelisted test it could edit to match.
    """
    monkeypatch.setattr(voice_loop, "_policy_now", lambda: NIGHT_TS)
    monkeypatch.setattr(policy_context, "wake_grant_active", lambda: False)
    tool, _ = voice_loop.validate_action({"tool": "tool_voice", "params": {"text": "hi"}})
    assert tool != "tool_voice"

    monkeypatch.setattr(policy_context, "wake_grant_active", lambda: True)
    tool, _ = voice_loop.validate_action({"tool": "tool_voice", "params": {"text": "hi"}})
    assert tool == "tool_voice"


# --- the fallback acknowledgement needs no wake-grant precondition of its own
#
# #262-class regression: a resident-brain timeout is exactly the delay that
# can age a live wake grant out, so requiring a *fresh* wake_grant_active()
# read before the deterministic ack would turn the one timeout it exists to
# answer into silence. It must still be gated exactly like any other
# interactive reply — quiet mode and night silence still apply — so the ack
# is neither exempt from policy nor held to a stricter bar than normal speech.

def test_acknowledgement_needs_no_wake_grant_when_policy_otherwise_allows(monkeypatch):
    monkeypatch.setattr(voice_loop, "_policy_now", lambda: DAY_TS)
    monkeypatch.setattr(voice_loop, "load_session", lambda: {})
    monkeypatch.setattr(voice_loop, "_load_awareness_for_policy", lambda: {})
    monkeypatch.setattr(policy_context, "wake_grant_active", lambda: False)

    spoken = []
    monkeypatch.setattr(
        voice_loop, "execute_tool",
        lambda tool, env, dry: spoken.append((tool, env)) or (0, "", ""),
    )

    assert voice_loop.acknowledge_unavailable(dry_run=True) is True
    assert len(spoken) == 1
    tool, _ = spoken[0]
    assert tool == "tool_voice"


def test_acknowledgement_is_still_downgraded_by_quiet_mode_without_a_grant(monkeypatch):
    """Not a blanket bypass: absent a live grant, the ack is downgraded to the
    same presence-safe substitute any other blocked interactive speech gets —
    it does not get the grant's exemption for free."""
    monkeypatch.setattr(voice_loop, "_policy_now", lambda: DAY_TS)
    monkeypatch.setattr(voice_loop, "load_session", lambda: {"spark_quiet_mode": True})
    monkeypatch.setattr(voice_loop, "_load_awareness_for_policy", lambda: {})
    monkeypatch.setattr(policy_context, "wake_grant_active", lambda: False)

    spoken = []
    monkeypatch.setattr(
        voice_loop, "execute_tool",
        lambda tool, env, dry: spoken.append((tool, env)) or (0, "", ""),
    )

    voice_loop.acknowledge_unavailable(dry_run=True)
    assert len(spoken) == 1
    tool, _ = spoken[0]
    assert tool != "tool_voice", "quiet mode must still silence the ack"


def test_acknowledgement_is_still_downgraded_by_night_silence_without_a_grant(monkeypatch):
    monkeypatch.setattr(voice_loop, "_policy_now", lambda: NIGHT_TS)
    monkeypatch.setattr(voice_loop, "load_session", lambda: {})
    monkeypatch.setattr(voice_loop, "_load_awareness_for_policy", lambda: {})
    monkeypatch.setattr(policy_context, "wake_grant_active", lambda: False)

    spoken = []
    monkeypatch.setattr(
        voice_loop, "execute_tool",
        lambda tool, env, dry: spoken.append((tool, env)) or (0, "", ""),
    )

    voice_loop.acknowledge_unavailable(dry_run=True)
    assert len(spoken) == 1
    tool, _ = spoken[0]
    assert tool != "tool_voice", "night silence must still silence the ack"


def test_a_timed_out_voice_turn_acknowledges_exactly_once_without_a_grant(monkeypatch):
    """End-to-end shape of the fix, at the real supervisor_loop call site: a
    resident-brain timeout must produce exactly one deterministic
    acknowledgement and stop — not loop back to re-ask a brain already
    established unavailable — even though the wake grant that opened the turn
    reads inactive by the time the timeout is handled."""
    monkeypatch.setattr(voice_loop, "_policy_now", lambda: DAY_TS)
    monkeypatch.setattr(voice_loop, "load_session", lambda: {})
    monkeypatch.setattr(voice_loop, "_load_awareness_for_policy", lambda: {})
    monkeypatch.setattr(policy_context, "wake_grant_active", lambda: False)

    monkeypatch.setattr(voice_loop, "ensure_session", lambda: None)
    texts = iter(["are you there?"])
    monkeypatch.setattr(voice_loop, "capture_text_input", lambda: next(texts, None))

    turn_calls = []
    monkeypatch.setattr(
        voice_loop, "run_voice_turn",
        lambda prompt, **kw: turn_calls.append(prompt) or
        (voice_loop.VOICE_BRAIN_UNAVAILABLE, "", "resident brain unavailable"),
    )

    acked = []
    monkeypatch.setattr(
        voice_loop, "acknowledge_unavailable",
        lambda dry_run=False: acked.append(dry_run) or True,
    )

    args = voice_loop.parse_args([
        "--backend", "brain", "--max-turns", "5",
        "--input-mode", "text", "--dry-run",
    ])
    voice_loop.supervisor_loop(args)

    assert len(turn_calls) == 1, "a saturated brain must not be re-asked"
    assert acked == [True]


# --- the sink, end to end, against a canary rather than a self-report -------

def test_hey_spark_at_three_am_gets_an_audible_reply(sink, tmp_path):
    d = tmp_path / "wake-3am"
    d.mkdir()
    _os.environ[wake_grant.GRANT_DIR_ENV] = str(d)
    try:
        wake_grant.open_grant()
    finally:
        del _os.environ[wake_grant.GRANT_DIR_ENV]
    payload, spoke = sink(session={}, **{wake_grant.GRANT_DIR_ENV: str(d)}, **ALWAYS_NIGHT)
    assert payload["status"] == "ok"
    assert spoke is True


def test_hey_spark_during_quiet_mode_speaks_and_quiet_mode_remains_set(sink, tmp_path):
    d = tmp_path / "wake-quiet"
    d.mkdir()
    _os.environ[wake_grant.GRANT_DIR_ENV] = str(d)
    try:
        wake_grant.open_grant()
    finally:
        del _os.environ[wake_grant.GRANT_DIR_ENV]
    payload, spoke = sink(session={"spark_quiet_mode": True},
                          **{wake_grant.GRANT_DIR_ENV: str(d)})
    assert payload["status"] == "ok"
    assert spoke is True
    session_after = _json.loads((tmp_path / "state" / "session.json").read_text())
    assert session_after["spark_quiet_mode"] is True


def test_an_expired_grant_is_silent_again(sink, tmp_path):
    """Real expiry against the real monotonic clock — no fixture stands in for
    the thing under test."""
    d = tmp_path / "wake-expired"
    d.mkdir()
    _os.environ[wake_grant.GRANT_DIR_ENV] = str(d)
    try:
        wake_grant.open_grant(ttl_s=0.05)
    finally:
        del _os.environ[wake_grant.GRANT_DIR_ENV]
    _time.sleep(0.15)
    payload, spoke = sink(session={}, **{wake_grant.GRANT_DIR_ENV: str(d)}, **ALWAYS_NIGHT)
    assert payload["status"] == "suppressed"
    assert payload["reason"] == "night_silence"
    assert spoke is False


def test_a_previous_boot_grant_is_silent(sink, tmp_path):
    d = tmp_path / "wake-oldboot"
    d.mkdir()
    _os.environ[wake_grant.GRANT_DIR_ENV] = str(d)
    try:
        wake_grant.open_grant()
        doc = _json.loads(wake_grant.grant_path().read_text())
        doc["boot_id"] = "00000000-0000-0000-0000-000000000000"
        wake_grant.grant_path().write_text(_json.dumps(doc))
    finally:
        del _os.environ[wake_grant.GRANT_DIR_ENV]
    payload, spoke = sink(session={}, **{wake_grant.GRANT_DIR_ENV: str(d)}, **ALWAYS_NIGHT)
    assert payload["status"] == "suppressed"
    assert spoke is False


@pytest.mark.parametrize("claim", [
    {"PX_WAKE_GRANT": "1"},
    {"PX_WAKE_GRANT": "true"},
    {"PX_WAKE_GRANT_ACTIVE": "1"},
    {"PX_WAKE_TURN": "1"},
    {"PX_WAKE_WORD": "hey spark"},
    {"PX_ORIGIN": "wake"},
])
def test_no_environment_variable_can_manufacture_a_grant(sink, tmp_path, claim):
    """An env var may say *where* to look. Nothing it says is believed.

    This is the bypass the sink exists to close: anything holding a shell on
    this box — including SPARK's own resident brain session, whose tool
    envelope is the whole bin/ directory — can set variables.
    """
    empty = tmp_path / "wake-empty"
    empty.mkdir()
    payload, spoke = sink(session={}, **{wake_grant.GRANT_DIR_ENV: str(empty)},
                          **claim, **ALWAYS_NIGHT)
    assert payload["status"] == "suppressed"
    assert payload["reason"] == "night_silence"
    assert spoke is False


def test_the_sink_will_not_take_wake_ness_as_an_argument():
    """evaluate_audio_sink loads the grant itself, exactly as it pins origin
    and effect. A caller that could pass it in could argue its way past the
    last gate before the speaker."""
    import inspect
    params = inspect.signature(policy_context.evaluate_audio_sink).parameters
    assert "wake_grant" not in params
    source = (REPO_ROOT / "bin" / "tool-voice").read_text(encoding="utf-8")
    assert "wake_grant" not in source
