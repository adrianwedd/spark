# SPARK Architecture Red-Team — 2026-08-25

Frontier-level adversarial review of SPARK as a deployed, persistent, embodied
agent. Commissioned to answer five questions: does the architecture deserve to
exist; where is SPARK still cosplaying agency; what failures are still hard to
see; where are authority boundaries still fake; and what should SPARK become
next.

**Method.** Four independent read-only code investigations (architecture/SPOFs,
agency-vs-theatre, observability, authority boundaries) run under the
mechanically-scoped `spark-investigator` agent type, plus direct reading of live
state on the production Pi and the open GitHub issue set. Every load-bearing
claim in the top-ten findings was independently re-verified at file:line by the
synthesising author before inclusion. This audit deliberately advances beyond
`docs/audits/2026-08-14-spark-persistent-agent-audit-v1.md` rather than
repeating it; where it confirms that audit it says so.

**Evidence discipline.** `[FACT]` = verified at the cited file:line or live
state file this session. `[INFERENCE]` = reasoned from code, not observed
running. `[LIVE]` = read from a state file on the robot at audit time. No
timestamps or causal explanations are invented. Nothing was changed on the
robot; the only write this session is this document.

---

## A. Architecture verdict

**The architecture deserves to exist, but for a narrower and more honest reason
than it advertises, and it currently carries three subsystems' worth of
routing residue that read as design.**

The intentional core — *semantic intelligence proposes; deterministic machinery
constrains* — is real, load-bearing, and vindicated by incident history. The
policy sink (`src/pxh/policy.py`, `policy_context.py`), the health system's
derive-status-at-read-time discipline (`src/pxh/health.py:189-212`), the
provenance confidence ceilings clamped on write and read
(`src/pxh/provenance.py:102-112`), and the brain's proven-round-trip readiness
(`src/pxh/brain.py:368-440`) are each a direct, well-reasoned response to a
specific reproduced failure. This is not a toy that grew ornaments; it is a
system that learned from its own outages.

But two verdicts qualify that.

1. **The resident-brain edifice is a workaround for a missing API, not an
   architecture in its own right.** [INFERENCE] Roughly 2,600 lines across
   `tmux_claude.py`, `brain_daemon.py`, and most of `brain.py` exist solely to
   make a terminal multiplexer behave like a programmatic session channel:
   filesystem replies because `capture-pane` returns rendered ANSI
   (`brain.py:17`), nonce handshakes because the prompt glyph renders
   identically for a listening session and one wedged behind a permission
   dialog (`tmux_claude.py:154-166`), self-heal recycles because a
   context-exhausted session narrated its reply command as prose for 4h43m
   (`brain_daemon.py:88-103`). Every layer maps to a real incident, so none is
   *scar tissue given the substrate* — but the substrate is the questionable
   choice, and the day a first-class persistent-session-with-tools API exists,
   most of this becomes deletable. The architecture is correct conditional on a
   premise it does not control.

2. **The claimed pipeline "M5 → resident brain" is wrong for the most important
   workload.** [FACT] Reflection — which CLAUDE.md calls the heart of the
   cognitive loop — never touches the brain. `reflection()` → `call_llm()` →
   `ask_m5()` is M5-Ollama-only with no fallback (`mind.py:2846`, `2464-2481`,
   verified: `call_llm` calls `ask_m5` directly). The resident brain serves
   *interactive* turns and a few high-value background kinds; reflection and the
   brain are parallel destinations for different work, not sequential stages.
   Worse, three files disagree about where reflection runs (finding B2), and
   the only reason it works is that the disagreeing paths are dead.

**Bottom line:** keep the doctrine, keep the sink, keep provenance and health.
Treat the tmux substrate as technical debt with a known payoff condition, not
as settled architecture. Delete the reflection-routing residue now, before it
becomes load-bearing by accident.

---

## B. Ten strongest findings, ranked by consequence

### B1. The brain reply channel is unauthenticated — any `pi` process can forge an answer. [FACT]
`collect_reply()` (`brain.py:775-798`) reads `outbox/<request_id>.json` and
`json.loads`es it with **zero writer authentication** — it never invokes
`tool-brain-reply`, never checks a nonce, never checks identity (verified this
session). The mailbox directories are `1777` (`brain.py:221`, `_DIR_MODE`) and
the pending `inbox/<uuid>.json` is world-readable. So `tool-brain-reply`'s
careful uuid/pending/size validation (`bin/tool-brain-reply:61-100`) is skipped
entirely by any co-resident process that reads the request id and writes the
outbox file directly. **Consequence:** forged `describe_scene`/`research`/
`compose`/`blog` replies flow to speech, memory, notes, or the *public blog and
Bluesky*; forged `voice_turn` replies become actions (re-gated downstream by
the tool-voice sink and `confirm_motion_allowed`, which contains the blast for
speech/motion but not for stored-text or public-post kinds).
**Attribution: REPO-CONFIG.** The `1777` justification (multi-uid daemons,
`brain.py:27-39`) is now largely obsolete — Stage 2 (#242) removed `spark-io`
and replies are written only by `pi`. Confidence: high (forge path mechanical,
not observed exploited).

### B2. Reflection routing is contradicted across three files; two functions are dead code with elaborate justifying docstrings. [FACT]
`brain.py:93` classifies `reflection` as an M5 kind and `ask_brain` refuses M5
kinds into the mailbox (`brain.py:879-882`). `claude_session.py:312` lists
`reflection` in `_DEFAULT_BRAIN_KINDS` — the opposite. `mind.py` defines
`call_brain_reflection()` (`:2380`) and `call_claude()` (`:2438`) that route
reflection through the brain, but **neither is reachable**: `call_llm()` calls
`ask_m5` directly and `call_claude()` has zero callers (verified this session).
**Consequence:** the most important cognitive workload's routing is described
three different ways; if anyone wires `call_claude` back in, reflection fails
closed with a misleading `brain_unavailable`. This is migration residue that
reads as architecture. **Attribution: REPO-CONFIG.** Confidence: high.

### B3. Consolidated memory is plumbed only into the reflection nobody hears. [FACT/LIVE]
`retrieve_memories()` has exactly one production caller: reflection
(`mind.py:2570`, verified — the only non-definition reference). Every channel a
human actually uses is amnesiac: the voice loop injects 3 recent thoughts + 10
conversation turns (`voice_loop.py:536-563`); `tool-recall` speaks the tail of
the notes file with no query (`bin/tool-recall:75-94`); obi-chat builds from
chat history only and is *instructed not to manufacture memories*
(`api.py:1166`) — the prompt papers over plumbing the code never laid.
**Consequence:** the one subsystem that would make SPARK a companion rather than
an installation runs into a solitary 5-minute loop with no audience. A child
bonds with something that remembers *him*; every channel Obi uses forgets past
10 turns. Cheapest high-leverage fix in the whole system.
**Attribution: MODEL/DESIGN (product), not a bug.** Confidence: high.

### B4. SPARK's durable memory is a diary about SPARK, and its one perception-grade writer is stranded. [FACT/LIVE]
Every consolidation record is `kind=narrative`, confidence 0.4
(`memory.py:343-360`); the live tail of `state/memories-spark.jsonl` (143
records) is philosophy about consciousness, recursion, and disk space. The only
`observation`-kind durable writer — wander's `_auto_remember` — writes to bare
`notes.jsonl` (`wander.py:468`), but spark-persona reflection and recall read
`notes-spark.jsonl` (`notes_file_for_persona("spark")`, `mind.py:76-80`,
verified as two distinct live files: `notes.jsonl` 19KB, `notes-spark.jsonl`
3MB). **Consequence:** 16 genuine exploration descriptions sit unreachable; the
robot's body produces no durable knowledge the mind can retrieve. There is no
structured accumulation of `report`-kind facts about Obi at all, though
provenance already supports the kind. **Attribution: REPO-CONFIG (path
mismatch) + MODEL/DESIGN (no person-model).** Confidence: high.

### B5. Reflection depends on a single flaky LAN hop with no fallback, by explicit design. [FACT]
`M5_HOST = "http://M5.local:11434"` hardcoded (`m5.py:20`, `mind.py:302`); on
any failure `call_llm` returns `BRAIN_DEFER` and the thought is skipped
(`mind.py:2479-2481`). The "reflection is optional, failing to think costs
nothing" judgment (`call_llm` docstring) **underprices a second-order cost**:
a prolonged M5 outage empties `thoughts-spark.jsonl`, which is the *input* to
nightly memory consolidation — so an outage degrades long-term memory, not just
a skipped musing. [INFERENCE] Combined with the documented brcmfmac SDIO Wi-Fi
wedge under load (#217), an M5 that is reachable-but-slow silently mutes
cognition with no process dying. **Attribution: MODEL/DESIGN + hardware.**
Confidence: high on mechanism, medium on the consolidation-degradation chain.

### B6. `confirm_motion_allowed` is a bare bool with no provenance — motion authority on the footing quiet-mode was on before #209. [FACT/LIVE]
Read directly in every motion tool (`bin/tool-drive:33`, `tool-figure8`,
`tool-circle`, `tool-wander`); stored as a plain default in `state.py:163`.
Unlike quiet mode — which got a canonical `{source,reason,set_at,expires_at}`
record after the identical unattributable-latch bug (#209,
`src/pxh/quiet_mode.py`) — motion records no actor, no origin, no TTL.
**Consequence:** any local `pi` writer flips it; a stale `true` left by a test
or a confused caller silently arms *physical motion* with no attribution and no
self-expiry. The exact class of bug already fixed for speech, left standing for
motion. **Attribution: REPO-CONFIG.** Confidence: high.

### B7. "Successful nothing" and "broken nothing" are indistinguishable on several safety-relevant paths. [FACT]
The code solves this well in places (`dropped_active`/`dropped_idle` split by
caller intent, `mic_stream.py:363-378`; `lease_wait` heartbeat mode,
`px-env:82-117`; the brain four-state vocabulary, `brain.py:418-440`) — but
collapses it where it matters: (a) M5 `busy` (correct zero-wait defer) and
`offline`/`timeout` (genuine outage) both land in
`record_failure("px-mind-reflection")` (`mind.py:2472-2481`, `2849-2852`), so a
*contended* M5 reads as a *broken* reflection layer; (b) `yield_alive` rc=0 is
identical for "parked, nothing to yield" and "exited, GPIO free"
(`px-env:114-116` vs `:198`); (c) `aplay` rc=0 with the amp disabled is a broken
nothing reported as `status: ok`. **Consequence:** the standing "SPARK seems
dead → it's infra" triage loop (memory: 3/3 such incidents were infra) is
noisier than it needs to be. **Attribution: REPO-CONFIG.** Confidence: high on
mechanism.

### B8. No physical/external receipt on three of four actuator paths — dispatch success is the exit code of the process that asked, not evidence from the far side. [FACT]
Only the brain has a fully closed loop (nonce echo through the filesystem,
`brain_daemon.py:383-393`). Sound: `aplay` rc=0 ≠ sound played (amp gating;
only the manual `px-mic-check` loopback proves audio physically happened).
Motion: no encoder/IMU confirmation anywhere; a motor command's receipt is the
subprocess exit code. Announce: `cast_ok` is HA accepting the cast
(`tool-announce:223`), not the Nest playing it. **Consequence:** the ALSA/
PyAudio splice that degraded ~32% of every utterance was invisible because
every offline metric was clean by construction (`mic_stream.py:9-12`); the
instrument that would catch a recurrence (`px-mic-check`) exists but is
manual-only and needs the mic freed. **Attribution: REPO-CONFIG /
MODEL-DESIGN.** Confidence: high.

### B9. Initiative is a timer drawing from a static 24-action menu; perception's *positive* influence is stochastic garnish. [FACT/LIVE]
Suppression is genuinely mechanical and event-driven (calendar/presence/
battery/charging/on-call gates, `mind.py:3095-3171`; `greet_arrival` is a fully
closed arrival loop, `mind.py:3592-3607`). But *which* action fires within a
tick is a small Ollama model choosing one item from a fixed enum
(`spark_config.py:343`), seeded 70% of the time by a random topic from a
90-item pool (`spark_config.py:86-190`). No experience ever changes a
parameter, threshold, prompt, or preference outside a human-merged px-evolve PR.
Live evidence: `intention-spark.json` shows a "goal" that is a `SPARK_ANGLE`
echoed verbatim, and `active: null`. **Consequence:** this is cron with a
personality layer; the inhibitory nervous system is real, the generative self
is not. **Attribution: MODEL/DESIGN.** Confidence: high.

### B10. Delegated-agent confinement is real at the tool-dispatch layer but fake at the identity layer; and the same-uid gap is what every other forge in this list rides on. [FACT]
`spark-investigator`'s `tools:` allowlist is harness-enforced and CI-pinned
(`tools/check_investigator_agent.py`), and an adversarial canary proved 5/5
escalations mechanically absent (#281 phase 1). But the agent still runs as
`pi`, same groups, same harness; OS identity separation is *designed only*
(`docs/operations/agent-os-isolation-design.md`, working bwrap prototype but no
live user/group change). **Consequence:** every same-uid forge in B1/B6 and the
health/lease rows below is reachable by *any* `pi` process, delegated or not —
the residual of the exact incident (#281) that motivated the boundary.
**Attribution: REPO-CONFIG (deferred phase-2 work, tracked).** Confidence: high.

**Honorable mentions (verified, below the top ten):** health records are
world-writable and unsigned (`health.py:100`), so any process can fabricate an
`ok` for a dead daemon; the GPIO lease token is world-readable and the borrow is
caller-attested (`tool-describe-scene:148-154`), so a foreign process can export
`PX_GPIO_LEASE_ID` and borrow hardware; `PX_NOTE_KIND` lets the caller pick the
provenance kind (`bin/tool-remember:57-61`), so the "model never chooses the
kind" guarantee holds only because trusted dispatchers set it; px-post QA maps
ambiguous→pass by design (`bin/px-post:936-937`); provenance defines seven kinds
but two (`inference`, `verification`) have no writer — SPARK never verifies a
belief against the world.

---

## C. Complexity worth deleting

1. **The dead reflection-routing path (B2).** Delete `mind.call_claude` and
   `call_brain_reflection` (`mind.py:2380`, `:2438`) and reconcile
   `claude_session._DEFAULT_BRAIN_KINDS`'s `reflection` entry (`:312`) with
   `brain._M5_KINDS` (`:93`). Three files describing one workload three ways is
   a latent trap, not redundancy.
2. **Duplicated night-silence logic.** `mind.expression()` enforces night
   silence inline (`mind.py:3095`) *and* calls `policy.evaluate` (`:3154`),
   which also enforces it. Fold the inline calendar/obi_mode/night gates into
   `policy.evaluate` (they predate #174) so the invariant lives in one pure
   function, kept honest by one test suite instead of two.
3. **The `spark_quiet_mode` legacy bool as a *writable* surface.** It is now
   correctly derived-at-read from `quiet_state` (`state.py:220-230`), but three
   docs and readers still reference the bare key; retire the writable spelling
   entirely once the last legacy latch is migrated (still `source="unknown"` on
   the live robot — the #209 record is `null`, the bool is `true`).
4. **The migration-era dual supervisor lock (#224)** once every runnable
   checkout is bridge-aware — it *widens* contention beyond the end-state design
   (`brain_daemon.py:1008-1168`).

**Do not delete to "simplify":** the arecord reader thread, the cliff-guard
layering, the `_last_known_findmyhub` module cache, the `_ExploringRefresher`
thread, the per-component health files, or any policy enforcement point. Each is
a headstone (Section D).

---

## D. Complexity worth defending

Each of these looks like over-engineering and is not — each maps to a specific
reproduced live failure:

- **The policy sink pinning `origin`/`effect` rather than accepting them**
  (`policy_context.py:138-181`) — defeats the persona-swap-replaces-prompt hole
  (#174); a caller cannot declare its way out of the 3am mute.
- **Session-read-fails-closed / awareness-read-fails-open** asymmetry
  (`policy_context.py:15-38`) — a failed read must not grant permission to speak
  during a meltdown, but must not mute SPARK for as long as px-mind is down.
- **Brain proven-round-trip readiness** (`brain.py:368-440`) — the glyph lies;
  the nonce does not. This one distinction is the highest-leverage receipt in
  the system (it would have collapsed the 4h43m outage to one supervisor tick).
- **Per-component `1777` health dir with derive-at-read status**
  (`health.py:96-212`) — cross-uid writers, and a dead daemon cannot leave a
  lying "ok". (Its blind spot — chronic sub-threshold failure — is real and
  noted in F.)
- **Provenance ceilings outside `spark_config.py`** (`provenance.py:34-36`) —
  keeps self-evolution from voting itself more confidence.
- **`boot_id`-guarded monotonic deadlines** (`m5.py:188`, `brain_daemon.py:
  237-249`) — the Pi has no RTC; without this a pre-reboot circuit reads open
  for days.

---

## E. Top 5 experiments that could falsify these conclusions

1. **Forge a brain reply (B1).** In an isolated scratch mailbox (never the live
   `state/brain/`), as an unprivileged `pi` process, read a pending inbox id and
   drop a matching outbox file; confirm `collect_reply` returns it unchallenged.
   *Falsifies B1 if* some undocumented check rejects it. (Read-only audit did
   not execute this; the code path is verified, the exploit is inferred.)
2. **Instrument reflection base rate for one week (already the #270 ask).** Log
   `voice_turn` wall-clock and M5 defer-vs-fail separately against
   `state/brain/meter.json`. *Falsifies B5/B7 if* M5 `busy` is rare and defers
   don't drive `reflection_status: offline`.
3. **Wire `retrieve_memories(user_text)` into the voice/obi-chat prompt behind a
   flag and A/B a week of family use (B3).** *Falsifies B3 if* retrieval quality
   (keyword-overlap, `memory.py:120-140`) is too poor to help — which would
   redirect the fix from plumbing to embeddings rather than confirm it.
4. **Replay a real wander run and check whether any `landmark`/`interesting`
   record ever lands in the consumers (B4/F).** Live state shows every
   `observation` record has `vision_failed: true, landmark: ""`. *Falsifies the
   "spatial memory never fires" claim if* a post-#202 run populates them.
5. **Kill M5 for an hour and measure consolidation input the next night (B5).**
   *Falsifies the second-order-cost inference if* consolidation degrades
   gracefully on a thin thoughts log.

---

## F. Top 10 next capabilities / issues, with why-now

Ranked for the proposition "SPARK — a robot that stays around" for a child.

1. **Memory retrieval in the channels humans use (B3).** Why now: the store,
   the retriever, and the provenance types already exist and run; only the read
   path into `voice_loop.build_prompt` and the obi-chat prompt is missing. Largest
   companion-value-per-line in the system.
2. **A person-model of Obi, not a diary of SPARK (B4).** Why now: consolidation
   already sees conversation turns and action outcomes; feed `report`-kind facts
   ("Obi likes X", "Z made him laugh") into the store so retrieval surfaces the
   child, not philosophy. Without this, B1's fix surfaces consciousness essays at
   a seven-year-old.
3. **Authenticate the brain reply channel (B1).** Why now: highest blast radius,
   cheapest fix (per-request nonce in a `0600 pi` sidecar, rejected on mismatch
   in `collect_reply`), and the multi-uid rationale for `1777` is obsolete
   post-#242.
4. **Give `confirm_motion_allowed` provenance (B6).** Why now: `quiet_mode.py`
   is a ready template; motion is the one physical-safety latch still on the
   pre-#209 footing.
5. **Outcome-conditioned adaptation (B9).** Why now: outcomes are already logged
   (`mind.py:3560-3572`) and shown to consolidation (`memory.py:299-307`); a
   trivial per-action engagement counter biasing the menu would convert
   cron-with-garnish into something that visibly grows with the family — the
   actual companion claim.
6. **A self-query "why did you do that?" tool (explainability).** Why now: the
   decision trace is already excellent and provenance-honest
   (`bin/tool-recall:37-56`, thought/action/suppression logs) but SPARK can only
   read a 3-item window; expose a `tool_why` over thoughts + history +
   suppression log. The questions a child asks most, whose confabulated answers
   most corrode trust.
7. **Delete the reflection-routing residue (B2, C1).** Why now: before it
   becomes load-bearing by accident.
8. **Defer-vs-fail separation in health + a chronic-failure ratio check (B7,
   F-blind-spot).** Why now: de-noises the standing "SPARK seems dead" triage
   and closes health's documented consecutive-only blindness
   (`health.py:199-212`).
9. **Land the spatial memory that already exists (B4).** Why now: fix the
   `notes.jsonl`/`notes-spark.jsonl` mismatch and the all-`vision_failed`
   landmark store first (both are plumbing bugs, not features), then add named
   places via camera-room fusion. Embodiment is SPARK's only differentiator over
   a smart speaker.
10. **Scheduled physical audio loopback (B8).** Why now: the mic is free during
    night silence; a nightly `px-mic-check` is the single highest-leverage
    *still-missing* receipt because the silent-splice class is undetectable by
    any other means.

**Explicitly rejected robotics-bingo:** SLAM, manipulation, multi-robot,
autonomous navigation beyond cliff-avoidance. None is justified by the
family-companion proposition; SPARK's differentiator is continuity and
presence, not mobility.

---

## G. "If I inherited this robot tomorrow" plan

**Day 1 — stop trusting forgeable state.** Authenticate the brain reply channel
(B1). Confirm no safety path (battery emergency shutdown) gates on a forgeable
health `ok`. These are the two places where a confused *or* malicious co-resident
process causes real harm.

**Week 1 — make memory reach a human.** Wire `retrieve_memories` into the voice
and obi-chat prompts (B3), fix the `notes.jsonl` persona mismatch (B4), and
start writing `report`-kind facts about Obi from conversation turns. Ship behind
a flag; measure engagement.

**Week 1, parallel — delete the traps.** Remove the dead reflection-routing
functions and reconcile the three-way kind disagreement (B2). Give
`confirm_motion_allowed` the #209 provenance treatment (B6).

**Month 1 — make failure legible.** Split defer-from-fail in reflection health,
add the chronic-failure ratio check, schedule the nightly audio loopback, and
add per-turn trace-id propagation to the *autonomous* paths (it exists only on
voice today). Goal: "successful nothing" and "broken nothing" never read the
same on any actuator path.

**Month 2-3 — close the learning loop.** Outcome-conditioned action biasing
(B9) and the `tool_why` explainability tool (F6). This is where SPARK stops
being an installation.

**Do not:** re-run the full pytest suite on the Pi (CI is the gate; the Pi is
resource-constrained); refactor the tmux brain machinery for elegance (it is
load-bearing given the substrate); "simplify" any headstone in Section D.

---

## H. One-page adversarial review (for another frontier model to critique)

> SPARK is a persistent embodied agent on a Raspberry Pi: local perception
> (sonar/grayscale/Frigate/mic) feeds a three-layer mind (awareness →
> M5-Ollama reflection → expression), a resident Claude session (`spark-brain`,
> driven through tmux) serves interactive turns and high-value background kinds,
> a pure policy layer gates all audio/motion, and durable memory carries
> epistemic provenance.
>
> **Claim 1: the architecture's real justification is narrower than stated.**
> The resident brain is ~2,600 lines making a terminal behave like an API; it
> is correct only conditional on the absence of a real session API. Reflection
> — the "heart of the cognitive loop" — never uses the brain at all
> (M5-only, no fallback), and three files disagree about where it runs, saved
> only because the disagreeing paths are dead. *Critique target: is the tmux
> substrate actually replaceable, or does the tool-use envelope make it
> irreducible?*
>
> **Claim 2: the doctrine "deterministic machinery constrains" is enforced for
> speech and unenforced for the reply channel, motion authority, health, and
> the GPIO lease.** `collect_reply` reads an unauthenticated world-writable
> outbox; `confirm_motion_allowed` is a bare bool with no provenance; health
> records are unsigned and world-writable; the lease token is world-readable and
> the borrow is caller-attested. All are reachable by any `pi` process because
> OS identity separation is designed but not implemented. Every hardened
> boundary in SPARK's history converged on one shape — *move the check to a
> chokepoint that reads the fact itself and cannot be told the answer* — and
> these are the same pattern not yet applied. *Critique target: is per-request
> nonce authentication sufficient, or does same-uid make all of this theater
> until phase-2 OS isolation ships?*
>
> **Claim 3: SPARK's generative agency is cosplay; its inhibitory agency is
> real.** Suppression is mechanical and event-driven; initiation is a timer
> drawing from a static 24-action menu seeded by a random topic pool, and no
> experience ever changes a parameter outside a human-merged PR. The memory
> system that would make it a companion is built and running but plumbed only
> into a solitary reflection no human hears; every channel Obi uses forgets past
> 10 turns. *Critique target: is memory-retrieval-in-conversation truly the
> highest-leverage fix, or is the keyword-overlap retriever too weak to matter
> without embeddings first?*
>
> **Falsifiable in a week:** instrument reflection base rate (defer vs fail),
> A/B memory-in-conversation, and check whether any spatial landmark has ever
> successfully persisted. If M5 defers are rare, if retrieval quality is too low
> to help, or if landmarks do populate post-#202, the top three findings weaken.

---

## Appendix: evidence index

**Files verified this session (author, not delegated):**
- `src/pxh/brain.py:775-798` (collect_reply, no writer auth), `:221` (_DIR_MODE
  1777), `:879-882` (M5-kind refusal)
- `src/pxh/mind.py:2464-2481` (call_llm → ask_m5, BRAIN_DEFER), `:2380`/`:2438`
  (dead call_claude / call_brain_reflection, zero callers), `:76-80`
  (notes_file_for_persona), `:2570` (sole retrieve_memories caller)
- `src/pxh/wander.py:468` (_auto_remember → notes.jsonl)
- `src/pxh/state.py:163` (confirm_motion_allowed bare default)
- `bin/tool-drive:33` (motion gate read)
- Live state: `state/session.json` (`spark_quiet_mode: true`, `quiet_state:
  null` — legacy latch still unmigrated), `state/intention-spark.json`
  (`active: null`, goal = verbatim angle), `state/consolidation_meta.json`
  (`done: false`, 2 failed attempts), `state/memories-spark.jsonl` (143 records),
  `state/brain/meter.json`, `state/health.json` (px-brain 219 lifetime failures,
  0 consecutive)

**Delegated read-only investigations (spark-investigator, this session):**
architecture/SPOFs; agency-vs-theatre; observability contract; authority
boundaries. Full transcripts in the session task outputs.

**GitHub issues referenced:** #217 (SDIO Wi-Fi wedge), #219/#286 (wake memory),
#224 (dual supervisor lock), #242 (spark-io removal), #256/#258 (brain outage),
#270 (voice_turn tail latency), #278 (consolidation vs recycle race), #281
(delegated-agent authority), #283 (arecord overruns), #287 (px-alive
start-timeout in lease_wait).

**Prior audit built upon:** `docs/audits/2026-08-14-spark-persistent-agent-
audit-v1.md` (this red-team confirms its "weakly-typed continuity" finding and
extends it with the reply-channel forge, the reflection-routing residue, and the
memory-plumbing dead-end).

**Distinguishing fact from inference:** the boundary forges (B1, and the
health/lease honorable mentions) are `[FACT]` at the code level and
`[INFERENCE]` at the exploited level — the paths are verified, the exploits are
reasoned, not observed. The second-order consolidation-degradation cost (B5) is
`[INFERENCE]`. Everything else in the top ten is `[FACT]`/`[LIVE]`.
