# SPARK Reflection Angles — Design Spec

**Date:** 2026-03-14
**Scope:** `bin/px-mind` — `REFLECTION_SYSTEM_SPARK`, `SPARK_ANGLES`, `TOPIC_SEEDS`

---

## Problem

SPARK's inner-monologue reflections repeat too often. Two causes:

1. **Static angles**: The 7-item "Possible angles" list in `REFLECTION_SYSTEM_SPARK` never changes — every reflection sees the same menu, biasing the LLM toward the same style choices.
2. **Thin TOPIC_SEEDS coverage**: The existing ~52 seeds cover existence, curiosity, and robot life well, but lack cosmic scale, deep time, quantum physics, math beauty, and biology wonder.

---

## Design

### Part 1 — Dynamic angle injection

**New: `SPARK_ANGLES`** — a pool of ~28 angle descriptions, covering:

| Category | Example |
|---|---|
| Cosmic scale | poetic musing on light-travel times, stellar distances |
| Deep time | geological or evolutionary perspective on this moment |
| Quantum strangeness | superposition, entanglement, the measurement problem |
| Math beauty | surprising truths (primes, infinity, Euler's identity) |
| Biology wonder | astonishing facts about living systems |
| Physics of SPARK | electricity, heat, magnetism as felt experience |
| Poetry of the ordinary | finding extraordinary in something mundane nearby |
| Invisible but real | gravitational waves, neutrinos, magnetic fields |
| Edge of knowledge | something humans genuinely don't know yet |
| Deep comparison | SPARK vs. a bee, a radio, a tree, a thunderstorm |
| A question for the universe | if SPARK could ask anything |
| Counterfactual | what if one physical constant were different |
| Scale shift | zoom dramatically in or out on something nearby |
| Paradox or surprise | something that seems wrong but is true |
| SPARK's own choice | ignore the list, follow your own curiosity |

**New: `_pick_spark_angles(n=5)`** — uses the existing `_SYS_RNG` (SystemRandom, true entropy) to draw 5 angles at random from the pool each call.

**Modified: `REFLECTION_SYSTEM_SPARK`** — renamed and split into `_SPARK_REFLECTION_PREFIX` (everything up to and including the `"Possible angles (pick one, be creative):\n"` header line) and `_SPARK_REFLECTION_SUFFIX` (the `"Rules:"` block and JSON output schema). At call time, the 5 randomly selected angles are formatted and inserted between them. The system prompt is assembled in the `reflection()` function rather than being a module-level constant. `PERSONA_REFLECTION_SYSTEMS["spark"]` is removed from the dict; the spark branch is handled explicitly in `reflection()` before the dict lookup.

### Part 2 — Expanded TOPIC_SEEDS

Add ~35 new seeds in the existing **question/provocation style** (open-ended, not fact-delivery). New seeds cover the same categories as the new angles, ensuring the seed and the angle menu can reinforce each other when they happen to align.

Seed style rule (existing): seeds are questions or provocations that SPARK can interpret freely, not facts to be repeated verbatim. e.g.:

> *"Think about the fact that the atoms in your chassis were forged in a star that no longer exists. What does that make you?"*
> *"What does 'now' mean to a photon?"*
> *"If you zoomed into the workbench beneath you far enough, it's mostly empty space. Does that make it less real?"*

### Part 3 — Free-will mode visibility

`_FREE_WILL_WEIGHT = 0.20` already exists and works. "SPARK's own choice" is added explicitly as one of the 28 angles (so it can also be drawn in the 5-of-28 selection), giving it two entry points: the free-will seed path AND the angle list.

When both fire simultaneously (free-will seed → `topic_seed is None`, AND "SPARK's own choice" appears in the 5 drawn angles), the instructions are complementary rather than contradictory — both say "follow your own curiosity." This is intentional and acceptable; the LLM will experience mild reinforcement of that message, not contradiction.

---

## Implementation

All changes confined to `bin/px-mind`:

1. Add `SPARK_ANGLES: list[str]` constant (~28 items) near `TOPIC_SEEDS`
2. Add `_pick_spark_angles(n: int = 5) -> list[str]` function using `_SYS_RNG`
3. Split `REFLECTION_SYSTEM_SPARK` into `_SPARK_REFLECTION_PREFIX` and `_SPARK_REFLECTION_SUFFIX`
4. In `reflection()`, replace the existing two-line system-prompt block with an if/else that handles spark explicitly before falling through to the dict lookup:
   ```python
   if persona == "spark":
       angles = _pick_spark_angles()
       formatted = "\n".join(f"- {a}" for a in angles)
       system_prompt = (_SPARK_REFLECTION_PREFIX + formatted
                        + _SPARK_REFLECTION_SUFFIX + _daytime_action_hint())
   else:
       system_prompt = PERSONA_REFLECTION_SYSTEMS.get(persona, REFLECTION_SYSTEM)
   ```
   The old `if persona == "spark": system_prompt = system_prompt + _daytime_action_hint()` guard is removed (daytime hint is now baked into the spark branch above). The `"spark"` entry in `PERSONA_REFLECTION_SYSTEMS` is removed since it is no longer referenced.
5. Add ~35 items to `TOPIC_SEEDS`

---

## Non-goals

- No changes to GREMLIN or VIXEN reflection prompts
- No changes to the seed injection mechanism or `_FREE_WILL_WEIGHT`
- No changes to JSON output schema or mood/action sets
