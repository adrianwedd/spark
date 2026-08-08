# On-Demand Reflection Context — Design

**Date:** 2026-08-05
**Status:** Approved for planning
**Goal:** SPARK stops receiving a verbose awareness dump in every reflection prompt
and instead asks for the intel it needs through a small set of named tools, running
free on M5 Ollama.

## Motivation

Every reflection currently ships the entire awareness object as indented JSON
(`src/pxh/mind.py:2697`), then re-states much of the same material in prose below it.
SPARK reads a wall of telemetry to answer a question that usually turns on one fact.

Three things follow from that, all of them measured on the live robot on 2026-08-05:

- The prompt is **~3,277 estimated input tokens** per reflection.
- The dump leaks private data. `awareness["findmyhub"]` carries Obi's raw
  coordinates and is sent to the model every tick — see *Privacy* below.
- SPARK cannot follow its own curiosity. It gets what `awareness_tick` happened to
  collect 60 seconds ago and nothing else, whether or not it is relevant.

## Measurements (live Pi, 2026-08-05)

Reflection prompt composition:

| part | ~tokens |
|---|---|
| system prompt (`_SPARK_REFLECTION_PREFIX` + `_SUFFIX`, 3,959 chars) | 989 |
| raw awareness JSON dump (`mind.py:2697`, 5,078 chars) | 1,269 |
| prose context + recent thoughts/moods | ~1,000 |
| **total** | **~3,277** |

Inside the dump, by serialized size: `recent_conversations` 857 chars, `frigate` 586,
`ha_calendar` 503, `weather` 296, `findmyhub` 285, `ha_presence` 283, `system` 178,
`ha_context` 118, `next_event` 117; everything else under 60. All HA-derived keys
together are ~947 chars (~237 tokens) — about 7% of the prompt.

**This matters for expectations.** HA is not the bulk of the verbosity. Removing it
alone would save little; the win comes from replacing the whole dump.

## Model probe (2026-08-05)

The design depends on the reflection model reliably calling a tool when the context
withholds the answer, then returning schema-valid JSON. Probed against real M5 Ollama
with a slim context that deliberately omitted presence, 2 tools offered, 4 trials each:

| model | called a tool | valid JSON | schema ok | median | turns / calls |
|---|---|---|---|---|---|
| **gemma4:12b** | 4/4 | 4/4 | 4/4 | **5.8s** | 2 turns, exactly 1 call |
| ornith:9b | 4/4 | 4/4 | 4/4 | 16.6s | up to 5 turns, 9 calls |
| llama3.2:1b | 4/4 | 1/4 | 1/4 | 1.4s | unusable |

All three advertise `tools` in `/api/show` capabilities. Advertised is not the same as
disciplined:

- **gemma4:12b** asks the single question the seed requires, answers, stops. It never
  called the second tool it did not need. This is the behaviour that makes pull-based
  context cheaper rather than more expensive.
- **ornith:9b** thrashes — one trial issued `ha_query ×3 → presence_query → ha_query ×4`
  across five turns, and the resulting thought degenerated into inventory
  ("Everything is on — lights, speaker, thermostat, all the switches"). It is the
  direct evidence for the caps in §1.
- **llama3.2:1b** returns prose instead of JSON in 3 of 4 trials.

For reference, current reflection latency in the live log is 15–25s on Ollama and
57–76s on the Claude fallback. A two-turn gemma4 tool loop at 5.8s is **faster than
what it replaces**, because the prompt is a fraction of the size.

The probe was run ad hoc against M5 on 2026-08-05. It does **not** yet exist in the
repo; §6 specifies committing it as `tests/test_reflection_tools.py`.

All live-robot figures in this spec — token counts, DNS results, latency, log counts,
model behaviour, awareness contents — were measured on 2026-08-05 and are not
reproducible from a checkout. Treat them as dated observations, not invariants.

## Non-goal: this is not primarily a token-reduction change

A tool loop re-sends the context on every turn, and the tool schemas are not free.
Measured by serializing the five definitions in §2: **1,002 chars ≈ 250 tokens**, not
the ~40 an earlier draft of this spec claimed.

Revised arithmetic:

| | ~tokens |
|---|---|
| trimmed system prompt | 989 |
| slim context (§3) | 250 |
| 5 tool schemas | 250 |
| **base prompt** | **~1,490** |
| turn 1 | 1,490 |
| turn 2 (base + tool call + result) | ~1,570 |
| **total, typical 2-turn reflection** | **~3,060** |

Against today's ~3,277 that is **roughly a 7% reduction — effectively nothing.** A
re-roll makes it worse than today.

**This design does not save tokens. Do not justify it on that basis.** It is worth
doing because it runs free on M5 rather than on paid Claude, latency drops (5.8s
measured vs 15–76s today), SPARK reasons from answers instead of parsing a telemetry
wall, and two live privacy leaks disappear. If token count is the actual goal, trim
the 989-token system prompt and use a tiered static context — that is a different,
cheaper change and it would beat this one.

## Prerequisite

**The M5 resolution fix must land first.** As of 2026-08-05 the Pi cannot resolve the
plain router-DNS name `M5` at all (`getent hosts M5` → rc=2), because M5's lease moved
192.168.0.100 → 192.168.0.249 and it is dual-homed (ethernet/wifi), so the router-DNS
name binds to whichever lease was recorded last. `mind.py:313` hardcodes
`http://M5:11434`. Consequence: 639 `falling back to claude` events in `px-mind.log`,
126 of them on 2026-08-05 alone, and every recent thought served by paid Claude Haiku.

mDNS is unaffected by the interface switch because it is advertised per host. Measured
from the Pi: `M5.local` → 192.168.0.249, 5/5 resolutions in 18–33 ms, and
`http://M5.local:11434/api/tags` returns 200 with 4.6 ms DNS. Fix is
`PX_OLLAMA_HOST=http://M5.local:11434`.

Note `mind.py:311-312` carries a comment asserting the opposite ("router DNS via UDR7,
not M5.local mDNS"); it is now contradicted by measurement and should be corrected in
the same change.

None of this design runs on the free tier until that lands.

## 1. Reflection becomes a bounded tool loop

`call_llm()` gains a tool-calling path, used when the serving backend advertises
`tools`. The base prompt is trimmed system + slim context + 5 tool definitions
(~250 tokens of schema — measured, see *Non-goal*). The loop runs until the model
returns a thought instead of a
tool call.

**Caps — all three are driven by the ornith:9b result, not by theory:**

- **max 3 turns**
- **max 5 tool calls total** across the reflection
- **repeated `(name, args)` pairs are served from an in-reflection cache** rather than
  re-dispatched, and the cached result is returned again so the model is not confused
  by a missing reply

Hitting a cap is **not** an error. The loop makes one final call appending
`"Answer now with your JSON."` and takes whatever comes back, which then flows through
the existing `_reroll_reason` path (`mind.py:2605`) like any other reflection.

**The forced call is an extra turn, and tools are withheld on it.** Turn budget is
therefore 3 normal turns + 1 forced = 4 calls maximum. The forced call omits the
`tools` key entirely, so the model cannot answer it with another tool call and the
loop cannot recurse. If the forced call *still* fails to yield parsable JSON, the
reflection is abandoned and reported as a normal skip — it is not retried.

**If a response carries both `content` and `tool_calls`, the tool calls win** and the
content is discarded. gemma4:12b did not do this in any trial, but the ambiguity must
not be left to the implementer: treating a response as final while it is still asking
questions would strand the tool result and produce a thought reasoning from data it
never received.

gemma4:12b needed 2 turns and 1 call in 4/4 trials, so these caps should never bind in
practice. They exist so that a model swap degrades instead of hanging px-mind.

The loop must never raise into `mind_loop`. Any exception falls through to the
degraded path in §5.

### The HTTP plumbing does not exist yet — this is most of the work

**`call_ollama()` posts to `/api/generate` (`mind.py:2348`). Ollama's tool calling
lives on `/api/chat`.** `/api/generate` takes a flat `prompt` string, returns flat
text, and does not accept a `tools` key or return `tool_calls`. The probe in this spec
worked because it called `/api/chat` directly; the production path cannot.

Do **not** switch `call_ollama()` over — it is on the hot path for every reflection
and every persona, and `/api/chat` has a different request shape (`messages` array
rather than `prompt`) and a different response shape.

Add a **separate `call_ollama_chat()`** alongside it, sharing the existing host-failure
backoff (`_host_failure_until`, `mind.py:2320-2325`), auth-token and timeout logic.
`call_ollama()` is left untouched, so the degraded path and persona reflections keep
their current behaviour bit-for-bit.

This is the largest single piece of implementation in the design and the plan should
sequence it first.

### Capability detection is a new endpoint, not a lookup

**`/api/show` does not appear anywhere in `src/pxh/`.** The existing model resolution
(`_resolve_ollama_model`, `mind.py:335-346`) uses `/api/ps` then `/api/tags`; neither
returns `capabilities`. Detection therefore needs a new `POST /api/show` with
`{"model": name}`, reading the `capabilities` array (`gemma4:12b` returns
`['completion','vision','audio','tools','thinking']`).

Requirements:

- Probe **once at startup**, not lazily on first reflection, so a cold probe never
  adds latency to a thought.
- A probe failure (404, timeout, host down) means **assume no tools** and take the §5
  degraded path. Fail closed.
- Cache per `(host, model)`. **Bound the cache lifetime to the existing model cache
  TTL** rather than the process lifetime: `_resolve_ollama_model` re-resolves the model
  every 1800s, so a model swap on M5 can change the model under a process-lifetime
  capability cache, leaving it asserting `tools` for a model that has none. Re-probe
  whenever the resolved model name changes.

### The loop belongs to one tier, and never spans two

`call_llm()` (`mind.py:2505`) owns the four-tier fallback. The loop must therefore be
scoped *inside a single tier attempt*, not wrapped around `call_llm()`:

- `call_llm()` gains a `tools=` parameter. When supplied **and** the tier being tried
  advertises `tools`, that tier runs the whole loop itself.
- **A tier never hands a half-finished loop to the next tier.** If any turn fails
  (timeout, socket error, malformed response), the entire loop for that tier is
  abandoned and fallback proceeds to the next tier *from scratch*, using the §5
  single-shot degraded context. Partial tool results are discarded.
- `result["backend"]` is therefore the single tier that completed the loop, and stays
  meaningful.

Without this, a turn-2 timeout on M5 would resume the conversation on Claude Haiku —
a multi-turn paid loop, which §5 forbids.

### Interaction with the re-roll loop

`reflection()` already makes **up to two attempts** (`mind.py:3023`), re-calling
`call_llm` with a fault hint when `_reroll_reason` returns `empty`, `similar` or
`no_json` (`mind.py:3035-3061`). Attempt 2 is gated by `_reroll_allowed(backend)`
(`mind.py:2649`), which permits free backends only.

**A re-roll must not run a second tool loop.** 4 turns × 2 attempts = 8 calls per
reflection, for a model that needed 1. Instead:

- Tool results from attempt 1 are **retained and passed into attempt 2 as context**,
  alongside the existing `_REROLL_HINTS` text.
- Attempt 2 is a **single-shot call with no `tools` key.** The model already has the
  answers; the re-roll exists because the *thought* was blank or duplicated, not
  because the intel was wrong.
- Total per reflection is therefore bounded at 4 calls (attempt 1 loop) + 1
  (attempt 2) = 5.

This also keeps `_reroll_allowed` honest: it reads one unambiguous backend, set by the
tier that owned the loop.

### SPARK persona only

`mind.py:2966-2970` builds two different system prompts: the SPARK persona gets
`_SPARK_REFLECTION_PREFIX + formatted + _SPARK_REFLECTION_SUFFIX`, while GREMLIN and
VIXEN get `PERSONA_REFLECTION_SYSTEMS` (`mind.py:547`).

**The tool loop applies to the SPARK persona only.** GREMLIN and VIXEN keep the
existing single-shot path unchanged. Their reflections are character performances, not
household reasoning, and giving them `who_is_home()` would both change their voice and
route household presence data through two more prompt surfaces for no benefit.

Implementation consequence: the `tools=` argument is passed by the SPARK branch only.
The persona branch must be left alone, and a test should assert a persona reflection
issues exactly one call with no `tools` key.

### On the concern that a JSON-only instruction suppresses tool calls

Worth recording because it looks like a real risk and is not: a reviewer raised that a
system prompt demanding "reply with ONLY a JSON object" would stop a model emitting
native tool calls, or force it to emit them as text.

The probe tested exactly this. Its system prompt contained *"When you have what you
need, reply with ONLY a JSON object"* **and** offered tools; gemma4:12b called a tool
in 4/4 trials and then returned schema-valid JSON. The two instructions coexist. No
split into a "tool phase" prompt and a "synthesis" prompt is required.

### The awareness snapshot is frozen for the whole reflection

`awareness_tick` runs every 60s and rewrites the awareness object. A reflection can
straddle a tick — gemma4's median was 5.8s but the caps allow four calls.

`reflection()` must take an immutable copy (`copy.deepcopy`) of awareness at entry,
and every tool dispatch in §2 must read *that* copy, not live state. Otherwise
`who_is_home()` on turn 1 and `what_changed()` on turn 3 can describe two different
moments, and SPARK reasons across a seam. The frozen copy's timestamp is what `age_s`
is computed against.

## 2. The five tools read the awareness snapshot, not HA live

| tool | returns |
|---|---|
| `who_is_home()` | `{"rooms": [...], "confidence": float, "age_s": int}` |
| `whats_on_today()` | `{"next": str｜null, "later": [...], "age_s": int}` |
| `house_state()` | `{"office_light": str, "adrian_on_call": bool, "media": str｜null, "age_s": int}` |
| `my_vitals()` | `{"battery_pct": int, "charging": bool, "cpu_temp_c": float, "sonar_cm": float, "age_s": int}` |
| `what_changed()` | `{"transitions": [...], "since_s": int}` |

No free-text entity access. SPARK cannot reach arbitrary HA entities, so both the
blast radius and the tool-definition token cost are fixed.

**Source fields, so the implementer invents nothing.** These names differ between the
two presence sources and must be read from the frozen snapshot:

| tool | reads |
|---|---|
| `who_is_home()` | `frigate.rooms_with_people` (room names); `ha_presence` for named people |
| `whats_on_today()` | `next_event`, `ha_calendar` |
| `house_state()` | `ha_context` (`office_light`, `adrian_on_call`, `adrian_mic_active`, `media_playing`, `media_title`) |
| `my_vitals()` | `battery_pct`, `battery_charging`, `system.cpu_temp`, `sonar_cm` |
| `what_changed()` | `transitions` |

`confidence` on `who_is_home()` is **passed through from Frigate if present and
omitted otherwise.** Do not synthesise a score — an invented number is worse than a
missing key, because SPARK will reason about it. If neither source has data the tool
returns `{"unavailable": true, "reason": "no camera or presence data"}`.

`who_is_home()` returns room names and people names only — never coordinates. See
*Privacy* in §3.

**Why snapshot and not live HTTP.** `awareness_tick` already polls HA every 60s, and
today's `px-mind.log` contains repeated `ha_context: global timeout (5s) — HA
unreachable`, `[Errno 113] No route to host`, and `HTTP Error 404`. Putting a live
call inside the reflection loop imports those failures into the thought path, where a
hang blocks cognition rather than degrading one field. Reading the snapshot preserves
the actual win — SPARK pays tokens only for what it asks — with no new network
fragility.

Every result carries `age_s` so SPARK knows how stale the answer is and can say so.
A tool whose backing data is missing returns `{"unavailable": true, "reason": str}`
rather than raising or returning an empty dict that reads as "nobody home".

If a specific tool later needs true freshness it can opt into a live call
individually; `house_state()` is the likely first candidate. That is out of scope here.

## 3. Static context shrinks to what SPARK cannot ask for

The slim context (~250 tokens) keeps only: time and time period, **battery percent and
charging state**, SPARK's own recent moods and actions, the active goal, and the
reflection seed.

`json.dumps(awareness_ctx, indent=2)` at `mind.py:2697` is **deleted**.

Battery stays static rather than tool-gated because it drives safety behaviour
(`BATTERY_WARN_20`, `BATTERY_CRITICAL`) and SPARK must never have to *ask* whether it
is about to die. `my_vitals()` covers the non-critical remainder — CPU temperature,
sonar distance, disk and memory pressure. An earlier draft listed vitals in both
places; this is the resolution.

### "Everything else becomes a tool" is not true, and the difference matters

The prose context assembled at `mind.py:2715-2940` currently also carries: recent
conversations, retrieved memories, persona notes, daemon health summary, sleep and
routine state, weather, exploration observations, and non-person camera sightings.
**No tool in §2 replaces any of these.** Deleting the dump without a decision on each
is a cognition and personality regression dressed up as prompt trimming.

Worse, several of them feed memory retrieval rather than only the prompt: `query_bits`
at `mind.py:2726-2735` builds the memory query from transitions, conversations,
calendar and Frigate rooms. Removing them silently degrades which memories SPARK
recalls, which is invisible in the prompt diff.

**The plan must make an explicit keep / tool-ify / retire decision for each**, recorded
in the plan document. The default is **keep in the slim context** — this design is
about removing the raw JSON dump and the duplicate representations, not about starving
reflection. In particular `recent_conversations` (857 chars, the single largest key)
and retrieved memories should be assumed *kept* unless a specific case is made.

### Privacy

Deleting the dump closes **two** live leaks. `mind.py:2695` filters exactly one key:

```python
awareness_ctx = {k: v for k, v in awareness.items() if k != "health"}
```

**Leak 1 — `findmyhub`.** Set at `mind.py:2113`. Live `state/awareness.json` on
2026-08-05: `{"obi_chipolo": {"lat": -43.16396, "lon": 147.08366, "distance_km": 4.22}}`.

**Leak 2 — `ha_presence`.** `_fetch_ha_presence` attaches per-person coordinates at
`mind.py:858-863`, rounded to 5 decimal places with GPS accuracy. Live on 2026-08-05:

```json
{"name": "Adrian", "state": "home", "home": true,
 "lat": -43.13558, "lon": 147.11829, "gps_accuracy_m": 5.0}
```

Adrian is home, so those are the house coordinates at 5-metre accuracy, sent to the
model on every reflection.

The comment at `mind.py:2829-2831` asserts tracker locations are excluded from
reflection context. They are not — the dump above it already sent both. Thoughts feed
`/api/v1/public/thoughts` and px-post.

No tool in §2 exposes coordinates. `who_is_home()` returns room and person *names*
with their home/away state, and strips `lat`, `lon` and `gps_accuracy_m`.

**If this design is delayed, the interim filter fix must ship separately and must
cover both leaks.** Dropping only `findmyhub` — the obvious one — leaves the house
address flowing through `ha_presence`. The safer interim fix is an allowlist of keys
to *include* rather than a denylist of keys to exclude, so a newly added awareness key
cannot silently reopen this.

## 4. Logging and visibility

Every turn appends to **`state/reflection_trace.jsonl`**: timestamp, model, backend,
turn index, tool name, args, result, per-turn latency, cap-hit flag, and a
`reflection_id`.

**Persisted thoughts have no id.** The thought dict built at `mind.py:3087` carries
`ts`, `thought`, `mood`, `action`, `salience` and nothing else, and `append_thought()`
(`mind.py:2244`) adds none. So the trace cannot reference a thought id that does not
exist. Instead: generate `reflection_id` at the *start* of the reflection, write it on
every trace record, and add the same `reflection_id` to the persisted thought. Turn
records are written as they happen (before the thought exists), and the id is what
joins them afterwards.

A reflection that is abandoned — no JSON, empty thought, suppressed as similar — still
leaves its trace records, with no thought carrying that id. That asymmetry is a
feature: it is how you see reflections that burned tool calls and produced nothing.

This is deliberately **not** written into `thoughts-spark.jsonl`, because that feeds
`/api/v1/public/thoughts`. Tool results contain presence and calendar data, and §3
documents one privacy leak that reached the public endpoint through exactly that path.

`px-mind.log` gets one human-readable summary line per reflection:

```
asked: who_is_home → office (age 34s) · 2 turns · 5.8s · gemma4:12b
```

That line is what the resident tmux Claude session, `px-motd`, and a human tailing the
log will actually read. It also makes ornith-style thrash visible instead of silent —
a reflection that burned 9 calls says so on one line.

`state/reflection_trace.jsonl` is **not** served by any public API endpoint.

**Rotation must be explicit — there is no shared policy to inherit.** `rotate_log()`
exists at `src/pxh/state.py:100` (default `max_bytes=5_000_000`, `.rotlock`
convention) but its only callers are log writers: `src/pxh/logging.py:65,84` and
`src/pxh/mind.py:598`. No `state/*.jsonl` file is rotated by it —
`thoughts-spark.jsonl` is 10,000 lines on the live Pi. This file writes one record
*per turn*, so at ~288 reflections/day it grows faster than thoughts do. The
implementer must call `rotate_log()` on it explicitly with a stated `max_bytes`,
holding the `.rotlock` as the existing callers do.

## 5. Degraded path

If the serving backend does not advertise `tools` — Claude fallback, Ollama Cloud, a
model swap, a probe failure — reflection falls back to **single-shot with a compact
allowlisted context**: the slim context of §3, plus one line per tool giving what that
tool would have returned, rendered by the same dispatch functions:

```
Who is home: office (34s ago)
Today: school pickup 15:20
House: office light on, Adrian not on a call
Changed since last reflection: obi_arrived_home
```

Rendering reuses the §2 dispatch, so the fallback cannot drift from the tool
behaviour. An `unavailable` tool renders as `Who is home: unknown (no camera data)`
rather than being omitted — a missing line reads as "nobody home", which is the
failure mode this whole design is meant to remove.

This keeps two properties:

- A Claude fallback **never** runs a multi-turn paid loop. Reflection on Claude stays
  one call, as today.
- The awareness dump does not come back as the fallback. The allowlist is the fallback.

Backend tool-capability is determined from Ollama `/api/show` `capabilities` and
cached per (host, model) for the process lifetime, not re-probed per reflection.

## 6. Testing

**Committed live probe — `tests/test_reflection_tools.py`, marked `live`.** Asserts
that the pinned model, given a context withholding presence, (a) calls a tool, (b)
returns schema-valid JSON, (c) does so within the turn cap. A model upgrade that
breaks tool discipline then fails CI instead of silently making SPARK stupid. The
llama3.2:1b result (1/4 valid JSON) is exactly what this catches.

**Unit tests against a fake Ollama** (no network, standard suite):

- turn cap enforced, and the final forced-answer call is made
- tool-call cap enforced
- duplicate `(name, args)` served from cache, not re-dispatched
- malformed tool call (bad JSON args, missing `name`) degrades without raising
- unknown tool name returns an error result rather than raising
- tool backed by missing data returns `{"unavailable": true, ...}`
- degraded path taken when backend reports no `tools` capability
- `reflection_trace.jsonl` written with one record per turn
- no exception escapes into `mind_loop`

**Privacy regression test:** build a reflection prompt from an awareness fixture that
populates **both** `findmyhub` and `ha_presence` with coordinates, then assert the
strings `lat`, `lon`, `gps_accuracy_m`, `findmyhub` and the literal coordinate values
appear nowhere in the assembled prompt — nor in any tool result from §2. Testing only
`findmyhub` would pass while the house address still leaks. This should be added even
if the loop slips.

**Test isolation is mandatory and is not inherited.** `tests/conftest.py` has exactly
one autouse fixture — it redirects `health_dir()` to tmp. `isolated_project` is
**opt-in** and mainly isolates subprocesses. An in-process reflection test that writes
`state/reflection_trace.jsonl` will therefore write into the **live robot's state
directory**, exactly the hazard CLAUDE.md documents for health records. Add an
**autouse** fixture redirecting the trace path to tmp, and resolve the trace path from
`PX_STATE_DIR` at call time rather than binding it at import.

**Ollama transport tests:** assert `call_ollama()` still posts to `/api/generate` and
is byte-for-byte unchanged in request shape (it remains the path for personas and the
degraded fallback), and that `call_ollama_chat()` posts to `/api/chat` with a
`messages` array. A regression that quietly points the old function at `/api/chat`
would break every persona reflection.

## Files touched

| file | change |
|---|---|
| `src/pxh/mind.py` | new `call_ollama_chat()` (`/api/chat`); `tools=` path in `call_llm()`; startup capability probe (`/api/show`); delete dump at 2697; slim context; freeze snapshot in `reflection()`; correct comment at 311-312 |
| `src/pxh/reflection_tools.py` | **new** — the 5 tools, snapshot readers, dispatch, cache, degraded-path rendering |
| `src/pxh/spark_config.py` | tool definitions, caps, trace path |
| `tests/test_reflection_tools.py` | **new** — live probe + unit tests |
| `.env` (Pi, not in repo) | `PX_OLLAMA_HOST=http://M5.local:11434` |

`mind.py` is 4,042 lines. The tools, dispatch and cache go in a new
`src/pxh/reflection_tools.py` rather than growing it further; `mind.py` keeps only
the loop.

## Out of scope

- Live HA calls from inside a tool (§2)
- Free-text entity access (rejected: 2,440 entities on the HA instance; a generic
  query tool is how a disciplined model starts behaving like ornith)
- Changes to expression, the action menu, or goals — separate work
- The `claude -p` removal and resumed-session work — separate, though the trace file
  in §4 is what makes the resident session useful for watching this
