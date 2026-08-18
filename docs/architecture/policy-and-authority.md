# Policy and Authority

**Owns:** who is allowed to do what, and where that is decided. Behavioural
policy (`src/pxh/policy.py`), the trust split between Claude sessions, the
tool whitelist, and the limits on self-evolution.

Hardware authority — which process may hold GPIO — is a different question and
lives in [hardware/gpio-and-alive](../hardware/gpio-and-alive.md).

---

## Invariant

### Intelligence proposes. Policy disposes.

An LLM chooses *what* SPARK should do. Deterministic code decides whether it
*may*. These are separate concerns implemented in separate files, and the
second never asks the first for permission.

Concretely: a persona prompt can say anything at all, and it still cannot make
the robot speak during quiet mode, because the rule is not in the prompt.

### Behavioural invariants live in `policy.py`, and only there

`policy.evaluate()` is pure — no file I/O, no clock, no subprocess, no import
of `mind.py` or `voice_loop.py`, and it never executes anything. It returns a
`PolicyVerdict`. Callers classify their own vocabulary into an `Effect`
(`audio` / `presence` / `other`) and an `Origin` (`interactive` /
`autonomous`) and pass the facts in.

That purity is what makes it testable and what stops it accreting. A new
audio-producing tool inherits every rule by being classified `audio` at its own
call site; `policy.py` never learns a tool name.

The rules, in order:

| Rule | Binds | Blocks when |
|---|---|---|
| 0 — session unavailable | both origins | the session could not be read at all |
| 1 — quiet mode | both origins | `spark_quiet_mode is True` |
| 2 — night silence | interactive only | Hobart hour in the night window |
| 3 — on-call / hot mic | interactive only | `ha_context.adrian_on_call` or `adrian_mic_active` |

The autonomous side's night rule is `mind.NIGHT_ALLOWED_ACTIONS` — the actions
that may still run during the 19:00–07:00 Hobart silence because they make no
sound and no motion: `wait`, `remember`, `research`, `compose`, `introspect`,
`self_debug`, `set_goal`, `update_goal`, `complete_goal`. **SPARK thinks
overnight; it does not speak or move.**

Rules 2 and 3 are interactive-only *by design*: the autonomous loop enforces
its own equivalents (`NIGHT_ALLOWED_ACTIONS`, on-call suppression) in
`mind.py`, with their own tests. Duplicating them here would put one invariant
in two places that can disagree.

### Three enforcement points, and the sink is the one that closes the hole

| Site | Origin | On a blocked verdict |
|---|---|---|
| `voice_loop.validate_action()` | `interactive` | downgrade to a presence-safe substitute |
| `mind.expression()` | `autonomous` | drop the action |
| **`bin/tool-voice`** | `interactive` | `{"status":"suppressed","reason":…}`, exit 0 |

The first two are dispatchers, so they bind only callers that go *through* a
dispatcher. `bin/tool-voice` is the sink every speech producer funnels into,
and it is what anything holding a shell reaches — including the resident
`spark-brain` session, whose tool envelope is SPARK's own `bin/`.

**Do not remove one because another exists.** They are defence in depth
against different classes of caller.

Three properties of the sink are load-bearing:

- **It pins `origin` and `effect` rather than accepting them.** A sink cannot
  know its caller — that is precisely why it needs its own gate. `interactive`
  is the stricter origin, so a wrong guess can only ever suppress. A caller
  that could declare its own effect could declare its way out of the gate.
- **The gate sits above both the persona reroute and the `PX_DRY` branch.**
  `tool-voice-persona` re-enters `tool-voice`, so a gate below the reroute
  would still catch the audio, but only after an Ollama round trip on text
  that was never going to be spoken. And a dry run must model the live
  decision, or every dry test of a speaking route asserts behaviour the robot
  will not show.
- **Substitution cannot recurse.** A blocked verdict with
  `suggest_presence_substitute` makes the caller re-evaluate its substitute at
  `_depth=1`. If the rules would block at `_depth >= 1`, `evaluate()` *raises*
  rather than returning — a presence-safe substitute must never itself be
  `effect="audio"`, and that guarantee is mechanical rather than assumed.

### Reading the facts: session fails closed, awareness fails open

`src/pxh/policy_context.py` is the **only** loader of the session/awareness
facts `policy.evaluate()` refuses to read for itself. Both the dispatcher and
the sink go through it so the two cannot drift. Its two reads have deliberately
opposite postures:

- **Session — fails closed.** `load_session_for_policy()` returns a
  `SessionRead(data, available)`, never a bare dict. A `{}` cannot carry both
  "no quiet flag set" and "no idea"; quiet mode is the dysregulation protocol,
  so resolving the second into the first grants permission to speak during a
  meltdown on the strength of a failed file read. The `except` there is broad
  on purpose — failing closed cannot permit anything — and every failure
  prints to stderr.
- **Awareness — fails open.** An unreadable snapshot yields `{}` and the
  on-call rule goes inactive, rather than muting SPARK for as long as `px-mind`
  is down. `awareness.json` is written by a daemon that is routinely down; the
  session is written by whatever is running. Quiet mode and night silence read
  nothing from this file.

### Every audio producer is inventoried, or the suite fails

`tests/test_policy_invariants.py::AUDIO_PRODUCERS` maps every file in `bin/`
and `src/pxh/` that reaches `aplay`, `espeak`, or a TTS endpoint to a
disposition: `gated`, `self-gated`, `delegates`, `ungated`, `diagnostic`,
`server`, or `mention`.

`test_every_audio_producer_is_inventoried` discovers producers by scanning for
the audio primitives and asserts the discovered set equals the inventory. **A
new file that reaches audio fails the suite until someone classifies it.** A
`delegates` claim is re-verified against the file rather than trusted.

Currently `ungated`, deliberately and on the record: `bin/tool-play-sound`,
`bin/px-perform`, `bin/px-wake-listen`'s chimes, `wander._speak()`,
`mind._play_alarm_beeps()`, and `px-battery-poll`'s plug/unplug tone.
`bin/tool-announce` is `self-gated` — it enforces night silence at its own
relay chokepoint via `policy.is_night_hour()`, so the Nest path and the onboard
speaker cannot disagree about when night is, but it does **not** enforce quiet
mode or on-call.

### Trust boundaries between Claude sessions

Two resident sessions, and the split is a trust boundary, not load balancing.
`spark-brain` runs at the repo root with SPARK's tools. `spark-io` handles text
SPARK did not write, from a cwd *outside* the repository, with exactly one tool.

`brain._IO_KINDS` is the classification. **A new kind that handles untrusted
input must be added to it** — the default routes to the privileged session, so
forgetting is the dangerous direction. See
[architecture/resident-brain](resident-brain.md) for the mechanism.

### The voice loop's tool whitelist

`voice_loop.ALLOWED_TOOLS` is an allowlist, and `validate_action()`
hard-validates every parameter into a range before it becomes an env var. A
tool not in the set cannot be dispatched no matter what the model emits.

Adding a tool is a checklist, not one edit — see
[Adding a tool](../SCRIPTS.md) and the steps in `CLAUDE.md`.

### Self-evolution is bounded by a whitelist *and* a blacklist

`px-evolve` opens a PR; a human merges it. Changes never auto-apply.

`claude_session.file_in_whitelist()` checks the blacklist first, then the
whitelist. Both exist because either alone is fragile: the blacklist names
files that must stay protected even if a future whitelist pattern grows broad
enough to cover them.

`BLACKLIST_FILES` includes `src/pxh/policy.py` and
`tests/test_policy_invariants.py` explicitly, for exactly that reason — the
constitutional layer and the test that pins it must not become evolvable by
accident. Evolvable policy coverage lives in `tests/test_policy.py`; **keep
that split.**

`PX_EVOLVE_MAX_FILES` (default 3) caps the diff, and the branch must pass
pytest before the PR opens.

---

## Why it looks like this

*History, not rule.*

Policy became a module (issue #174) because `voice_loop.py`'s persona swap
**replaces** the system prompt rather than supplementing it. Every safety
behaviour that lived only in prose vanished the moment GREMLIN or VIXEN was
active. Prose could not be the mechanism.

The sink gate (#206) came later and closed a real hole: before it, the only
thing between the resident `spark-brain` session and the speaker at 3am was a
paragraph in a system prompt. The brain's tool envelope is SPARK's own `bin/`,
so it could call `bin/tool-voice` directly, and both dispatcher gates sat
upstream of that call.

Rule 0 (fail closed on an unreadable session) replaced an earlier fail-open
posture. The argument for failing open was that a contended lock would mute
SPARK under load. It bought no such thing: `tool-voice` calls
`update_session()` on that same lock a few lines later and dies there, so
pre-fix contention produced an utterance *and* a traceback. Pinned by
`test_direct_tool_voice_is_silent_while_the_session_lock_is_held` and
`test_direct_tool_voice_is_silent_when_the_session_cannot_be_read`, which
assert against a canary player script on disk rather than against tool-voice's
own JSON — a sink that speaks and then crashes prints no self-report at all.
