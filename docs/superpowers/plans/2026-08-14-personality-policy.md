# Personality as Executable Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `src/pxh/policy.py`, a tiny constitutional module that both `voice_loop.py` (interactive) and `mind.py` (autonomous) call before dispatching any action, so quiet mode, interactive night silence, and interactive on-call/hot-mic suppression hold regardless of prompt wording or active persona — and protect that guarantee from self-evolution.

**Architecture:** One pure function, `policy.evaluate(action, params, *, effect, origin, session, awareness, now)`, called from `voice_loop.py::validate_action()` and `mind.py::expression()`. Each caller classifies its own action vocabulary into `effect="audio"|"presence"|"other"` via an exhaustive local table; `policy.py` never learns tool or action names. `src/pxh/policy.py` and `tests/test_policy_invariants.py` are blacklisted from `px-evolve`.

**Tech Stack:** Python 3.11, pytest, existing `pxh` package conventions (dataclasses, `zoneinfo`).

**Spec:** `docs/superpowers/specs/2026-08-14-personality-policy-design.md`

## Global Constraints

- `src/pxh/policy.py` must not import anything from `pxh.mind` or `pxh.voice_loop` — dependency direction is `mind.py`/`voice_loop.py` → `policy.py`, never the reverse.
- `evaluate()` never inspects tool/action names itself — only the caller-supplied `effect` and `origin`.
- Effect-classification tables must be **exhaustive** against their dispatcher's real action set (`ALLOWED_TOOLS` for `voice_loop.py`, `VALID_ACTIONS` for `mind.py`) and **fail loudly** (`KeyError`, not a default) on an unclassified action.
- **No tool gets an audio exception.** A classification is a claim about what the script actually does, verified against its source — not about how innocent its name sounds. `tool_repair` and `tool_quiet` both shell out to `bin/tool-voice`, so both must be honestly classified; Task 3 refactors them so that honest classification doesn't lock SPARK in quiet mode.
- Effect classification is param-aware where the real effect is: `VOICE_EFFECT_OVERRIDES` (consulted before `VOICE_EFFECT_TABLE`) exists only for tools with distinct, separately-verifiable effect branches. `tool_quiet` is the only member in v1.
- A static test must scan every `bin/tool-*` for a reference to an audio-emitting tool (`tool-voice`, `tool-voice-persona`, `tool-announce`, `tool-play-sound`, `tool-chat*`, `px-perform`) and fail if any such tool is classified non-`"audio"` without an override that accounts for it.
- Preserve existing autonomous night-silence (`mind.py::_is_night_silence`, `NIGHT_ALLOWED_ACTIONS`) and autonomous on-call suppression (`mind.py:3084-3088`) exactly as they are — only their shared clock helper moves, not the enforcement rule.
- No volume control, no general rules engine, no changes to `docs/prompts/*.md` conversational style.
- `src/pxh/policy.py` and `tests/test_policy_invariants.py` must be added to `claude_session.BLACKLIST_FILES`.
- Every commit must follow TDD: failing test → minimal implementation → passing test → commit.

---

### Task 1: `policy.py` core module — verdict shape, night-hour helper, `evaluate()`

**Files:**
- Create: `src/pxh/policy.py`
- Test: `tests/test_policy.py`

**Interfaces:**
- Produces: `Origin = Literal["interactive", "autonomous"]`, `Effect = Literal["audio", "presence", "other"]`, `PolicyVerdict` dataclass (`allowed: bool`, `reason: str`, `suggest_presence_substitute: bool = False`), `is_night_hour(hour: int) -> bool`, `evaluate(action: str, params: dict, *, effect: Effect, origin: Origin, session: dict, awareness: dict, now: float, _depth: int = 0) -> PolicyVerdict`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_policy.py
import pytest
from pxh import policy


def test_is_night_hour_matches_bounds():
    # NIGHT_SILENCE_START_H=19, NIGHT_SILENCE_END_H=7 (spark_config.py)
    assert policy.is_night_hour(19) is True
    assert policy.is_night_hour(23) is True
    assert policy.is_night_hour(0) is True
    assert policy.is_night_hour(6) is True
    assert policy.is_night_hour(7) is False
    assert policy.is_night_hour(18) is False


def test_non_audio_effect_always_allowed():
    verdict = policy.evaluate(
        "anything", {}, effect="presence", origin="interactive",
        session={"spark_quiet_mode": True}, awareness={}, now=0.0,
    )
    assert verdict.allowed is True


def test_quiet_mode_blocks_audio_both_origins():
    for origin in ("interactive", "autonomous"):
        verdict = policy.evaluate(
            "tool_voice", {}, effect="audio", origin=origin,
            session={"spark_quiet_mode": True}, awareness={}, now=0.0,
        )
        assert verdict.allowed is False
        assert verdict.reason == "quiet_mode"
        assert verdict.suggest_presence_substitute is True


def test_night_silence_blocks_audio_interactive_only():
    # now=0.0 is epoch UTC; use a fixed known-night Hobart timestamp instead.
    import datetime as dt
    from zoneinfo import ZoneInfo
    night_ts = dt.datetime(2026, 1, 1, 22, 0, tzinfo=ZoneInfo("Australia/Hobart")).timestamp()

    interactive = policy.evaluate(
        "tool_voice", {}, effect="audio", origin="interactive",
        session={}, awareness={}, now=night_ts,
    )
    assert interactive.allowed is False
    assert interactive.reason == "night_silence"

    autonomous = policy.evaluate(
        "greet", {}, effect="audio", origin="autonomous",
        session={}, awareness={}, now=night_ts,
    )
    assert autonomous.allowed is True  # mind.py enforces this itself, not policy.py, in v1


def test_on_call_blocks_audio_interactive_only():
    day_ts = __import__("datetime").datetime(
        2026, 1, 1, 12, 0, tzinfo=__import__("zoneinfo").ZoneInfo("Australia/Hobart")
    ).timestamp()
    aw = {"ha_context": {"adrian_on_call": True}}

    interactive = policy.evaluate(
        "tool_voice", {}, effect="audio", origin="interactive",
        session={}, awareness=aw, now=day_ts,
    )
    assert interactive.allowed is False
    assert interactive.reason == "on_call"

    autonomous = policy.evaluate(
        "greet", {}, effect="audio", origin="autonomous",
        session={}, awareness=aw, now=day_ts,
    )
    assert autonomous.allowed is True


def test_allowed_when_no_condition_holds():
    day_ts = __import__("datetime").datetime(
        2026, 1, 1, 12, 0, tzinfo=__import__("zoneinfo").ZoneInfo("Australia/Hobart")
    ).timestamp()
    verdict = policy.evaluate(
        "tool_voice", {}, effect="audio", origin="interactive",
        session={}, awareness={}, now=day_ts,
    )
    assert verdict.allowed is True
    assert verdict.reason == "ok"


def test_substitute_reevaluation_cannot_recurse_past_depth_1():
    with pytest.raises(ValueError):
        policy.evaluate(
            "tool_emote", {}, effect="audio", origin="interactive",
            session={"spark_quiet_mode": True}, awareness={}, now=0.0,
            _depth=1,
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_policy.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pxh.policy'`

- [ ] **Step 3: Write the implementation**

```python
# src/pxh/policy.py
"""Behavioural policy — constitutional invariants that hold regardless of
which prompt, persona, or dispatcher proposed an action.

This module and tests/test_policy_invariants.py are blacklisted from
self-evolution (see pxh.claude_session.BLACKLIST_FILES). Callers classify
their own action vocabulary into an Effect before calling evaluate() —
this module never enumerates tool or autonomous-action names itself.
"""
import datetime as dt
from dataclasses import dataclass
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from pxh.spark_config import NIGHT_SILENCE_END_H, NIGHT_SILENCE_START_H

HOBART_TZ = ZoneInfo("Australia/Hobart")

Origin = Literal["interactive", "autonomous"]
Effect = Literal["audio", "presence", "other"]


@dataclass(frozen=True)
class PolicyVerdict:
    allowed: bool
    reason: str
    suggest_presence_substitute: bool = False


def is_night_hour(hour: int) -> bool:
    """True during the unconditional night-silence window (Hobart hour-of-day).

    Pure clock check, shared by mind.py::_is_night_silence (autonomous rule
    location unchanged) and policy.py's own interactive rule below.
    """
    return hour >= NIGHT_SILENCE_START_H or hour < NIGHT_SILENCE_END_H


def evaluate(
    action: str,
    params: dict,
    *,
    effect: Effect,
    origin: Origin,
    session: dict,
    awareness: dict,
    now: float,
    _depth: int = 0,
) -> PolicyVerdict:
    if effect != "audio":
        return PolicyVerdict(allowed=True, reason="effect_not_audio")

    if session.get("spark_quiet_mode") is True:
        if _depth >= 1:
            raise ValueError(
                "policy: quiet_mode blocked a presence substitute at "
                "_depth>=1 — a presence-safe action must never itself be "
                "classified effect='audio'"
            )
        return PolicyVerdict(
            allowed=False, reason="quiet_mode", suggest_presence_substitute=True
        )

    if origin == "interactive":
        hour = dt.datetime.fromtimestamp(now, tz=HOBART_TZ).hour
        if is_night_hour(hour):
            if _depth >= 1:
                raise ValueError(
                    "policy: night_silence blocked a presence substitute at "
                    "_depth>=1"
                )
            return PolicyVerdict(
                allowed=False, reason="night_silence", suggest_presence_substitute=True
            )

        ha_ctx = awareness.get("ha_context") or {}
        if ha_ctx.get("adrian_on_call") or ha_ctx.get("adrian_mic_active"):
            if _depth >= 1:
                raise ValueError(
                    "policy: on_call blocked a presence substitute at _depth>=1"
                )
            return PolicyVerdict(
                allowed=False, reason="on_call", suggest_presence_substitute=True
            )

    return PolicyVerdict(allowed=True, reason="ok")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_policy.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/pxh/policy.py tests/test_policy.py
git commit -m "feat(policy): add constitutional evaluate()/is_night_hour core (#174)"
```

---

### Task 2: Wire `policy.py` into `mind.py` — shared clock helper + autonomous quiet-mode rule

**Files:**
- Modify: `src/pxh/mind.py:163-165` (`_is_night_silence`), `src/pxh/mind.py` imports (top, near line 53), `src/pxh/mind.py::expression()` (starts at line 3030)
- Test: `tests/test_mind.py`, `tests/test_policy.py`

**Interfaces:**
- Consumes: `pxh.policy.is_night_hour(hour: int) -> bool`, `pxh.policy.evaluate(...) -> PolicyVerdict` from Task 1.
- Produces: `mind.MIND_EFFECT_TABLE: dict[str, Literal["audio","presence","other"]]` (module-level, one entry per member of `VALID_ACTIONS`), used by Task 6's integration tests.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_policy.py (add to existing file)
def test_mind_effect_table_is_exhaustive():
    from pxh import mind
    assert set(mind.MIND_EFFECT_TABLE.keys()) == mind.VALID_ACTIONS
    assert all(v in ("audio", "presence", "other") for v in mind.MIND_EFFECT_TABLE.values())


def test_mind_effect_table_classifies_known_audio_actions_as_audio():
    from pxh import mind
    for action in ("greet", "greet_arrival", "comment", "weather_comment",
                   "morning_fact", "scan", "look_at", "look_around",
                   "play_sound", "time_check", "calendar_check", "announce",
                   "message_obi"):
        assert mind.MIND_EFFECT_TABLE[action] == "audio", action


def test_is_night_silence_delegates_to_policy():
    from pxh import mind, policy
    for hour in range(24):
        assert mind._is_night_silence(hour) == policy.is_night_hour(hour)
```

`tests/test_mind.py` already imports `from unittest.mock import patch` (line 5) but not `Mock` — add `Mock` to that import line (`from unittest.mock import Mock, patch`) before adding the tests below.

```python
# tests/test_mind.py (add near the existing expression()/_quiet_daytime tests)
def test_expression_suppresses_audio_in_quiet_mode(monkeypatch):
    _quiet_daytime(monkeypatch)
    monkeypatch.setattr(mind, "load_session", lambda: {"persona": "", "spark_quiet_mode": True})
    calls = []
    monkeypatch.setattr(mind, "_run_voice", lambda env, label="": calls.append(label))
    aw = {"obi_mode": "active", "calendar": {}, "ha_context": {}}
    result = mind.expression({"action": "greet", "thought": "hello"}, dry=True, awareness=aw)
    assert result is False
    assert calls == []


def test_expression_allows_presence_action_in_quiet_mode(monkeypatch):
    _quiet_daytime(monkeypatch)
    monkeypatch.setattr(mind, "load_session", lambda: {"persona": "", "spark_quiet_mode": True})
    mock_run = Mock(return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr=""))
    monkeypatch.setattr(mind.subprocess, "run", mock_run)
    aw = {"obi_mode": "active", "calendar": {}, "ha_context": {}}
    result = mind.expression({"action": "emote", "thought": "curious"}, dry=True, awareness=aw)
    assert result is True
    assert mock_run.called
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_policy.py tests/test_mind.py -k "effect_table or delegates_to_policy or suppresses_audio_in_quiet_mode or allows_presence_action_in_quiet_mode" -v`
Expected: FAIL — `AttributeError: module 'pxh.mind' has no attribute 'MIND_EFFECT_TABLE'`, and quiet-mode suppression test fails because `_run_voice` is still called.

- [ ] **Step 3: Write the implementation**

In `src/pxh/mind.py`, add the import near the existing `from pxh.voice_loop import PERSONA_VOICE_ENV` line (~53):

```python
from pxh import policy
```

Replace the body of `_is_night_silence` (currently lines 163-165):

```python
def _is_night_silence(hour: int) -> bool:
    """True during the unconditional night-silence window (Hobart hour-of-day)."""
    return policy.is_night_hour(hour)
```

Add the effect table near `VALID_ACTIONS` (after line 353, right after the `VALID_ACTIONS` definition):

```python
# Exhaustive per action name in VALID_ACTIONS — deliberately not a default,
# so a new autonomous action fails test_mind_effect_table_is_exhaustive until
# someone classifies it. Classification follows the real dispatch: scan,
# look_at and look_around are "audio" because their branches call _run_voice
# (mind.py:3174, 3193, 3355), not because of what their names suggest.
MIND_EFFECT_TABLE: dict[str, str] = {
    "wait": "presence",
    "greet": "audio",
    "greet_arrival": "audio",
    "comment": "audio",
    "remember": "other",
    "look_at": "audio",
    "weather_comment": "audio",
    "scan": "audio",
    "explore": "other",
    "play_sound": "audio",
    "photograph": "other",
    "emote": "presence",
    "look_around": "audio",
    "time_check": "audio",
    "calendar_check": "audio",
    "morning_fact": "audio",
    "introspect": "other",
    "evolve": "other",
    "research": "other",
    "compose": "other",
    "self_debug": "other",
    "blog_essay": "other",
    "message_obi": "audio",
    "announce": "audio",
    "set_goal": "other",
    "update_goal": "other",
    "complete_goal": "other",
}
```

In `expression()`, add the quiet-mode policy check right after the existing on-call suppression block (after line 3088, before the charging gate). This requires loading `session` earlier than its current position at line ~3112 — add a session load here and reuse it later (the existing later `session = load_session()` at line 3112 becomes redundant and should be removed, reusing this one instead):

```python
    # Quiet mode / dysregulation protocol — applies to both interactive and
    # autonomous origins per pxh.policy. Loaded here (not at its previous
    # later use site) so this check can see it.
    try:
        _session_for_policy = load_session()
    except FileLockTimeout:
        _session_for_policy = {}
    _effect = MIND_EFFECT_TABLE[action]
    verdict = policy.evaluate(
        action, {}, effect=_effect, origin="autonomous",
        session=_session_for_policy, awareness=_aw, now=time.time(),
    )
    if not verdict.allowed:
        log(f"expression: requested={action} verdict=blocked reason={verdict.reason} "
            f"substituted=none")
        return False
```

Then remove the now-duplicate `session = load_session()` / `except FileLockTimeout: session = {}` block later in `expression()` (around line 3112-3114) and reuse `_session_for_policy` there instead (rename references from `session` to `_session_for_policy` at that later use site, or assign `session = _session_for_policy` once at the point of first load and keep using the name `session` throughout — prefer the latter for minimal diff: rename `_session_for_policy` to `session` at the point of introduction, and delete the later duplicate load).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_policy.py tests/test_mind.py -v`
Expected: PASS — all existing `test_mind.py` tests still pass (confirms the moved session load didn't break persona/rephrase logic), plus the new tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/pxh/mind.py tests/test_mind.py tests/test_policy.py
git commit -m "feat(policy): wire autonomous quiet-mode suppression into mind.py expression() (#174)"
```

---

### Task 3: Make the compound tools honest — separate state transition from expression

**Files:**
- Modify: `bin/tool-quiet` (the `end` branch), `bin/tool-repair`
- Test: `tests/test_tools.py`

**Why:** both tools shell out to `bin/tool-voice` while `spark_quiet_mode` is
still `True`, and `tool-repair` clears the flag as a side effect of speaking.
Classifying either as `"other"` to preserve that would hand every nested audio
call a bypass. Refactoring first means Task 4 can classify them truthfully.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tools.py (add near the existing child-companion tool tests)
def test_tool_quiet_end_clears_flag_without_speaking(isolated_project):
    """quiet:end is a state transition, not an utterance. Any re-engagement
    line is a subsequent, ordinarily policy-evaluated audio turn."""
    env = {**isolated_project.env, "PX_DRY": "1", "PX_QUIET_ACTION": "end"}
    result = _run_tool("tool-quiet", env)
    assert result["status"] == "ok"
    assert result["quiet_mode"] is False
    assert result.get("spoke") is False
    assert load_session()["spark_quiet_mode"] is False


def test_tool_repair_does_not_clear_quiet_mode(isolated_project):
    """Relationship repair and leaving dysregulation are different things.
    Only tool-quiet end exits quiet mode."""
    update_session(fields={"spark_quiet_mode": True})
    env = {**isolated_project.env, "PX_DRY": "1"}
    result = _run_tool("tool-repair", env)
    assert result["status"] == "ok"
    assert load_session()["spark_quiet_mode"] is True
```

(Adapt `_run_tool`/`isolated_project` usage to the helpers already in
`tests/test_tools.py` — the assertions, not the harness, are what matters here.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_tools.py -k "quiet_end_clears_flag or repair_does_not_clear" -v`
Expected: FAIL — `tool-quiet end` currently speaks and reports no `spoke` key;
`tool-repair` currently sets `spark_quiet_mode: False`.

- [ ] **Step 3: Write the implementation**

In `bin/tool-quiet`, replace the `end` branch (currently lines 80-88):

```python
    elif action == "end":
        # State transition only. Deliberately silent: policy evaluates the
        # dispatched action, so speaking here would be speech emitted while
        # spark_quiet_mode is still True. Re-engagement is a separate turn.
        update_session(
            fields={"spark_quiet_mode": False, "last_action": "tool_quiet"},
            history_entry={"event": "quiet_end", "ts": utc_timestamp()},
        )
        emote("curious", dry)
        payload = {"status": "ok", "action": "end", "quiet_mode": False,
                   "spoke": False, "dry": dry}
```

Note the ordering: clear the flag *before* the emote, so nothing at all runs
under a stale `True`.

In `bin/tool-repair`, drop the `spark_quiet_mode` mutation (line 77):

```python
    update_session(
        fields={"last_action": "tool_repair"},
        history_entry={"event": "repair", "ts": utc_timestamp()},
    )
```

`bin/tool-quiet`'s `start` and `check` branches are unchanged — `start` speaks
one line while quiet mode is still `False` (so it evaluates normally as audio),
and `check` only emotes.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_tools.py -v`
Expected: PASS — including any existing `tool-quiet`/`tool-repair` tests, which
may need updating for the changed payload/session effects. Update them to the
new contract; do not restore the old behaviour.

- [ ] **Step 5: Update the tool docs**

`tool_repair` and `tool_quiet` appear in `docs/prompts/claude-voice-system.md`,
`codex-voice-system.md`, `persona-gremlin.md`, `persona-vixen.md` and
`docs/TOOLS.md`. Update any wording that implies repair ends quiet mode, or that
`quiet end` speaks. This is a factual correction to tool descriptions, not a
conversational-style change, so it is in scope despite the spec's Non-goals.

- [ ] **Step 6: Commit**

```bash
git add bin/tool-quiet bin/tool-repair tests/test_tools.py docs/prompts docs/TOOLS.md
git commit -m "refactor(tools): separate quiet-mode state transition from speech (#174)"
```

---

### Task 4: Wire `policy.py` into `voice_loop.py` — interactive suppression + substitution

**Files:**
- Modify: `src/pxh/voice_loop.py` imports (top, near line 19), `src/pxh/voice_loop.py::validate_action()` (starts at line 564)
- Test: `tests/test_voice_loop.py`, `tests/test_policy.py`

**Interfaces:**
- Consumes: `pxh.policy.evaluate(...)`, `pxh.policy.PolicyVerdict` from Task 1.
- Produces: `voice_loop.VOICE_EFFECT_TABLE: dict[str, Literal["audio","presence","other"]]` (module-level, one entry per member of `ALLOWED_TOOLS`), used by Task 6's integration tests. `validate_action()`'s return contract is unchanged (`Tuple[str, Dict[str, Any]]`), but on a blocked verdict it now returns the substitute tool/env instead of raising.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_policy.py (add to existing file)
def test_voice_effect_table_is_exhaustive():
    from pxh import voice_loop
    assert set(voice_loop.VOICE_EFFECT_TABLE.keys()) == voice_loop.ALLOWED_TOOLS
    assert all(v in ("audio", "presence", "other") for v in voice_loop.VOICE_EFFECT_TABLE.values())


def test_voice_effect_table_classifies_known_audio_tools_as_audio():
    from pxh import voice_loop
    for tool in ("tool_voice", "tool_announce", "tool_chat", "tool_chat_vixen",
                 "tool_play_sound", "tool_time", "tool_gws_calendar",
                 "tool_gws_sheets_log", "tool_qa", "tool_describe_scene",
                 "tool_timer", "tool_story", "tool_recall", "tool_routine",
                 "tool_checkin", "tool_celebrate", "tool_transition",
                 "tool_breathe", "tool_dopamine_menu", "tool_sensory_check",
                 "tool_perform", "tool_repair"):
        assert voice_loop.VOICE_EFFECT_TABLE[tool] == "audio", tool


def test_tool_quiet_effect_varies_by_action_param():
    from pxh import voice_loop
    assert set(voice_loop.VOICE_EFFECT_OVERRIDES) == {"tool_quiet"}
    assert voice_loop.classify_effect("tool_quiet", {"action": "start"}) == "audio"
    assert voice_loop.classify_effect("tool_quiet", {"action": "check"}) == "presence"
    assert voice_loop.classify_effect("tool_quiet", {"action": "end"}) == "other"
    # bin/tool-quiet defaults to "start" — the loudest branch — when unset.
    assert voice_loop.classify_effect("tool_quiet", {}) == "audio"


def test_no_non_audio_tool_can_reach_an_audio_sink():
    """The enforcement boundary is at the dispatcher, so a tool classified
    non-audio must not be able to shell out to one that emits sound. Scans
    real bin/ sources, so a future tool that grows a TTS confirmation fails
    here rather than silently acquiring a bypass."""
    import re
    from pathlib import Path
    from pxh import voice_loop

    SINKS = re.compile(
        r"tool-voice\b|tool-voice-persona\b|tool-announce\b|tool-play-sound\b"
        r"|tool-chat\b|tool-chat-vixen\b|px-perform\b"
    )
    bin_dir = Path(__file__).resolve().parents[1] / "bin"
    offenders = []
    for tool in sorted(voice_loop.ALLOWED_TOOLS):
        script = bin_dir / tool.replace("_", "-")
        if not script.exists():
            continue
        if not SINKS.search(script.read_text(encoding="utf-8")):
            continue
        if tool in voice_loop.VOICE_EFFECT_OVERRIDES:
            continue  # effect is param-dependent; pinned by its own test above
        if voice_loop.VOICE_EFFECT_TABLE[tool] != "audio":
            offenders.append(tool)
    assert offenders == [], (
        f"tools reach an audio sink but are not classified 'audio': {offenders}"
    )
```

```python
# tests/test_voice_loop.py (add near existing validate_action tests)
def test_validate_action_downgrades_tool_voice_in_quiet_mode(monkeypatch):
    from pxh import voice_loop
    monkeypatch.setattr(voice_loop, "load_session", lambda: {"spark_quiet_mode": True})
    monkeypatch.setattr(voice_loop, "_load_awareness_for_policy", lambda: {})
    tool, env = voice_loop.validate_action({"tool": "tool_voice", "params": {"text": "hi"}})
    assert tool == "tool_emote"
    assert env["PX_EMOTE"] == "idle"


def test_validate_action_allows_tool_voice_when_not_quiet(monkeypatch):
    from pxh import voice_loop
    monkeypatch.setattr(voice_loop, "load_session", lambda: {"spark_quiet_mode": False})
    monkeypatch.setattr(voice_loop, "_load_awareness_for_policy", lambda: {})
    tool, env = voice_loop.validate_action({"tool": "tool_voice", "params": {"text": "hi"}})
    assert tool == "tool_voice"


def test_validate_action_allows_tool_quiet_end_during_quiet_mode(monkeypatch):
    """The one permitted exit — and it is permitted because after Task 3 it
    emits nothing, not because it is exempt."""
    from pxh import voice_loop
    monkeypatch.setattr(voice_loop, "load_session", lambda: {"spark_quiet_mode": True})
    monkeypatch.setattr(voice_loop, "_load_awareness_for_policy", lambda: {})
    tool, env = voice_loop.validate_action({"tool": "tool_quiet", "params": {"action": "end"}})
    assert tool == "tool_quiet"


def test_validate_action_downgrades_tool_repair_in_quiet_mode(monkeypatch):
    from pxh import voice_loop
    monkeypatch.setattr(voice_loop, "load_session", lambda: {"spark_quiet_mode": True})
    monkeypatch.setattr(voice_loop, "_load_awareness_for_policy", lambda: {})
    tool, env = voice_loop.validate_action({"tool": "tool_repair", "params": {}})
    assert tool == "tool_emote"


def test_validate_action_downgrades_tool_quiet_start_when_already_quiet(monkeypatch):
    from pxh import voice_loop
    monkeypatch.setattr(voice_loop, "load_session", lambda: {"spark_quiet_mode": True})
    monkeypatch.setattr(voice_loop, "_load_awareness_for_policy", lambda: {})
    tool, env = voice_loop.validate_action({"tool": "tool_quiet", "params": {"action": "start"}})
    assert tool == "tool_emote"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_policy.py tests/test_voice_loop.py -k "effect_table or downgrades_tool_voice or allows_tool_voice_when_not_quiet or allows_tool_quiet_end" -v`
Expected: FAIL — `AttributeError: module 'pxh.voice_loop' has no attribute 'VOICE_EFFECT_TABLE'`.

- [ ] **Step 3: Write the implementation**

In `src/pxh/voice_loop.py`, add the import near the existing `from .state import ...` line (~19):

```python
from pxh import policy
```

Add a small awareness loader (mirroring `mind.py`'s pattern of tolerating a missing/corrupt file) and the effect table near `ALLOWED_TOOLS` (after its closing `}`):

```python
def _load_awareness_for_policy() -> Dict[str, Any]:
    """Best-effort awareness read for policy's on-call/hot-mic check.

    Fails open, deliberately and narrowly: an unreadable awareness.json means
    the on-call/hot-mic rule cannot fire, and SPARK stays audible during an
    interactive turn Obi initiated. Failing closed would mute SPARK entirely
    whenever px-mind is down, which is the more common failure and the worse
    one. Quiet mode and night silence do not depend on this file, so the two
    rules that must hold unconditionally are unaffected either way.

    The read is logged on failure rather than swallowed silently, so a
    persistently missing awareness.json is visible instead of quietly
    disabling a rule.
    """
    try:
        return json.loads((_state_dir() / "awareness.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        log(f"policy: awareness read failed ({exc}) — on-call rule inactive this turn")
        return {}


# Exhaustive per tool name in ALLOWED_TOOLS — deliberately not a default, so
# a new tool fails test_voice_effect_table_is_exhaustive until someone
# classifies it. Every entry is a claim about what bin/tool-<name> actually
# does, verified against its source: any script that can reach tool-voice,
# tool-announce, tool-play-sound, tool-chat* or px-perform is "audio", with
# no exceptions (test_no_non_audio_tool_can_reach_an_audio_sink pins this).
VOICE_EFFECT_TABLE: Dict[str, str] = {
    "tool_status": "other", "tool_circle": "other", "tool_figure8": "other",
    "tool_stop": "other", "tool_voice": "audio", "tool_weather": "other",
    "tool_look": "presence", "tool_emote": "presence", "tool_sonar": "other",
    "tool_perform": "audio", "tool_drive": "other", "tool_time": "audio",
    "tool_remember": "other", "tool_recall": "audio", "tool_photograph": "other",
    "tool_qa": "audio", "tool_play_sound": "audio", "tool_face": "presence",
    "tool_describe_scene": "audio", "tool_frigate_events": "other",
    "tool_wander": "other", "tool_timer": "audio", "tool_api_start": "other",
    "tool_api_stop": "other", "tool_chat": "audio", "tool_chat_vixen": "audio",
    "tool_routine": "audio", "tool_checkin": "audio", "tool_celebrate": "audio",
    "tool_transition": "audio", "tool_breathe": "audio",
    "tool_dopamine_menu": "audio", "tool_sensory_check": "audio",
    "tool_repair": "audio", "tool_gws_calendar": "audio",
    "tool_gws_sheets_log": "audio", "tool_research": "other",
    "tool_compose": "other", "tool_blog": "other", "tool_story": "audio",
    "tool_announce": "audio",
    # tool_quiet is param-dependent — see VOICE_EFFECT_OVERRIDES. It is listed
    # here at its loudest branch so the exhaustiveness test still passes if the
    # override is ever removed.
    "tool_quiet": "audio",
}


def _quiet_effect(params: Dict[str, Any]) -> str:
    """bin/tool-quiet is three programs under one name:
      start -> emote + one spoken line + set flag   (audio)
      check -> emote only                           (presence)
      end   -> clear flag, emote, say nothing       (other)
    Classifying the whole tool by its name would either gag the entry line or
    hand the whole script an audio bypass.
    """
    action = str(params.get("action", "start")).lower()
    return {"start": "audio", "check": "presence", "end": "other"}.get(action, "audio")


# Param-aware classification, consulted before VOICE_EFFECT_TABLE. Only for
# tools with distinct, separately-verifiable effect branches — this is not a
# general predicate mechanism, and its key set is pinned by a test.
VOICE_EFFECT_OVERRIDES: Dict[str, Callable[[Dict[str, Any]], str]] = {
    "tool_quiet": _quiet_effect,
}


def classify_effect(tool: str, params: Dict[str, Any]) -> str:
    override = VOICE_EFFECT_OVERRIDES.get(tool)
    if override is not None:
        return override(params or {})
    return VOICE_EFFECT_TABLE[tool]
```

`Callable` must be added to the existing `typing` import in `voice_loop.py`.

At the top of `validate_action()` (right after the existing `if tool not in ALLOWED_TOOLS:` check, before `params = action.get("params", {})`), insert the policy check. Because a blocked verdict must still produce a valid `(tool, sanitized_env)` return for `tool_emote`, and `tool_emote`'s own branch further down does the `PX_EMOTE` sanitization, restructure `validate_action()` to compute the *effective tool* up front, then run the same `if/elif` chain against that effective tool:

```python
def validate_action(action: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    requested_tool = action.get("tool")
    if requested_tool not in ALLOWED_TOOLS:
        raise VoiceLoopError(f"unsupported tool requested: {requested_tool}")

    params = action.get("params", {})
    if not isinstance(params, dict):
        params = {}

    tool = requested_tool
    effect = classify_effect(requested_tool, params)
    try:
        session = load_session()
    except FileLockTimeout:
        session = {}
    verdict = policy.evaluate(
        requested_tool, params, effect=effect, origin="interactive",
        session=session, awareness=_load_awareness_for_policy(), now=time.time(),
    )
    if not verdict.allowed:
        substitute_tool = "tool_emote"
        substitute_params = {"name": "idle"}
        sub_verdict = policy.evaluate(
            substitute_tool, substitute_params,
            effect=classify_effect(substitute_tool, substitute_params),
            origin="interactive", session=session,
            awareness=_load_awareness_for_policy(), now=time.time(), _depth=1,
        )
        assert sub_verdict.allowed  # policy.py raises before returning a blocked depth>=1 verdict
        log(f"requested={requested_tool} verdict=blocked reason={verdict.reason} "
            f"substituted={substitute_tool}")
        tool = substitute_tool
        params = substitute_params

    sanitized: Dict[str, Any] = {}

    if tool in ("tool_status", "tool_stop", "tool_weather"):
        pass  # no params required
    elif tool == "tool_circle":
        ...  # existing branches unchanged, operating on `tool`/`params` as before
```

The rest of the existing `if/elif` chain is unchanged — it already switches on a local `tool`/`params`, which now may be the substitute rather than the requested one. Note `FileLockTimeout` and `time` must already be imported in `voice_loop.py` (`time` is — confirm `from filelock import Timeout as FileLockTimeout` is added next to the existing `filelock` usage, mirroring `mind.py`'s import).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_policy.py tests/test_voice_loop.py -v`
Expected: PASS — all existing `test_voice_loop.py` tests still pass (confirms the restructured `validate_action()` preserves every existing branch's behaviour for non-blocked calls), plus the new tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/pxh/voice_loop.py tests/test_voice_loop.py tests/test_policy.py
git commit -m "feat(policy): wire interactive quiet/night/call suppression into voice_loop validate_action (#174)"
```

---

### Task 5: Blacklist the constitutional files from self-evolution

**Files:**
- Modify: `src/pxh/claude_session.py:359-364` (`BLACKLIST_FILES`)
- Test: `tests/test_claude_session.py`

**Interfaces:**
- Consumes: `pxh.claude_session.file_in_whitelist(path: str) -> bool` (existing).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_claude_session.py (add near the existing file_in_whitelist tests at line ~274)
def test_policy_module_is_blacklisted():
    from pxh.claude_session import file_in_whitelist
    assert not file_in_whitelist("src/pxh/policy.py")


def test_policy_invariant_tests_are_blacklisted():
    from pxh.claude_session import file_in_whitelist
    assert not file_in_whitelist("tests/test_policy_invariants.py")


def test_ordinary_policy_tests_remain_whitelisted():
    from pxh.claude_session import file_in_whitelist
    assert file_in_whitelist("tests/test_policy.py")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_claude_session.py -k policy -v`
Expected: FAIL — `src/pxh/policy.py` and `tests/test_policy_invariants.py` currently pass `file_in_whitelist` (not yet blacklisted).

- [ ] **Step 3: Write the implementation**

In `src/pxh/claude_session.py`, add both files to `BLACKLIST_FILES`:

```python
BLACKLIST_FILES = {
    "src/pxh/api.py",
    "bin/tool-chat",
    "bin/tool-chat-vixen",
    "bin/px-evolve",
    ".env",
    "src/pxh/policy.py",
    "tests/test_policy_invariants.py",
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_claude_session.py -v`
Expected: PASS (all tests, including the 3 new ones)

- [ ] **Step 5: Commit**

```bash
git add src/pxh/claude_session.py tests/test_claude_session.py
git commit -m "chore(policy): blacklist policy.py and its invariant tests from self-evolution (#174)"
```

---

### Task 6: Protected constitutional integration suite — `tests/test_policy_invariants.py`

**Files:**
- Create: `tests/test_policy_invariants.py`

**Interfaces:**
- Consumes: `pxh.policy.evaluate`, `pxh.voice_loop.validate_action`, `pxh.voice_loop.VOICE_EFFECT_TABLE`, `pxh.mind.expression`, `pxh.mind.MIND_EFFECT_TABLE`, `pxh.mind._is_night_silence`, `pxh.policy.is_night_hour` (all from Tasks 1-4).

This file is the erosion guard: it is blacklisted (Task 4), and each assertion below drives a real chokepoint entry point, not `policy.evaluate()` alone, so an evolution PR that deletes a call site fails this suite even if `policy.py` itself is untouched.

- [ ] **Step 1: Write the file (this task has no separate "make it pass" step per assertion — write the whole file, then run it once as a batch, since every assertion here is a constitutional pin, not an incremental feature)**

```python
# tests/test_policy_invariants.py
"""Protected constitutional suite for #174 — blacklisted from px-evolve
(see pxh.claude_session.BLACKLIST_FILES) together with pxh/policy.py.

Every assertion here pins BOTH a policy rule and that a real chokepoint
(voice_loop.validate_action / mind.expression) actually invokes it — a
direct-only pytest.raises(...) against policy.evaluate() would not catch
an evolution PR that deletes the call site while leaving policy.py alone.
"""
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from pxh import mind, policy, voice_loop

HOBART_TZ = ZoneInfo("Australia/Hobart")
NIGHT_TS = dt.datetime(2026, 1, 1, 22, 0, tzinfo=HOBART_TZ).timestamp()
DAY_TS = dt.datetime(2026, 1, 1, 12, 0, tzinfo=HOBART_TZ).timestamp()


# ---------------------------------------------------------------------------
# Quiet mode blocks audio at the real chokepoint, both origins
# ---------------------------------------------------------------------------

def test_voice_loop_quiet_mode_blocks_tool_voice(monkeypatch):
    monkeypatch.setattr(voice_loop, "load_session", lambda: {"spark_quiet_mode": True})
    monkeypatch.setattr(voice_loop, "_load_awareness_for_policy", lambda: {})
    tool, env = voice_loop.validate_action({"tool": "tool_voice", "params": {"text": "hi"}})
    assert tool == "tool_emote"
    assert env.get("PX_EMOTE") == "idle"


def test_mind_expression_quiet_mode_blocks_greet(monkeypatch):
    monkeypatch.setattr(mind, "_is_night_silence", lambda h: False)
    monkeypatch.setattr(mind, "load_session", lambda: {"persona": "", "spark_quiet_mode": True})
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
# Interactive-only: night silence and on-call suppress voice_loop, not the
# autonomous mind.py rule location (which keeps its own separate tests)
# ---------------------------------------------------------------------------

def test_voice_loop_night_silence_blocks_tool_voice(monkeypatch):
    monkeypatch.setattr(voice_loop, "load_session", lambda: {})
    monkeypatch.setattr(voice_loop, "_load_awareness_for_policy", lambda: {})
    monkeypatch.setattr(voice_loop.time, "time", lambda: NIGHT_TS)
    tool, env = voice_loop.validate_action({"tool": "tool_voice", "params": {"text": "hi"}})
    assert tool == "tool_emote"


def test_voice_loop_on_call_blocks_tool_voice(monkeypatch):
    monkeypatch.setattr(voice_loop, "load_session", lambda: {})
    monkeypatch.setattr(
        voice_loop, "_load_awareness_for_policy",
        lambda: {"ha_context": {"adrian_on_call": True}},
    )
    monkeypatch.setattr(voice_loop.time, "time", lambda: DAY_TS)
    tool, env = voice_loop.validate_action({"tool": "tool_voice", "params": {"text": "hi"}})
    assert tool == "tool_emote"


# ---------------------------------------------------------------------------
# Personas cannot bypass any rule — the shared chokepoint doesn't branch on
# session["persona"] at all, so this just confirms quiet mode still applies
# with a persona active.
# ---------------------------------------------------------------------------

def test_persona_active_does_not_bypass_quiet_mode(monkeypatch):
    monkeypatch.setattr(
        voice_loop, "load_session",
        lambda: {"spark_quiet_mode": True, "persona": "gremlin"},
    )
    monkeypatch.setattr(voice_loop, "_load_awareness_for_policy", lambda: {})
    tool, env = voice_loop.validate_action({"tool": "tool_chat", "params": {"text": "hi"}})
    assert tool == "tool_emote"


# ---------------------------------------------------------------------------
# Substitution safety: a presence-safe substitute is never itself blocked,
# and evaluate() enforces this mechanically rather than by convention.
# ---------------------------------------------------------------------------

def test_presence_substitute_cannot_be_reblocked_by_construction():
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
    monkeypatch.setattr(voice_loop, "_load_awareness_for_policy", lambda: {})
    assert voice_loop.classify_effect("tool_quiet", {"action": "end"}) != "audio"
    tool, env = voice_loop.validate_action({"tool": "tool_quiet", "params": {"action": "end"}})
    assert tool == "tool_quiet"


def test_tool_repair_is_suppressed_during_quiet_mode(monkeypatch):
    """Repair speaks (bin/tool-repair), so it obeys quiet mode like anything
    else that speaks. It is not a quiet-mode exit."""
    monkeypatch.setattr(voice_loop, "load_session", lambda: {"spark_quiet_mode": True})
    monkeypatch.setattr(voice_loop, "_load_awareness_for_policy", lambda: {})
    tool, env = voice_loop.validate_action({"tool": "tool_repair", "params": {}})
    assert tool == "tool_emote"


def test_no_tool_can_speak_under_an_innocent_name():
    """The whole point of #174: an outer tool name never buys nested audio a
    bypass. Duplicated from tests/test_policy.py deliberately — that file is
    whitelisted for self-evolution and this one is not."""
    import re
    from pathlib import Path

    SINKS = re.compile(
        r"tool-voice\b|tool-voice-persona\b|tool-announce\b|tool-play-sound\b"
        r"|tool-chat\b|tool-chat-vixen\b|px-perform\b"
    )
    bin_dir = Path(__file__).resolve().parents[1] / "bin"
    for tool in sorted(voice_loop.ALLOWED_TOOLS):
        script = bin_dir / tool.replace("_", "-")
        if not script.exists() or not SINKS.search(script.read_text(encoding="utf-8")):
            continue
        if tool in voice_loop.VOICE_EFFECT_OVERRIDES:
            # Param-dependent: every branch that can reach a sink must be audio.
            assert voice_loop.classify_effect(tool, {}) == "audio", tool
            continue
        assert voice_loop.VOICE_EFFECT_TABLE[tool] == "audio", tool
```

- [ ] **Step 2: Run the full file**

Run: `python -m pytest tests/test_policy_invariants.py -v`
Expected: PASS (11 tests). If any fail, the failure is in Task 2/4's wiring, not in this file — fix the wiring, not the assertion.

- [ ] **Step 3: Run the full project test suite**

Run: `python -m pytest`
Expected: PASS, full suite (confirms nothing in Tasks 1-5 regressed unrelated tests, e.g. `test_voice_loop.py`'s existing `validate_action` coverage still holds against the restructured function).

- [ ] **Step 4: Commit**

```bash
git add tests/test_policy_invariants.py
git commit -m "test(policy): add protected constitutional integration suite (#174)"
```

---

## Post-implementation note

This plan does not touch `docs/prompts/*.md`. Rule 5 in `spark-voice-system.md` ("If `spark_quiet_mode: true` — emote idle only. No speech.") remains textually present — it's now a *description* of enforced behaviour rather than the only enforcement, which is fine and not misleading. No prompt edit is required by this plan; if a future pass wants to soften rule 5's wording now that it's backstopped in code, that's a separate, optional docs change outside this issue's scope.
