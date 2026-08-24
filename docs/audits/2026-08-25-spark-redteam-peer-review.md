# Adversarial Peer Review of the 2026-08-25 SPARK Architecture Red-Team

Independent frontier-model review of
`docs/audits/2026-08-25-spark-architecture-redteam.md` (hereafter "the report").
Commission: attack it. Do not accept its framing, rankings, or recommendations
because a frontier model wrote them.

**Method.** Every top-ten claim was re-traced to file:line by four read-only
`spark-investigator` passes *and* by the reviewer directly (the crown jewels —
B1, B3, B4, B7a, B9 — were read first-hand, not delegated). Live state on the
production Pi was read at review time. Where I confirm the report I say so;
where it overreaches I say where and by how much.

**Headline judgement.** The report is *factually* excellent — nearly every
file:line citation checks out, and the two crown-jewel findings (memory
plumbing, motion latch) are real and correctly aimed. But it has a **systematic
framing bias in exactly one direction**: it dresses *same-uid trust
assumptions* as *security-boundary bypasses* (B1, and the health/lease
honorable mentions), and it dresses *known, test-pinned, documented dead code*
as *latent architectural chaos* (B2). Both inflate consequence. Symmetrically,
it **under-states** the finding that matters most (B3/B4 memory) and **gets its
flagship agency claim wrong on its own cited line** (B9). Net: the ranking is
miscalibrated. The security findings should move down; the memory/learning
findings and one live-broken pipeline should move up.

---

## 1. Verdict on the top ten

| # | Claim (short) | My verdict | The correction that matters |
|---|---|---|---|
| B1 | Brain reply forgeable | **VERIFIED mechanism / OVERSTATED as #1 security** | Same-uid capability, not a boundary bypass; strongest sink re-gated. But feasibility *understated* — id is world-readable, no uuid guess needed. |
| B2 | Reflection routing contradicted; 2 dead fns | **VERIFIED facts / OVERSTATED framing** | Dead code is test-pinned + documented, not drift. Real residue = one stale config line. Low consequence. |
| B3 | Memory reaches only reflection | **VERIFIED** (correctly ranked) | Sole prod caller confirmed. One citation misattributed (obi-chat quote is from the *public* chat prompt). |
| B4 | Durable memory is a SPARK diary; writer stranded | **VERIFIED and UNDERSTATED** | Stranded *twice* (wrong file + raw-notes path itself dead). But the stranded records are lower-value than implied. |
| B5 | Reflection = one LAN hop, no fallback; 2nd-order memory cost | **PARTLY TRUE** | "Hardcoded" is env-overridable; wrong 2nd constant cited; "empties" = "24h window empties." The *live* break is different (see §5.1). |
| B6 | `confirm_motion_allowed` bare bool, no provenance | **VERIFIED** | Live value is `true` now with no actor/TTL. Default is safe (`false`); API has a confirm-gate but it still records no provenance. |
| B7 | Successful-nothing vs broken-nothing collapse | **VERIFIED (observability, not safety)** | Live-confirmed: health `ok` while last_error = "M5 busy"/"handshake failed". |
| B8 | No physical receipt on 3/4 actuator paths | **VERIFIED** | Zero encoder/IMU in `src/pxh`; announce `cast_ok` = HA accepted, not played. |
| B9 | Initiative = timer over static menu; perception is garnish | **PARTLY TRUE — flagship line WRONG** | "No experience ever changes a prompt" is contradicted by its own cited line (`memory.py:299-307` reads outcomes back). Counts wrong (25 not 24; 75 not 90). |
| B10 | Delegated confinement real at tool layer, fake at identity layer | **VERIFIED** | Accurate as stated. |

### The two findings the report got most wrong (in opposite directions)

**B1 is over-ranked.** `collect_reply` (`brain.py:775-798`) does read an
unauthenticated `outbox/<id>.json` — I read it directly; there is no nonce, no
signature, no writer check, only `isinstance(reply, dict)`. And the report
*under*-sells feasibility: because the mailbox is `1777` (`brain.py:221`) the
in-flight `request_id` is a *filename in a world-readable directory* and is also
written to `current.json`, so a co-resident process needs no uuid guess — it
`readdir`s and races a file write against a multi-second Claude turn. So far the
report is if anything too generous.

But the *consequence* ranking is wrong. This is a **same-uid trust assumption,
not a privilege boundary.** `tool-brain-reply`'s own header states its threat
model: the untrusted party is *the language model that may have read a
stranger's text*, never other OS processes. Any `pi` process that can write
`outbox/` can already, at the same uid: call `bin/tool-voice` directly, append
to `notes-spark.jsonl`, write `feed.json`, or edit `session.json`. Every sink
the report lists ("speech / memory / notes / public blog + Bluesky") is
*directly* reachable without touching the mailbox. The forged-reply path grants
**no capability the attacker doesn't already have**, and the report's own
parenthetical concedes the one sink with teeth (`voice_turn` → action) is
re-gated by the `tool-voice` policy sink and `confirm_motion_allowed`. B10 then
admits the whole identity layer is same-uid. So B1 is not a security bypass; it
is a **robustness gap against a *confused* co-resident process** — which is a
real thing (it is exactly the #281 delegated-fork incident), and a per-request
nonce is a cheap, correct defense-in-depth. It is a P1 hardening, not the P0
"highest blast radius" the report makes it.

Corollary — the health honorable mention has **no safety consequence at all**:
the battery emergency shutdown (`mind.py`) re-reads *live voltage* via
`read_battery()` and never consults a health `ok`. A forged health record
misleads a dashboard, not a shutdown. The report calls this out as a boundary;
it is cosmetic.

**B9's flagship sentence is false.** "No experience ever changes a parameter,
threshold, prompt, or preference outside a human-merged px-evolve PR" is
contradicted by the report's *own citation*. `memory.py:299-307` reads the last
30 action **outcomes** back out of session history and injects them into the
nightly consolidation prompt; consolidated memories are then retrieved into
future reflection prompts (`mind.py:2570`), alongside the last-5 moods/actions
and mood momentum (`MOOD_ALPHA=0.55`). There *is* an
experience→memory→prompt→selection loop. It is language-mediated, not
parametric, and it is weak — but "stochastic garnish" and "no experience ever
changes a prompt" are both wrong. The claim that survives is *narrower*: no
**numeric** weight, threshold, cooldown, or the action space itself ever mutates
from experience. Hold that narrower claim; drop the absolute one. (Also: 25
actions not 24, 75 topic seeds not 90, and 3 of the 4 live goals in
`intention-spark.json` are original situated compositions — one reached `done`
with three progress notes — not the verbatim-angle echo the report quoted.)

### Where the report is right and well-aimed

B3, B4, B6, B7, B8 all hold on first-hand reading, and B4 is if anything
*understated*: reflection's raw-notes fallback (`mind.py:2567-2586`) is itself
dead code — it only fires when `has_memory_store("spark")` is False, but the
store exists and is populated — so notes-class files (including any repointed
wander observations) barely feed cognition even after you fix the
`notes.jsonl`/`notes-spark.jsonl` path mismatch. The stranding is two layers
deep, not one.

---

## 2. The architecture verdict, attacked from both sides

The report says: keep the doctrine, treat the tmux brain as debt with a known
payoff condition. I'll argue both directions harder than it did, then name the
evidence that actually decides.

**A. Keep the resident-brain architecture.**
- The incident history is not rhetorical. A resident session with proven-nonce
  readiness (`brain.py:368-440`) would have collapsed the 4h43m narration outage
  to one supervisor tick, and the doctrine of "one warm session, never
  cold-start under contention" is the *direct* fix for the 151-second child wait
  (each cold fallback amplified the contention that caused the failure). That is
  a load-bearing, empirically-earned property.
- The "~2,600 lines to make a terminal an API" figure is real (~2,650 across the
  three files) **but only 367 of them are tmux-specific** (`tmux_claude.py`).
  The other ~2,280 are the mailbox/validation protocol and supervisor lifecycle
  — request/reply correlation, wedge detection, context recycling, single-flight
  locking. A "real session API" deletes the 367-line driver and *some* readiness
  glue; it does **not** delete request correlation, single-flight, or lifecycle
  management, which any async out-of-process model channel needs. So most of the
  edifice is not tmux scar tissue; it is IPC that survives the substrate swap.
- The tool-use envelope is genuinely irreducible to a stateless call:
  `describe_scene` runs Claude Code's own `Read` tool on a photo path
  (`vision._within_photos`), pinned by `test_brain_envelope.py`. A plain
  text-completion API cannot do that; you would rebuild an agent loop anyway.

**B. Substantially replace it.**
- The architecture is correct *only conditional on a premise it does not
  control*: that no first-class persistent-session-with-tools API exists. The
  Anthropic Agent SDK / a managed persistent session is exactly that premise
  dissolving. The day it lands, the 367-line driver and much of the
  glyph/wedge/recycle machinery is deletable, and the remaining IPC shrinks to a
  thin client.
- The workload argument is stronger than "keep": **reflection — the documented
  heart of the loop — never touches the brain** (M5-only, no fallback), and the
  background kinds that used to justify a second session were migrated to M5 in
  Stage 2 (#242), which *deleted* `spark-io`. The brain's remaining niche is
  interactive voice turns plus a couple of visual kinds. A shrinking niche
  guarded by 2,650 lines and a systemd supervisor is a replacement candidate,
  not settled architecture.
- Live reliability is not free: `state/health.json` shows px-brain's last_error
  as "handshake failed after 2 attempts" even while status derives `ok`, and the
  history carries hundreds of lifetime handshake failures. The substrate is
  *maintained* healthy, at real supervisor cost.

**Evidence that discriminates (neither side asserted this):** instrument, for
one week, the *fraction of all Claude-bound requests that actually land on the
brain* vs. defer to M5 vs. return None-and-fall-back, plus the brain's true
availability (validated-round-trips ÷ attempts). If interactive turns are a
small minority and brain availability is already high, the resident session is
over-built for its residual load and a stateless-plus-thin-cache design wins. If
interactive turns dominate and cold-start latency is the binding constraint,
keep it. This is a measurement, not an opinion, and #270 already asks for half
of it.

---

## 3. The agency claim, attacked

The report's dichotomy — *inhibitory agency real, generative agency fake* — is
half-right and mis-framed. Inhibition **is** real and mechanical
(calendar/presence/battery/on-call/night gates; `greet_arrival` is a genuinely
closed reactive loop). But "generative agency is fake" is imprecise on two
counts. First, **generation is real and copious** — a 75-item seed pool, novel
situated goals, original prose. Second, there **is** a feedback loop
(§1, B9). What SPARK actually lacks is not generation and not *some* feedback;
it is **retention of the consequences of its own initiative**. The sharper thesis:

> SPARK has reactive and inhibitory agency and a language-mediated feedback loop
> that is (a) diffuse — it changes prompt *content*, never the action space or
> any weight — and (b) **currently broken in production** (§5.1). It cannot
> reliably accumulate what its own initiative produced.

That is a stronger indictment than "cosplay," because it names a *fixable
mechanism* rather than a category error.

**Operational definitions, scored from live evidence only:**

| Capacity | Operational test | SPARK score | Evidence |
|---|---|---|---|
| **Noticing** | A change in perception alters the next thought/action within one tick | **Strong** | Awareness→reflection transitions; `greet_arrival` closed loop; battery/health nudges (`mind.py:2610-2618`). |
| **Initiative** | Acts without an external trigger | **Real but shallow** | Timer + M5 choice over a frozen enum; 70/30 seed. Fires, but the space never grows. |
| **Remembering** | A fact from interaction N is available at interaction N+k in the channel a human uses | **Near-zero** | Voice/obi-chat inject ≤10 turns + 3 thoughts, no `retrieve_memories` (B3). Durable store is all narrative philosophy (B4). |
| **Learning** | An outcome changes a future decision | **Present in code, broken live** | `memory.py:299-307` reads outcomes into consolidation → retrieval; but consolidation is failing now (§5.1) and retrieval is keyword-overlap over a philosophy store. |
| **Continuity** | Identity/relationship state persists across restarts and is acted on | **Weak** | `intention-spark.json` persists but `active: null`; goals expire unacted; memories persist but aren't retrieved by humans. |
| **Embodied adaptation** | A physical outcome (cliff, stuck, drift) changes a future physical parameter | **Zero** | No encoder/IMU (B8); cliff calibration is manual; no proprioceptive feedback writes any threshold. |

The honest verdict is *not* "inhibitory real, generative fake." It is:
**noticing and initiative are real; remembering, learning, and embodied
adaptation are wired but non-functional or absent.** The report's binary hides
that three of these are *plumbing/liveness* problems, not design impossibilities
— which is precisely where the leverage is.

---

## 4. The smallest change that makes SPARK meaningfully remember people

The report's instinct (wire `retrieve_memories` into the voice prompt) is a trap
it half-sees: **the store is all `narrative`/0.4 consciousness philosophy**, so
the 50-line plumbing fix *alone* surfaces "recursion is not sterile" essays at a
seven-year-old. B3 and B4 are **coupled**: retrieval only helps if the store
contains retrievable *person facts*, and today it structurally cannot (the only
`report`-kind writes land in a flat file no human-facing channel queries; the
one `observation` writer is stranded).

Ranked options:

1. **Inject `retrieve_memories(user_text)` into voice + obi-chat prompts
   (the report's pick).** ~50 lines. *Necessary but insufficient alone* — it
   surfaces philosophy until the store has facts. Do it, but not first and not
   by itself.
2. **Write `report`-kind person facts from conversation turns, into a
   retrievable place.** This is the actual smallest *meaningful* change. The raw
   material already exists: `state/obi_chat.jsonl` logs every Obi message, and
   voice turns already pass through a dispatcher that could stamp a
   one-line "Obi said/likes/did X" `report` record. Pair (2)+(1) and you have a
   real memory of Obi in the channels Obi uses, for ~a page of code.
3. **Reuse `contextual_preference.py` — it already is a person/context/outcome
   store — instead of building one.** It has the exact schema and **zero
   production callers** (§5.3). The decision is "wire it up or delete it," not
   "design a person-model."
4. **Fix the two stranded perception paths** (repoint wander's `notes.jsonl`
   write to the persona file; unfreeze the root-owned `exploration.jsonl`).
   Plumbing bugs, not features — but low value until (2) exists, and the
   stranded records are partly mislabeled garbage (§5.4), so *filter on land*.
5. **Episodic / spatial / embedding memory platforms.** Do **not** build these
   before (1)+(2) are proven. They are the "memory platform" the commission
   warns against.

**Smallest high-value change: (2) then (1)**, reusing (3)'s schema. Everything
else is a platform waiting for a justification the family hasn't yet provided.

---

## 5. What Fable missed (findings absent from its top ten)

The most consequential of these — 5.0 — is a genuine privacy/correctness bug the
report walked past entirely. The rest range from live-broken pipelines to
advertised-but-dead capabilities to safety parameters sitting inside the one file
self-evolution may edit.

### 5.0 The `message_obi` privacy redaction is bypassed through session history — into the jailbroken personas *and* the public feed [PRIVACY/CORRECTNESS BUG]
The redaction that makes SPARK's private DMs to Obi safe
(`display_text = "[private message to Obi]"`, `mind.py:2892-2897`) covers **only
the thoughts-file persist path**. `expression()` must receive the *unredacted*
thought so `_emit_message_obi` can actually send it — and then writes the raw
text straight into session history: `text = thought.get("thought","")`
(`mind.py:3173`) → `history_entry = {"event":"mind", ..., "thought": text}`
(`mind.py:3560-3572`), with no `display_text` branch. From session history the
raw private DM then flows to three places the invariant swears it never reaches:
- **the voice-loop prompt** as "Recent events" (last 3 history entries,
  `voice_loop.py:523-532`) — *including when GREMLIN or VIXEN is the active
  persona*, the exact cross-context bleed the per-persona buffers exist to
  prevent, and those personas can speak it aloud;
- **the reflection prompt**, via awareness digesting `"mind"` events
  (`mind.py:2046-2061`) into `recent_conversations`, which is allowlisted into
  reflection (`_REFLECTION_AWARENESS_KEYS`, `mind.py:2492`); reflection output
  feeds `thoughts-spark.jsonl` → `/api/v1/public/thoughts`, the site and Bluesky,
  so the model can paraphrase a private DM into public content;
- **`GET /api/v1/session`** (`api.py:1783-1801`), which redacts weather coords and
  the prompt excerpt but *not* history thought text.

The documented invariant ("private DM content never reaches social posting or the
public thoughts endpoint") holds at exactly one chokepoint and leaks around it at
three. This is a real bug with a real blast radius (a child's private message
resurfacing in a jailbroken persona's mouth or on a public feed), not a same-uid
trust assumption — and it outranks B1 as a security finding by any honest
measure. **P0.**

### 5.1 The memory pipeline is broken *right now* — a live correctness bug, not B5's hypothetical
`state/consolidation_meta.json` reads `{"last_date":"2026-08-24","attempts":2,
"done":false}` and `state/memories-spark.jsonl` has produced **no new record
since 2026-08-23** — while `thoughts-spark.jsonl` is 3.6 MB and actively
growing. So consolidation is failing *despite* a full input, which is a
different and more urgent failure than B5's "an M5 outage would starve the
thoughts log." The report frames the memory-degradation risk in the subjunctive;
the live system is already in it. This should be P0.

### 5.2 Self-evolution is advertised, hard-disabled, provenance-falsified, failure-masked, and health-invisible — five defects stacked
`claude_session.py:306` ("`evolve` is deliberately NOT here and is therefore
disabled") raises `ColdStartForbidden` at `:411`. Yet the reflection prompt tells
SPARK "You can use action='evolve' to propose a change to yourself"
(`mind.py:2765`), `evolve` is in the action enum, `expression()` dispatches it
(`mind.py:3443`), and SPARK is even shown its "Evolution history"
(`mind.py:1247-1249`) as if the capability works. Five independent defects
compound (the last four missed by both the report and my first pass):
- **Provenance falsification:** `bin/tool-evolve:31` hardcodes
  `enqueue_evolve(intent, requester="adrian", source="cli")` — so an *autonomous*
  self-modification request from px-mind is logged in the evolve queue as a
  *human CLI request*. For the one subsystem whose entire safety story is
  audit-and-approve, the audit trail lies about who asked.
- **Failure masked into a branch leak:** `_run_in_worktree` catches only
  `SessionBudgetExhausted`/`TimeoutExpired` (`px-evolve:343-358`), so
  `ColdStartForbidden` escapes into a poll-level `except Exception`
  (`px-evolve:748-753`), leaves the entry `building` → reset to `pending` → retry
  — and the retry's `git worktree add -b spark/evolve-<id>` now fails because
  `_cleanup_worktree` removes the worktree but never the branch
  (`px-evolve:577-586`), terminating as a fictional `failed:worktree` while a
  stray `spark/evolve-*` branch accumulates in the live checkout per attempt.
- **Health-invisible:** px-evolve is absent from `KNOWN_COMPONENTS`
  (`health.py:44-59`) and never records success/failure — a permanently-dead
  pipeline doesn't even read as `missing`.
- **Routing residue:** `brain.py:106-108` classifies `evolve` as a routable kind
  (1800s deadline) while `claude_session.py` refuses it — a single
  `PX_BRAIN_KINDS` edit (the documented live rollout dial) silently converts the
  "deliberate outage" into requests served by a session with no worktree and no
  Write/Edit tools, ending as misleading `failed:no_changes`.

A headline CLAUDE.md capability is dead end-to-end, SPARK is prompted to keep
trying it, and the failures corrupt the very audit trail evolution's safety
depends on. Either fund the worktree or remove it from the prompt/enum and log
one honest disabled-record.

### 5.2b The sole resident session has no priority for its highest-value workload — voice can be starved up to 15 minutes
The per-session single-flight `FileLock` is held for a whole turn
(`brain.py:740-748`, `LOCK_WAIT_S=10`). Background kinds hold `spark-brain` for
their full deadlines — research/compose 300s, consolidate 600s, **self_debug
900s** (`brain.py:139-162`) — and `research`/`compose` are deliberately *not*
absence-gated (`mind.py:417-420`), so they run while the family is home. During
one, "Hey Spark" gets `VOICE_BRAIN_UNAVAILABLE` after ≤2 bounded attempts
(`voice_loop.py:698-735`) and speaks the deterministic ack "I heard you. Give me
a second." (`voice_loop.py:663`) — and **there is no deferred retry anywhere**;
the turn ends. So the resident architecture the report defends has no preemption
or priority for the one workload (interactive voice) that justifies its
existence, and can be deterministically unanswerable to the child for up to 15
minutes — worst during `self_debug`, i.e. exactly when things are already
degraded. This is a concrete cost on the §2 keep/replace ledger the report's
"keep" case never books.

### 5.2c The 3am-silence bounds and the Nest voice pointer live in the file self-evolution may edit
`NIGHT_SILENCE_START_H/END_H` (`spark_config.py:43-44`), `ANNOUNCE_ENABLED`
(`:26`) and `ANNOUNCE_RELAY_URL` (`:27`, where announce audio is POSTed) sit in
`spark_config.py` — *first* in the px-evolve whitelist
(`claude_session.py:423-430`). The codebase applies the opposite principle
elsewhere and says why: provenance ceilings live outside `spark_config.py`
"which self-evolution can propose editing," and `PX_WANDER_VISION_ENABLED` was
made an env var *specifically* so evolve cannot re-enable autonomous vision. Yet
`policy.py` (blacklisted) reads its night window from the whitelisted file via
`night_silence_bounds()`. The night-silence *rule* is constitutional but its
*parameters* are evolvable, and a one-line `NIGHT_SILENCE_END_H = 5` or a changed
relay URL is exactly the small plausible diff that survives human PR review. The
3am invariant and the "where does SPARK's voice go on the LAN" pointer are on a
weaker footing than the ceilings the project deliberately moved out of reach.

### 5.3 A complete person/outcome-learning store exists as dead code
`src/pxh/contextual_preference.py` implements a person/context/outcome
`Experience` schema — exactly the shape both the B3 and B4 fixes and B9's
outcome-conditioning need — with **zero production callers** (`make_experience`
is invoked only by its own tests). The report treats person-memory and
outcome-learning as things to *build*; the scaffolding is already written and
abandoned. This reframes the roadmap from "design" to "wire up or delete."

### 5.4 Provenance has three unwritten kinds and a lying docstring
`inference`, `verification`, **and** `model_perception` have no production
writer. The report named the first two; it missed that `model_perception` is
also unwritten *and* that the provenance module's own docstring table claims
"model_perception ← wander's scene descriptions" while wander actually stamps
`observation` (ceiling 1.0) — over-crediting Claude-vision output. Live
`notes.jsonl` shows some of that "observation" content is "the photo is
essentially all black" garbage. So B4's "16 genuine exploration descriptions sit
unreachable" is partly wrong: they are *mislabeled, partly worthless* vision
output, and landing them as-is would inject over-confident junk. SPARK never
`verify`s a belief against the world — the epistemic loop the provenance system
was built to enable was never closed.

### 5.5 A second stranded perception path: `exploration.jsonl`, root-owned and frozen
Reflection reads `exploration.jsonl` for landmark hints (`mind.py:2832-2840`,
gated on `entry.interesting`). Live, that file is `root`-owned and last modified
**2026-08-17** — frozen for over a week, unwritable by the `pi` daemons that
would append to it, and its recent entries carry no parseable observations. A
second perception→cognition path is silently dead, and the ownership makes it
fail closed and invisible. (This is the same root-created-file ownership hazard
CLAUDE.md documents for `health/` and the mailbox, recurring uncaught here.)

### 5.6 The reflection layer emits degenerate and confabulated content that then becomes durable memory and public output
Live `thoughts-spark.jsonl` contains many `[curious/wait/sal 0.0]` entries whose
thought text is a single character `t`, and substantive thoughts that
*confabulate* infra state — e.g. "my disk is filling up and my CPU is pegged"
while disk is at 69% and capped. These low-quality/false thoughts are the
**input to consolidation** (§5.1) and feed `/api/v1/public/thoughts`, the site,
and Bluesky. Garbage-in is not filtered before it becomes durable memory and
public record. This undercuts any memory fix downstream: retrieval quality is
bounded by generation quality, and generation is currently noisy.

### 5.7 Disabled subsystems are invisible to health
`px-evolve` has **no health record at all** in live `state/health.json`. A
subsystem that is advertised (§5.2) but disabled, and also unmonitored, cannot
be noticed as broken by the observability layer the report otherwise praises.
Combined with health's *consecutive-only* derivation (an intermittent failure
reads `ok` the instant one tick succeeds — live-confirmed: px-brain and
px-mind-reflection both show `ok` with failure last_errors), the health system's
blind spots are wider than the report's single "chronic sub-threshold" note.

### 5.8 Two paid-Claude cognitive outputs have zero readers — including SPARK's own self-diagnosis
`self_debug` spends a Sonnet session (2/day) writing a diagnosis to
`state/debug_reports.jsonl` (`mind.py:3506-3518`) that **nothing reads** —
repo-wide grep finds no reader (a 2026-07 QA doc already flagged it write-only and
it still is). `compose` writes full text to `state/compositions-spark.jsonl`
(`bin/tool-compose:66-91`), also unread. This is B9's "cannot learn from
outcomes" in its sharpest form: the action whose literal prompt is *"SPARK's
reflection layer is failing. Diagnose"* produces a diagnosis neither SPARK nor any
human ever reads, while billing the budget. Nothing closes the loop from
diagnosis to remedy.

### 5.9 The vision audit ledger is caller-asserted env, defaulting to the flattering category
`bin/tool-describe-scene:122-124` reads `PX_VISION_ORIGIN` (default
`"interactive"`), `PX_VISION_REASON`, `PX_VISION_TASK_ID` from the *caller's*
environment into the vision log. CLAUDE.md leans on this log to prove the novelty
budget is respected, but any process that fails to export the vars — e.g. an
env-dropping sudo chain like the documented `tool-wander` one — logs an
*autonomous* Claude-vision call as an *on-demand human* request, the unbudgeted
category. Same class as the report's `PX_NOTE_KIND` honorable mention, aimed at
the one paid-vision ledger the project uses to prove restraint.

*Corroboration:* the dedicated hunt independently confirmed §5.1 (consolidation
can halt permanently with no health component and a self-limiting 2-attempts/day
cap, `memory.py:386-397`) and cleared several suspected issues as non-findings
(px-alive writes are on tmpfs; conversation/thoughts/notes/memories are all
bounded; API token/PIN comparisons use `secrets.compare_digest`) — worth
recording so the re-rank isn't padded with already-fixed items.

---

## 6. My re-ranking

### P0 — correctness / safety (fix first)
1. **`message_obi` redaction bypass (§5.0).** A private DM leaks through session
   history into the jailbroken personas' prompts and the public thoughts feed.
   This is the genuine top security/privacy finding — the one the report's #1 (B1)
   should have been. Add a `display_text` branch at the history-write and
   awareness-digest sites, not just the thoughts persist.
2. **Consolidation is failing live (§5.1).** The memory pipeline has produced
   nothing since Aug-23; everything the report calls the "highest-leverage fix"
   is moot while the writer is down. Diagnose `done:false`, and give
   consolidation a health component so the next silent halt is visible.
3. **`confirm_motion_allowed` provenance + TTL (B6).** The one *physical-safety*
   latch still on the pre-#209 footing, live-`true` now with no record of who
   armed it. `quiet_mode.py` is a ready template.
4. **Brain reply per-request nonce (B1) — as P1 hardening, demoted from the
   report's P0.** Cheap defense-in-depth against a confused/delegated co-resident
   process; not the top security hole, because same-uid already dissolves the
   boundary and the strongest sink is re-gated. Fix §5.0 first — it is the real
   one.
5. **Move `NIGHT_SILENCE_*` and `ANNOUNCE_*` out of the evolve-whitelisted file
   (§5.2c).** A one-line diff to the 3am invariant should not be within
   self-evolution's reach when the ceilings deliberately are not.

### Complexity to delete (or decide)
- Dead reflection routing (`call_claude`, `call_brain_reflection`) + the stale
  `reflection`/`post_qa` entries in `claude_session._DEFAULT_BRAIN_KINDS` (B2).
  Low-risk — already test-pinned dead — so this is hygiene, not the trap the
  report implies.
- **Decide `evolve` (§5.2):** either fund the worktree or remove it from the
  prompt/enum. Advertising a hard-disabled capability is worse than not having
  it.
- **Decide `contextual_preference.py` (§5.3):** wire it up (it is the person/
  outcome store the roadmap wants) or delete it. Dead scaffolding that *looks*
  like a feature is the exact "dead code pretending to be redundancy" the
  commission asked about.

### 3 highest-leverage product capabilities
1. **Person-facts written from conversation, retrieved in voice/obi-chat**
   (B3+B4 coupled, §4 option 2+1, reusing §5.3's schema). The only change that
   makes SPARK remember *Obi*.
2. **Repair + repoint the perception→memory paths** (fix consolidation §5.1;
   repoint `notes.jsonl`; unfreeze `exploration.jsonl` §5.5) so the body
   produces retrievable, *filtered* knowledge.
3. **Close the outcome loop parametrically** — a per-action engagement counter
   biasing the enum. The language loop exists (B9) but is diffuse and broken;
   one numeric bias that survives restarts is the smallest thing that makes
   behavior visibly grow with the family.

### 3 experiments worth frontier attention
1. **Retriever quality A/B.** Wire `retrieve_memories(user_text)` behind a flag;
   measure whether keyword-overlap retrieval over a *fact-populated* store helps,
   or whether embeddings are required. Falsifies §4 option 1's cheapness.
2. **Brain-vs-M5-vs-fallback traffic + brain availability, one week.**
   Discriminates the §2 keep/replace question with data, not taste.
3. **Does closing the outcome loop change anything a family notices?** A/B the
   parametric bias (capability 3) against thoughts+action logs. Tests whether
   the missing loop is the actual gap or a red herring.

### Explicitly do NOT build
- SLAM, manipulation, multi-robot, autonomous navigation (agree with report).
- An embedding/episodic **memory platform** before the 50-line plumbing + person
  facts are proven (§4).
- More resident Claude sessions, or any widening of the brain tool envelope.
- Re-enabling autonomous Claude vision (CLAUDE.md invariant — leave it).
- A "fix" for the same-uid health/lease/reply gaps that is anything *other* than
  a cheap nonce, until phase-2 OS isolation (#281) actually ships — over-building
  authentication between processes that share a uid is theater.

---

## 7. Final adversarial verdict — "3 months of engineering left"

**Preserve** (these are earned, not ornamental — do not touch):
- The policy sink pinning `origin`/`effect` (`policy_context.py`) — the one
  thing standing between a persona-swap and the speaker at 3am.
- Health derive-status-at-read (`health.py:189-212`) and provenance ceilings
  outside `spark_config.py` (`provenance.py`) — both defeat a specific class of
  lie.
- Brain **proven-nonce readiness** (`brain.py:368-440`) — the single
  highest-value receipt in the system; it, not the reply channel, is where the
  brain's engineering paid off.
- The M5-only, no-fallback reflection discipline — the cold-fallback ladder it
  replaced *caused* the 151-second child wait; keep it deleted.

**Delete / decide:**
- Dead reflection routing + stale brain-kinds config (B2).
- `evolve`: stop advertising a disabled capability (§5.2).
- `contextual_preference.py`: wire up or delete (§5.3).

**Build (in priority order):**
1. Fix consolidation — the memory loop is *dead on the robot today* (§5.1).
   Nothing else in the memory story matters until the writer runs.
2. Person-facts from conversation + retrieval into voice/obi-chat (§4). This is
   the difference between an installation and a companion, and the report is
   right that it is the highest product leverage — it just can't happen on top
   of a broken writer and an all-philosophy store.
3. Motion provenance (B6) and one parametric outcome bias (B9). The first closes
   a live physical-safety gap; the second is the smallest real "it learns."

**The one-sentence disagreement with the report:** Fable ranked an
unauthenticated *same-uid reply channel* as the #1 security finding while
walking past an actual privacy bug (a child's private DM leaking into the
jailbroken personas and the public feed, §5.0) and a memory pipeline that is
**dead in production right now** — so the ranking has the real security hole and
the real product failure both outside its podium, and a cheap same-uid hardening
on top of it. Fix the leak, revive the memory loop, provenance the motion latch;
the elegant reply-channel nonce can wait its turn.

---

## Appendix: what I verified first-hand (not delegated)
- `brain.py:775-798` (`collect_reply`, no writer auth — read directly);
  `:943,:981-985` (uuid + world-readable inbox/current.json — read directly).
- `mind.py:2464-2481` (`call_llm`→`ask_m5`, no fallback); `:2846-2851`
  (`record_failure` on any error, busy included); `:2832-2840` (exploration.jsonl
  landmark path); `:2765,:3443` (evolve advertised + dispatched); `:76-80`
  (`notes_file_for_persona`).
- `state.py:163` (`confirm_motion_allowed` default False).
- Repo grep: sole `retrieve_memories` prod caller = `mind.py:2570`;
  `contextual_preference` zero prod callers; `NOTES_LIMIT`/`THOUGHTS_LIMIT`
  =10000 (bounded).
- Live state: `session.json` (`confirm_motion_allowed:true`,
  `spark_quiet_mode:true`, `quiet_state:null`); `intention-spark.json`
  (`active:null`, 3/4 goals original); `consolidation_meta.json`
  (`done:false`, 2 attempts, Aug-24); `memories-spark.jsonl` (frozen Aug-23, all
  narrative/0.4); `notes.jsonl` (19KB, Aug-17, black-photo record) vs
  `notes-spark.jsonl` (3MB, live); `exploration.jsonl` (root-owned, Aug-17);
  `health.json` (`overall:ok` while last_errors show busy/handshake-fail;
  px-evolve absent); `thoughts-spark.jsonl` (3.6MB, degenerate `sal 0.0 "t"`
  entries + disk confabulation).
- Delegated cross-checks (four `spark-investigator` passes) confirmed every
  file:line above and supplied the `_DEFAULT_BRAIN_KINDS`, `test_dead_tier_
  functions.py`, `MIN_THOUGHTS`, and provenance-writer-census details.
- A fifth read-only pass (missed-findings hunt) contributed §5.0 (the
  `message_obi` redaction-bypass chain, `mind.py:2892-2897`/`:3173`/`:3560-3572`
  → `voice_loop.py:523-532`/`mind.py:2046-2061,2492`/`api.py:1783-1801`), the
  §5.2 evolve provenance/branch-leak/health/routing detail, §5.2b (single-flight
  voice starvation), §5.2c (evolvable safety bounds), §5.8 (unread paid outputs)
  and §5.9 (caller-asserted vision ledger), each re-verified against the cited
  lines before inclusion. All investigator agents run under the read-only
  `spark-investigator` authority boundary (#281) — no agent in this review could
  touch production state, systemd, GPIO, or hardware.
