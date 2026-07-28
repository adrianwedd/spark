# Remove Reactive Mechanism / Transition-Driven greet_arrival Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete the template-based reactive path in `px-mind` and route arrival/presence transitions through normal reflection→expression, with `greet_arrival` exposed to SPARK and a cooldown bypass so arrival greetings aren't silenced by the 30-minute expression budget.

**Architecture:** All transitions now trigger Layer 2 reflection (the `reacted` short-circuit is removed). `expression()` returns `bool` so the main loop only charges the expression budget for actions that actually dispatched. A new pure function `_should_express()` implements the cooldown gate, including a `person_arrived_home`-conditioned bypass for `greet_arrival` guarded by `GREET_ARRIVAL_COOLDOWN_S` anti-flap.

**Tech Stack:** Python 3.11, pytest (monkeypatch style, `isolated_project` autouse fixture from `conftest.py`).

## Global Constraints

- All time-of-day logic uses `ZoneInfo("Australia/Hobart")` — never hardcoded UTC offsets.
- `greet_arrival` is deliberately **NOT** added to `ABSENT_GATED_ACTIONS` (arrivals invalidate the absence heuristic) and **NOT** added to `NIGHT_ALLOWED_ACTIONS` (it speaks).
- Tunable constants live in `src/pxh/spark_config.py` (self-evolution whitelist target).
- Battery-warning paths in `mind_loop()` keep stamping `last_expression_mono` directly — deterministic safety paths are exempt from the return-status change.
- Run tests with: `python -m pytest tests/test_mind.py tests/test_mind_utils.py -q` per task; full `python -m pytest -m "not live" -q` at the end. Pre-existing failures (memory-dedup test, live/GPIO tests) are out of scope.
- Line numbers below refer to the working tree after Task 0.

---

### Task 0: Commit pre-existing WIP so our commits are clean

The working tree already carries unrelated, coherent work (HA sleep-sensor 404 handling, `FileLockTimeout` hardening in `awareness_tick`/`expression()`, matching tests, CLAUDE.md + `bin/px-api-server` tweaks). It textually overlaps `expression()` and `reactive_response()`, so it must be committed first — as its own commit, clearly not part of this feature.

**Files:**
- Modify: none (commit only). Leave `package-lock.json` and `state/blog_failures.json` untracked.

- [ ] **Step 1: Review the diff to write an honest message**

Run: `git diff CLAUDE.md bin/px-api-server src/pxh/mind.py src/pxh/spark_config.py tests/test_mind.py tests/test_mind_utils.py`

- [ ] **Step 2: Verify the WIP passes its own tests**

Run: `python -m pytest tests/test_mind.py tests/test_mind_utils.py -q`
Expected: PASS

- [ ] **Step 3: Commit tracked modifications only**

```bash
git add CLAUDE.md bin/px-api-server src/pxh/mind.py src/pxh/spark_config.py tests/test_mind.py tests/test_mind_utils.py
git commit -m "fix: pre-existing WIP — HA sleep 404 latch + FileLockTimeout hardening"
```

---

### Task 1: `expression()` returns executed/suppressed status; loop charges budget only on execute

**Files:**
- Modify: `src/pxh/mind.py` (`expression()` ~3044–3510; `mind_loop()` cooldown block ~3736–3742)
- Test: `tests/test_mind.py`

**Interfaces:**
- Produces: `expression(thought: dict, dry: bool, awareness: dict | None = None) -> bool` — `True` iff an action was dispatched (or attempted past all gates); `False` when gated/suppressed or `action == "wait"`. Task 4's loop wiring relies on this.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mind.py` (add `import inspect` to the imports if writing Task 2's test in the same file later):

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mind.py -k "expression_returns" -v`
Expected: FAIL — `expression()` currently returns `None`, so `result is True` / `is False` assertions fail.

- [ ] **Step 3: Implement the return contract**

In `src/pxh/mind.py`:

1. Change the signature and docstring:

```python
def expression(thought: dict, dry: bool, awareness: dict | None = None) -> bool:
    """Layer 3: act on a thought.

    Returns True when an action was dispatched (the expression budget should
    be charged), False when the action was gated/suppressed or was "wait".
    """
```

2. Replace every bare `return` inside `expression()` with `return False`. There are exactly 12, currently at lines 3050 (wait), 3059 (night silence), 3072/3075/3078 (absent/at-school/at-mums), 3085 (decompress), 3088 (quiet time), 3091 (bedtime), 3099 (call/mic), 3109 (charging), 3211 and 3240 (explore aborts).

3. At the very end of the function, after the `update_session(...)` try/except block, add:

```python
    return True
```

4. In `mind_loop()`, replace the cooldown block:

```python
            if thought and thought.get("action", "wait") != "wait":
                # Layer 3: Expression (with cooldown)
                if (now - last_expression_mono) > EXPRESSION_COOLDOWN_S:
                    expression(thought, args.dry_run, awareness=awareness)
                    last_expression_mono = now
                else:
                    log(f"expression suppressed (cooldown): {thought['action']}")
```

with:

```python
            if thought and thought.get("action", "wait") != "wait":
                # Layer 3: Expression (with cooldown). Only a dispatched action
                # charges the budget — a gate-suppressed one must not silence
                # SPARK for the next 30 minutes.
                if (now - last_expression_mono) > EXPRESSION_COOLDOWN_S:
                    if expression(thought, args.dry_run, awareness=awareness):
                        last_expression_mono = now
                else:
                    log(f"expression suppressed (cooldown): {thought['action']}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mind.py tests/test_mind_utils.py -q`
Expected: PASS (including the pre-existing lock-timeout expression tests).

- [ ] **Step 5: Commit**

```bash
git add src/pxh/mind.py tests/test_mind.py
git commit -m "feat(mind): expression() reports executed/suppressed; only executed charges cooldown"
```

---

### Task 2: Delete the reactive mechanism; all transitions trigger reflection

**Files:**
- Modify: `src/pxh/mind.py`
- Test: `tests/test_mind.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `mind_loop()` with no `reacted` short-circuit — `should_reflect = len(transitions) > 0 or (now - last_reflection_mono) > effective_interval`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mind.py` (ensure `import inspect` is present at the top of the file):

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mind.py::test_reactive_mechanism_removed -v`
Expected: FAIL on the first `hasattr` assertion.

- [ ] **Step 3: Delete the reactive mechanism**

In `src/pxh/mind.py`, delete all of:

1. Line 104: `REACTIVE_COOLDOWN_S    = 30    # min seconds between reactive responses (was 15)`
2. Lines 279–377: the comment block starting `# Reactive response templates (bypass LLM for instant reaction).` and the entire `REACTIVE_TEMPLATES = { ... }` dict (ends at the `}` on line 377).
3. Line ~1779: `_last_reactive_phrases: dict = {}  # key="transition:persona", value=recent phrase list (max 3)`
4. In `_reset_state()`: change `global _mood_history, _last_reactive_phrases` to `global _mood_history`, and delete the `_last_reactive_phrases = {}` line.
5. The whole `reactive_response()` function (def at ~3513 through `log(f"reactive error: {exc}")`, up to but not including `def _consolidation_tick`).
6. In `mind_loop()`: the `last_reactive_mono = 0.0` initializer; in the backoff comment change `(listening, transition, reactive event)` to `(listening, transition)`; and the whole block:

```python
        # Reactive behavior: instant template response for key transitions
        reactive_transitions = {"someone_appeared", "someone_left", "person_arrived_home"}
        reacted = False
        if transitions and (now - last_reactive_mono) > REACTIVE_COOLDOWN_S:
            for t in transitions:
                # Transitions like "person_arrived_home:obi_chipolo" use prefix matching
                t_base = t.split(":")[0] if ":" in t else t
                if t_base in reactive_transitions and t_base in REACTIVE_TEMPLATES:
                    reactive_response(t_base, awareness, args.dry_run)
                    last_reactive_mono = now
                    last_expression_mono = now  # count as expression too
                    reacted = True
                    break  # one reactive response per tick
```

7. Change:

```python
        should_reflect = not reacted and (
            len(transitions) > 0
            or (now - last_reflection_mono) > effective_interval
        )
```

to:

```python
        should_reflect = (
            len(transitions) > 0
            or (now - last_reflection_mono) > effective_interval
        )
```

- [ ] **Step 4: Verify no dangling references and tests pass**

Run: `grep -in "reactive" src/pxh/mind.py src/pxh/spark_config.py` — Expected: no output.
Run: `python -m pytest tests/test_mind.py tests/test_mind_utils.py -q` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pxh/mind.py tests/test_mind.py
git commit -m "refactor(mind): remove template reactive path — transitions now trigger reflection"
```

---

### Task 3: Expose `greet_arrival` to SPARK; gate it for decompress (not absence)

**Files:**
- Modify: `src/pxh/spark_config.py` (`_SPARK_REFLECTION_SUFFIX`), `src/pxh/mind.py` (decompress tuple ~3083, `ABSENT_GATED_ACTIONS` comment ~454)
- Test: `tests/test_mind.py`

**Interfaces:**
- Consumes: `expression() -> bool` from Task 1.
- Produces: SPARK prompt containing the `greet_arrival` action + usage rule.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mind.py` (add `from pxh import spark_config` to imports if not present):

```python
def test_spark_prompt_exposes_greet_arrival():
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
```

- [ ] **Step 2: Run tests to verify expected failures**

Run: `python -m pytest tests/test_mind.py -k "greet_arrival or spark_prompt" -v`
Expected: `test_spark_prompt_exposes_greet_arrival` FAILS (count is 0); `test_greet_arrival_suppressed_during_decompress` FAILS (returns True — not yet in the decompress tuple); the other two PASS already (they pin current, correct behavior as regression guards).

- [ ] **Step 3: Implement**

1. `src/pxh/spark_config.py`, inside `_SPARK_REFLECTION_SUFFIX`, insert a bullet after the `- "blog_essay" ...` line:

```
- "greet_arrival" — greet a person who just arrived home. Use ONLY when a person_arrived_home transition appears under "Transitions just detected"; never otherwise.
```

2. Same file, in the JSON output block, change the action enumeration line to include `greet_arrival` after `greet`:

```
  "action": "one of: wait, greet, greet_arrival, comment, remember, look_at, weather_comment, scan, play_sound, photograph, emote, look_around, time_check, calendar_check, introspect, evolve, morning_fact, research, compose, self_debug, blog_essay, message_obi, set_goal, update_goal, complete_goal",
```

3. `src/pxh/mind.py` line ~3083, change:

```python
    if "decompress" in _current_event and action in ("greet", "comment", "scan", "calendar_check"):
```

to:

```python
    if "decompress" in _current_event and action in ("greet", "greet_arrival", "comment", "scan", "calendar_check"):
```

4. `src/pxh/mind.py`, directly above `ABSENT_GATED_ACTIONS` (~454), add:

```python
# Deliberately NOT absence-gated: "greet_arrival". Arrivals are the one moment
# the absence heuristic is guaranteed stale — the person just walked in — so
# absent/at-school/at-mums must never suppress an arrival greeting.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mind.py tests/test_mind_utils.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pxh/spark_config.py src/pxh/mind.py tests/test_mind.py
git commit -m "feat(mind): expose greet_arrival to SPARK; decompress-gate it, never absence-gate it"
```

---

### Task 4: Arrival bypass of the expression cooldown (`_should_express` + anti-flap)

**Files:**
- Modify: `src/pxh/spark_config.py` (new constant), `src/pxh/mind.py` (new `_should_express()` above `mind_loop()`, loop wiring)
- Test: `tests/test_mind.py`

**Interfaces:**
- Consumes: `expression() -> bool` (Task 1); Task 1's loop wiring is replaced here.
- Produces: `_should_express(action: str, transitions: list, now: float, last_expression_mono: float, last_greet_arrival_mono: float) -> bool`; constant `GREET_ARRIVAL_COOLDOWN_S = 120` in `spark_config`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mind.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mind.py -k "should_express" -v`
Expected: FAIL with `AttributeError: module 'pxh.mind' has no attribute 'GREET_ARRIVAL_COOLDOWN_S'` (or `_should_express`).

- [ ] **Step 3: Implement**

1. `src/pxh/spark_config.py`, directly under `EXPRESSION_COOLDOWN_S`:

```python
GREET_ARRIVAL_COOLDOWN_S = 120   # anti-flap for arrival greetings that bypass the expression budget
```

2. `src/pxh/mind.py`: add `GREET_ARRIVAL_COOLDOWN_S` to the existing `from pxh.spark_config import (...)` list (line ~37).

3. `src/pxh/mind.py`, directly above `def mind_loop(args)`:

```python
def _should_express(action: str, transitions: list, now: float,
                    last_expression_mono: float,
                    last_greet_arrival_mono: float) -> bool:
    """Layer 3 cooldown gate.

    greet_arrival bypasses the global expression budget when an arrival
    transition is present this tick — an arrival is the one moment the
    30-minute budget must not silence SPARK. GREET_ARRIVAL_COOLDOWN_S keeps
    a flapping tracker from spamming greetings through the bypass, while
    still allowing separate people arriving minutes apart to each get one.
    """
    if action == "greet_arrival" and any(
            t.split(":", 1)[0] == "person_arrived_home" for t in transitions):
        if (now - last_greet_arrival_mono) > GREET_ARRIVAL_COOLDOWN_S:
            return True
    return (now - last_expression_mono) > EXPRESSION_COOLDOWN_S
```

4. In `mind_loop()`: add `last_greet_arrival_mono = 0.0` next to `last_expression_mono = 0.0`, and replace Task 1's cooldown block with:

```python
            if thought and thought.get("action", "wait") != "wait":
                _action = thought["action"]
                # Layer 3: Expression. Only a dispatched action charges the
                # budget; greet_arrival may bypass it on a real arrival.
                if _should_express(_action, transitions, now,
                                   last_expression_mono, last_greet_arrival_mono):
                    if expression(thought, args.dry_run, awareness=awareness):
                        last_expression_mono = now
                        if _action == "greet_arrival":
                            last_greet_arrival_mono = now
                else:
                    log(f"expression suppressed (cooldown): {_action}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mind.py tests/test_mind_utils.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pxh/spark_config.py src/pxh/mind.py tests/test_mind.py
git commit -m "feat(mind): greet_arrival bypasses expression cooldown on real arrivals (anti-flap 120s)"
```

---

### Task 5: Documentation sync + full-suite verification

**Files:**
- Modify: `docs/specs/layer-3-expression.md`, `CLAUDE.md`

- [ ] **Step 1: Fix the spec**

In `docs/specs/layer-3-expression.md`:
1. Both cooldown statements (~lines 105 and 138): change `EXPRESSION_COOLDOWN_S = 120` seconds (2 minutes) to `EXPRESSION_COOLDOWN_S = 1800` seconds (30 minutes); drop the stale `line 3124` references (refer to "the main loop in `mind.py`" instead).
2. Amend the "Any action (except `wait`) resets the cooldown timer" sentence to: "Only an action that actually dispatches charges the cooldown; a gate-suppressed action does not. `greet_arrival` bypasses the global cooldown when a `person_arrived_home` transition was detected this tick, throttled by `GREET_ARRIVAL_COOLDOWN_S` (120 s)."
3. Add `greet_arrival` to the example action enumeration (~line 312), after `greet`.

- [ ] **Step 2: Fix CLAUDE.md**

In the Layer 3 bullet of the Cognitive Loop section: change `**Layer 3 — Expression** (2min cooldown)` to `**Layer 3 — Expression** (30min cooldown; `greet_arrival` bypasses it on a real arrival)`. Add `greet_arrival` to the listed valid actions.

- [ ] **Step 3: Full verification**

Run: `python -m pytest -m "not live" -q`
Expected: same pass count as baseline plus the new tests; no new failures beyond the known pre-existing ones (memory-dedup test).

- [ ] **Step 4: Commit**

```bash
git add docs/specs/layer-3-expression.md CLAUDE.md
git commit -m "docs: sync expression cooldown (1800s), greet_arrival, executed-only budget semantics"
```
