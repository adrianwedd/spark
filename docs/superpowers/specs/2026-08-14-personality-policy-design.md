# Personality as Executable Policy — Design

**Issue:** #174 (parent: #169)

## Goal

Separate SPARK's conversational *style* (which may live in prompts, may vary by
persona, may adapt with #172's lived-experience preferences) from a small set of
*behavioural invariants* whose consequences must hold regardless of which prompt is
active, which persona is speaking, or what SPARK has learned. Acceptance per the
issue: load-bearing traits have observable tests that do not depend on exact prompt
wording.

## Audit

Cross-referencing `docs/prompts/*.md` against `src/pxh/voice_loop.py` and
`src/pxh/mind.py` for every trait implied to have a behavioural consequence:

| Trait | Status | Evidence |
|---|---|---|
| Expression cadence (30 min cooldown) | Code-enforced | `mind.py` Layer 3, `EXPRESSION_COOLDOWN_S` |
| Mood→emote/sound mapping | Code-enforced | `MOOD_TO_EMOTE`/`MOOD_TO_SOUND`, used programmatically |
| Night silence, autonomous loop | Code-enforced | `mind.py::_is_night_silence`, `NIGHT_ALLOWED_ACTIONS` |
| Night silence, `tool-announce` | Code-enforced | separate chokepoint, same bounds |
| Presence-aware expression | Code-enforced | awareness/Frigate checks gate Layer 3 |
| On-call/hot-mic suppression, autonomous loop | Code-enforced | `mind.py:3084-3088` blocks `greet`/`comment`/`play_sound`/etc. when `adrian_on_call` or `adrian_mic_active` |
| Connection-before-direction, "we" language | Correctly style-only | no behavioural consequence beyond wording; out of scope unless it implies a concrete forbidden action (e.g. coercive instruction) |
| **Quiet mode / dysregulation protocol** | **Prompt-only — gap** | `spark_quiet_mode` is a session flag surfaced only in `spark-voice-system.md` rule 5; nothing in `validate_action()` blocks `tool_voice` or other tools when it is true |
| **Night silence, interactive voice loop** | **Absent — gap** | autonomous suppression exists; `voice_loop.py` has no night-window awareness at all for Obi-initiated turns |
| **On-call/hot-mic, interactive voice loop** | **Absent — gap** | the "be quiet or whisper" string (`mind.py:1143-1148`) feeds only px-mind's private reflection prompt (`reflection()` → `ha_ctx_text`), never `voice_loop.py`; no volume-control capability exists anywhere in the codebase (no `amixer`/`pactl`), so "whisper" was never implementable — the achievable invariant is binary suppression, matching the existing autonomous pattern |
| **Persona prompt swap (GREMLIN/VIXEN)** | **Confirmed bypass** | `voice_loop.py:947-951` fully *replaces* `current_prompt` with `persona-gremlin.md`/`persona-vixen.md` rather than supplementing it; neither persona prompt mentions `spark_quiet_mode`, so every prompt-only safety behaviour (quiet mode, dysregulation protocol) silently vanishes while a persona is active. Personas share the same `execute_tool`/`validate_action`/`ALLOWED_TOOLS` dispatch as SPARK — no separate path — so a chokepoint there covers them automatically. |

Three real gaps, one shape: **quiet mode**, **interactive night silence**, and
**interactive on-call/hot-mic** all reduce to the same primitive already used for
the autonomous on-call case:

> condition → suppress `effect="audio"` → presence-safe acknowledgement

This is the only abstraction the evidence supports. This design does not build a
general policy engine, a rules DSL, or a learned-policy layer.

## Trust ordering

```
lived-experience preference (#172)  <  semantic action proposal (LLM)  <
behavioural policy (#174, this design)  <  lower-level safety/authority gates
(validate_action param ranges, motion confirmation, GPIO leases, tool whitelist)
```

#172's preferences choose *within* what policy allows; they cannot widen it. The
LLM proposes actions; policy constrains them before they reach execution
mechanics. Policy can downgrade an action's semantics; it cannot bypass a
lower-level safety gate — a substitute action still passes through
`validate_action` and every existing check exactly as if the model had proposed it
directly.

## Module: `src/pxh/policy.py`

```python
from dataclasses import dataclass
from typing import Literal, Optional

Origin = Literal["interactive", "autonomous"]
Effect = Literal["audio", "presence", "other"]

@dataclass(frozen=True)
class PolicyVerdict:
    allowed: bool
    reason: str
    suggest_presence_substitute: bool = False

def is_night_hour(hour: int) -> bool:
    """Pure Hobart-local-hour check. mind.py::_is_night_silence delegates here
    so the clock semantics live in one place without policy.py importing mind.py."""
    ...

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
    ...
```

`origin` is supplied explicitly by the caller — never inferred from session state
or which module happens to be calling. This keeps the two dispatchers legible and
leaves room for a future explicit origin (e.g. `"admin"`) without pretending all
callers are equivalent.

`effect` is also supplied explicitly by the caller, which maps its own action
vocabulary to it locally (see below) — `policy.py` never enumerates tool names or
autonomous action names itself. This is what makes the module vocabulary-agnostic:
`voice_loop.py`'s `tool_voice` and `mind.py`'s `greet` both become `effect="audio"`
at their own call site, and the constitutional rule is just "when suppression
condition X holds, `effect == "audio"` is forbidden."

`evaluate()` is pure decision logic: no file I/O, no subprocess calls, no session
mutation, and no dependency on `mind.py` or `voice_loop.py`. It reads
`session`/`awareness` dicts the caller already loaded and returns a verdict; the
caller remains responsible for choosing and executing its own presence-safe
substitute in its own vocabulary.

### v1 rules

Each rule is `(condition) → block effect="audio"`. No rule currently distinguishes
by `effect="other"`; that value exists for future non-audio, non-presence actions
(e.g. servo motion) so the type is honest rather than a placeholder.

1. `session.get("spark_quiet_mode") is True` → applies to **both** origins.
2. `is_night_hour(...)` (Hobart time, the shared pure helper — see below) →
   applies to `origin == "interactive"` only. Autonomous night silence keeps its
   existing, separately-tested `mind.py` implementation and rule location for v1;
   migrating the *enforcement rule* (not the clock helper) into `policy.py` is a
   deliberate follow-up, not part of this change, to avoid duplicating one
   invariant in two places before there's a reason to.
3. `awareness.get("ha_context", {}).get("adrian_on_call")` or
   `adrian_mic_active` → applies to `origin == "interactive"` only (the
   autonomous case is already correctly handled in `mind.py:3084-3088` and is out
   of scope for this change).

### Shared clock helper (no circular dependency)

`policy.py` owns `is_night_hour(hour: int) -> bool`, a pure function with no
imports from `mind.py`. `mind.py::_is_night_silence(hour)` is changed to delegate
to `policy.is_night_hour(hour)`, preserving its existing call surface for the
autonomous rule at `mind.py:3084-3088` and `NIGHT_ALLOWED_ACTIONS` handling. The
dependency is one-directional (`mind.py` → `policy.py`), so `policy.py` never
imports anything from `mind.py` and there is no cycle.

### Substitution safety

`evaluate()` never chooses the substitute action itself — it cannot, since it
doesn't know either caller's vocabulary. On a blocked verdict with
`suggest_presence_substitute=True`, the caller picks its own presence-safe action
(`voice_loop.py` → `tool_emote({"name": "idle"})`; `mind.py` → its existing
presence-safe autonomous action, e.g. `wait`) and re-evaluates *that* action
through `evaluate()` with `effect="presence"` and `_depth=1` before executing it.
`evaluate()` raises if called with `_depth >= 1` and the rules would block a
`presence` effect (they don't, in v1, but this keeps the guarantee mechanical
rather than assumed) — this is what guarantees *policy substitution can never
recursively produce a less-restricted action* even as the presence-safe action set
changes later.

### Call sites

- `voice_loop.py::validate_action()` — maps its own tool name to `effect` via a
  small local table (`tool_voice`/`tool_announce`/`tool_play_sound`/persona chat
  tools → `"audio"`; `tool_emote`/`tool_look`/etc. → `"presence"`), then calls
  `policy.evaluate(..., origin="interactive")` before dispatch. On a blocked
  verdict, execute the presence substitute (re-evaluated per above, still passing
  through the rest of `validate_action`'s existing checks) if suggested,
  otherwise skip the turn.
- `mind.py` Layer 3 `expression()` — maps its own action name to `effect` via its
  own local table (`greet`/`comment`/`play_sound`/etc. → `"audio"`;
  `look_around`/`scan`/etc. → `"presence"`), then calls `policy.evaluate(...,
  origin="autonomous")` alongside (not replacing) the existing suppression checks
  at `mind.py:3084-3088`. For v1 this only adds the quiet-mode rule on the
  autonomous path, since night/call suppression already exists there via
  `NIGHT_ALLOWED_ACTIONS` and the on-call check.

Each table must be **exhaustive against its own dispatcher's current action
vocabulary**, not permissive-by-default: an action absent from the table is a
lookup error, not a silent `"other"`. `"other"` is a value someone can choose
deliberately for an action, never a fallback. A test in `test_policy.py` asserts
each table's key set equals the dispatcher's actual known-action set (`ALLOWED_TOOLS`
for `voice_loop.py`; the equivalent constant/enum for `mind.py`'s autonomous
actions), so adding a new tool or autonomous action without classifying it fails
CI immediately rather than silently defaulting to unsuppressed audio.

This also means a future audio-producing action in either vocabulary — a new
`tool_voice_v2`, or GREMLIN race commentary — only has to be classified as
`effect="audio"` at its own call site to automatically obey quiet mode, night
silence, and on-call suppression; `policy.py` never needs to learn its name.

### Logging

Both call sites log a structured line on any non-`allowed` verdict:

```
requested=tool_voice verdict=blocked reason=quiet_mode substituted=tool_emote
```

This is the only human-facing surface of a block for now (existing `log()`
helpers in each file); a dashboard affordance is future scope.

## Self-evolution guard

Add to `claude_session.py`:

- `BLACKLIST_FILES` gains `src/pxh/policy.py` and `tests/test_policy_invariants.py`.

`tests/test_policy_invariants.py` holds the pinned constitutional assertions, and
critically, at least one of them per chokepoint exercises the **real integration
path** — `voice_loop.validate_action()` and `mind.expression()` imported and
called directly — not `policy.evaluate()` in isolation. A direct-only test suite
would leave a real loophole: an evolution PR could leave `policy.py` and the
invariant tests untouched, delete the `policy.evaluate()` call from
`voice_loop.py`, and adjust the (whitelisted) `tests/test_voice_loop.py`
accordingly — the protected suite would still pass. Pinning "the rule exists" *and*
"each required chokepoint actually invokes it" in the same blacklisted file closes
that. Concretely: a test that sets `spark_quiet_mode=True` in a constructed
session, calls `voice_loop.validate_action({"tool": "tool_voice", ...})` (or the
equivalent for `mind.expression()`), and asserts the *actual dispatched tool* was
downgraded to the presence substitute — not just that `policy.evaluate()` alone
returns `allowed=False`. This does not require blacklisting all of
`test_voice_loop.py`/`test_mind.py`; only this one small file needs the
protection, and it earns it by testing the wiring, not just the module.

Any broader or adaptive policy tests (e.g. exercising new presence-safe actions as
they're added, or additional `effect` classifications) live in a separate,
whitelisted `tests/test_policy.py`. This is what keeps ordinary policy test
evolution possible while the constitutional wiring stays protected — `tests/` is
currently blanket-whitelisted for px-evolve, so this requires adding these two
file-level exceptions, not a new mechanism.

`mind.py` and `voice_loop.py` remain whitelisted (evolution may still legitimately
touch them for unrelated reasons); it's the blacklisted integration assertions
above — not file protection on `mind.py`/`voice_loop.py` themselves — that catch a
call-site deletion, since `pytest` must pass before a PR is created.

## Non-goals

- No general policy engine, rules DSL, or configuration-driven rule authoring.
- No migration of autonomous night-silence or autonomous on-call suppression into
  `policy.py` — both already work and are already tested; duplicating them here
  before there's a second reason to would be premature abstraction.
- No volume/audio-level control. "Whisper" is retired as a concept; the
  implementable invariant is binary suppression.
- No changes to `docs/prompts/*.md` conversational style — connection-before-
  direction, "we" language, tone, etc. stay exactly where they are.

## Testing

- `tests/test_policy_invariants.py` (blacklisted): the constitutional assertions
  — quiet mode blocks `effect="audio"` regardless of origin, night/call
  suppression applies to `interactive` and not (redundantly) to `autonomous`,
  substitution re-evaluates and cannot recurse past depth 1, personas don't
  bypass any rule — with at least one assertion per chokepoint driven through the
  real entry point (`voice_loop.validate_action()`, `mind.expression()`), not
  `policy.evaluate()` alone. No prompt text involved anywhere in this file,
  satisfying the issue's acceptance criterion directly.
- `tests/test_policy.py` (whitelisted): ordinary coverage — effect-mapping table
  completeness per caller, non-matching conditions return `allowed=True`, reason
  strings present on every block, `is_night_hour()` boundary cases.
- `tests/test_voice_loop.py` / `tests/test_mind.py`: may separately grow their own
  ordinary coverage of the new behaviour; not relied upon for the erosion
  guarantee, since only the blacklisted file is protected from evolution.
