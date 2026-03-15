# px-mind Tool Expansion Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand px-mind's expression layer from 8 to 14 actions, giving SPARK richer autonomous behaviour (sounds, photos, emotes, head movement, time/calendar checks).

**Architecture:** All changes confined to `bin/px-mind`. Expand `VALID_ACTIONS`, add 6 `elif` branches in `expression()`, update gates, update 4 reflection prompts. All target tools already exist.

**Tech Stack:** Python 3.11 (embedded in bash heredoc), existing `bin/tool-*` scripts

**Spec:** `docs/superpowers/specs/2026-03-15-mind-tool-expansion-design.md`

---

## File Structure

| File | Responsibility |
|------|---------------|
| `bin/px-mind` | All changes -- VALID_ACTIONS, expression(), gates, prompts |
| `tests/test_mind_utils.py` | 22 new tests (mood mappings, gates, dispatch, validation) |

---

## Chunk 1: Mappings, Validation, Gates

### Task 1: VALID_ACTIONS and Mood Mappings

**Files:**
- Modify: `bin/px-mind:308-309` (VALID_ACTIONS)
- Modify: `tests/test_mind_utils.py`

- [ ] **Step 1: Write mapping and validation tests**

  In `tests/test_mind_utils.py`, extract new constants via the existing `_load_mind_helpers()` pattern. Write:
  - `test_valid_actions_includes_new_actions` -- verify all 14 actions in VALID_ACTIONS
  - `test_mood_to_sound_mapping` -- curious/alert -> beep, happy/excited/playful -> tada, content/peaceful -> chime
  - `test_mood_to_emote_mapping` -- happy->happy, curious->curious, etc.
  - `test_mood_mapping_fallback` -- unknown mood -> chime (sound) and idle (emote)

- [ ] **Step 2: Run tests -- expect FAIL**

  Run: `python -m pytest tests/test_mind_utils.py -v -k "valid_actions or mood_to"`

- [ ] **Step 3: Implement in px-mind**

  At `bin/px-mind:308-309`, expand VALID_ACTIONS to include all 14 actions.

  After VALID_ACTIONS, add mood mapping dicts:
  - `MOOD_TO_SOUND` -- maps mood names to sound names (beep/tada/chime)
  - `MOOD_TO_EMOTE` -- maps mood names to emote names (happy/curious/alert/excited/thinking/shy)
  - Default fallback: chime for sounds, idle for emotes

- [ ] **Step 4: Run tests -- expect PASS**

- [ ] **Step 5: Commit**

  ```
  git add bin/px-mind tests/test_mind_utils.py
  git commit -m "feat(mind): expand VALID_ACTIONS to 14 + mood mapping dicts"
  ```

---

### Task 2: Gate Updates

**Files:**
- Modify: `bin/px-mind:~2063-2073` (charging and absent gates in expression())
- Modify: `tests/test_mind_utils.py`

- [ ] **Step 1: Write gate tests**

  - `test_charging_gate_blocks_emote`
  - `test_charging_gate_blocks_look_around`
  - `test_charging_gate_blocks_calendar_check` (uses servos via internal emote)
  - `test_charging_gate_allows_photograph` (camera works while charging)
  - `test_absent_gate_blocks_play_sound`
  - `test_absent_gate_blocks_photograph` (speaks to empty room)
  - `test_absent_gate_blocks_time_check`
  - `test_absent_gate_blocks_calendar_check`
  - `test_absent_gate_allows_emote` (silent physical expression)

- [ ] **Step 2: Run tests -- expect FAIL**

  Run: `python -m pytest tests/test_mind_utils.py -v -k "gate"`

- [ ] **Step 3: Update gates in expression()**

  Charging gate (~line 2073) -- add `emote`, `look_around`, `calendar_check`:
  ```python
  if _charging and action in ("scan", "look_at", "explore", "emote", "look_around", "calendar_check"):
  ```

  Absent gate (~line 2063) -- add `play_sound`, `time_check`, `calendar_check`, `photograph`:
  ```python
  if _obi_mode == "absent" and action in ("greet", "comment", "weather_comment", "scan",
                                           "play_sound", "time_check", "calendar_check", "photograph"):
  ```

- [ ] **Step 4: Run tests -- expect PASS**

- [ ] **Step 5: Commit**

  ```
  git add bin/px-mind tests/test_mind_utils.py
  git commit -m "feat(mind): update charging + absent gates for new actions"
  ```

---

## Chunk 2: Expression Dispatch, Prompts

### Task 3: Expression Dispatch Branches

**Files:**
- Modify: `bin/px-mind:~2049-2280` (expression() function)
- Modify: `tests/test_mind_utils.py`

- [ ] **Step 1: Write dispatch tests (mock subprocess)**

  - `test_expression_play_sound_calls_tool` -- verify `tool-play-sound` called with `PX_SOUND` env
  - `test_expression_photograph_calls_describe_scene` -- verify `tool-describe-scene` called (NOT tool-photograph)
  - `test_expression_emote_calls_tool` -- verify `tool-emote` called with `PX_EMOTE` env
  - `test_expression_look_around_calls_tool` -- verify `tool-look` called with `PX_PAN`/`PX_TILT` env
  - `test_expression_time_check_calls_tool` -- verify `tool-time` called
  - `test_expression_calendar_check_calls_tool` -- verify `tool-gws-calendar` with `PX_CALENDAR_ACTION=next`
  - `test_unknown_action_logged` -- unrecognised action triggers "unhandled action" log

- [ ] **Step 2: Run tests -- expect FAIL**

  Run: `python -m pytest tests/test_mind_utils.py -v -k "expression_"`

- [ ] **Step 3: Add 6 elif branches to expression()**

  Before the `else: log("unhandled action")` clause, add branches for each new action:

  - `play_sound`: map mood via MOOD_TO_SOUND, set PX_SOUND env, call tool-play-sound (timeout 15s)
  - `photograph`: call tool-describe-scene via Popen+SIGTERM pattern (timeout 120s). Do NOT call tool-photograph separately. Do NOT call yield_alive (tool-describe-scene handles it internally).
  - `emote`: map mood via MOOD_TO_EMOTE, set PX_EMOTE env, call tool-emote (timeout 15s). Do NOT call yield_alive (px-emote handles it).
  - `look_around`: random PX_PAN (-40..40) and PX_TILT (-10..30), call tool-look (timeout 15s). Do NOT call yield_alive (px-look handles it).
  - `time_check`: call tool-time (timeout 15s)
  - `calendar_check`: set PX_CALENDAR_ACTION=next, call tool-gws-calendar (timeout 60s)

- [ ] **Step 4: Run tests -- expect PASS**

- [ ] **Step 5: Commit**

  ```
  git add bin/px-mind tests/test_mind_utils.py
  git commit -m "feat(mind): 6 new expression branches -- sound, photo, emote, look, time, calendar"
  ```

---

### Task 4: Prompt Updates and Explore Injection Fix

**Files:**
- Modify: `bin/px-mind:~520-640` (reflection prompts)
- Modify: `bin/px-mind:~1943` (explore injection)
- Modify: `tests/test_mind_utils.py`

- [ ] **Step 1: Write prompt test**

  - `test_explore_injection_after_enum_expansion` -- verify explore is correctly injected into the expanded action enum

- [ ] **Step 2: Run test -- expect FAIL**

- [ ] **Step 3: Update all 4 prompt action lists**

  Expand the action enum in REFLECTION_SYSTEM, REFLECTION_SYSTEM_GREMLIN, REFLECTION_SYSTEM_VIXEN, and _SPARK_REFLECTION_SUFFIX to include all 14 actions with descriptions for the 6 new ones.

- [ ] **Step 4: Fix explore injection**

  Update the string-replace target to match the new enum ending. The last action before explore is now `calendar_check`:
  ```python
  system_prompt = system_prompt.replace(
      'time_check, calendar_check"',
      'time_check, calendar_check, explore"'
  )
  ```

- [ ] **Step 5: Run test -- expect PASS**

- [ ] **Step 6: Run full test suite**

  Run: `python -m pytest -q` (expect all pass)

- [ ] **Step 7: Commit**

  ```
  git add bin/px-mind tests/test_mind_utils.py
  git commit -m "feat(mind): update reflection prompts for 14 actions + fix explore injection"
  ```
