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

> condition → suppress the explicit audio-producing tool class → presence-safe
> acknowledgement

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

@dataclass(frozen=True)
class PolicyVerdict:
    allowed: bool
    reason: str
    substitute_action: Optional[str] = None
    substitute_params: Optional[dict] = None

def evaluate(
    action: str,
    params: dict,
    *,
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

`evaluate()` is pure decision logic: no file I/O, no subprocess calls, no session
mutation. It reads `session`/`awareness` dicts the caller already loaded and
returns a verdict; the caller remains responsible for executing the substitute or
logging the block.

### v1 rules

Each rule is `(condition) → block {audio-producing tool} → substitute
tool_emote({"name": "idle"})`. The audio-producing set is an explicit, small
constant — not inferred: `{"tool_voice", "tool_announce", "tool_chat",
"tool_chat_vixen", "tool_play_sound"}` (exact membership confirmed against
`ALLOWED_TOOLS` during implementation).

1. `session.get("spark_quiet_mode") is True` → applies to **both** origins.
2. Night-silence window (`NIGHT_SILENCE_START_H`/`NIGHT_SILENCE_END_H`, Hobart
   time, via the existing `_is_night_silence` logic reused not reimplemented) →
   applies to `origin == "interactive"` only. Autonomous night silence keeps its
   existing, separately-tested `mind.py` implementation for v1; migrating it into
   `policy.py` is a deliberate follow-up, not part of this change, to avoid
   duplicating one invariant in two places before there's a reason to.
3. `awareness.get("ha_context", {}).get("adrian_on_call")` or
   `adrian_mic_active` → applies to `origin == "interactive"` only (the
   autonomous case is already correctly handled in `mind.py:3084-3088` and is out
   of scope for this change).

### Substitution safety

A substitute action is re-evaluated through `evaluate()` before being returned,
with `_depth` incremented and capped at 1 (a substitute may not itself produce a
further substitute). This guarantees the constitutional property — *policy
substitution can never recursively produce a less-restricted action* — holds even
as the presence-safe action set changes later, rather than relying on the initial
set being hand-verified safe forever. If a substitute's re-evaluation is itself
blocked, `evaluate()` returns `allowed=False` with no substitute rather than
recursing further.

### Call sites

- `voice_loop.py::validate_action()` — call `policy.evaluate(..., origin="interactive")`
  before dispatch; on a blocked verdict, execute `substitute_action`/`substitute_params`
  if present (still passing through the rest of `validate_action`'s existing
  checks), otherwise skip the turn.
- `mind.py` Layer 3 expression dispatch — call `policy.evaluate(...,
  origin="autonomous")` alongside (not replacing) the existing suppression checks
  at `mind.py:3084-3088`; for v1 this only adds the quiet-mode rule on the
  autonomous path, since night/call suppression already exists there.

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

`tests/test_policy_invariants.py` holds only the pinned constitutional assertions:
quiet mode blocks `tool_voice` regardless of origin, night/call suppression
applies to `interactive` and not (redundantly) to `autonomous`, substitution
re-evaluates and cannot recurse past depth 1, and personas (a session with
`persona` set) do not bypass any rule. Any broader or adaptive policy tests
(e.g. exercising new presence-safe actions as they're added) live in a separate,
whitelisted `tests/test_policy.py`. This closes the failure mode where an
automated evolution PR "passes the tests" by editing the constitution and its
constitution test in the same change — `tests/` is currently blanket-whitelisted
for px-evolve, so this requires adding these two file-level exceptions, not a new
mechanism.

`mind.py` and `voice_loop.py` remain whitelisted (evolution may still legitimately
touch them for unrelated reasons); the blacklisted invariant tests are what
prevents an evolution PR from silently deleting the `policy.evaluate()` call site,
since `pytest` must pass before a PR is created and the invariant tests exercise
real behaviour, not the call site's presence.

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
  above, called directly against `policy.evaluate()` with constructed
  session/awareness dicts — no prompt text involved, satisfying the issue's
  acceptance criterion directly.
- `tests/test_policy.py` (whitelisted): ordinary coverage — audio-tool-set
  membership, non-matching conditions return `allowed=True`, reason strings are
  present on every block.
- `tests/test_voice_loop.py` / `tests/test_mind.py`: extend existing suites with
  one integration case per call site confirming `policy.evaluate()` is actually
  invoked and its verdict respected (catches a call-site deletion even though
  `voice_loop.py`/`mind.py` themselves aren't blacklisted).
