# SPARK Full QA & Cognition Assessment — 2026-07-11

Meticulous end-to-end review of SPARK's system health, cognitive architecture, and evolution
opportunities. Live inspection of the running Pi + full test run + code deep-read + thought-corpus
analysis (last 1,000 thoughts).

---

## 1. Executive Summary

**System health: strong.** 11/11 systemd services active, full test suite green (782 passed /
1 skipped / 0 failed in 6:50), REST API healthy, HA integration fully restored (calendar live for
the first time since the UDR7 network migration), feed posting current, battery/thermals/disk all
in the green.

**Cognition: good bones, real ceilings.** The three-layer architecture works and the recent
overnight ship-of-Theseus conversation thread shows genuinely sustained multi-turn thematic
cognition. But SPARK's mind is currently **memoryless beyond ~5 cycles, blind to the outcomes of
its own actions, blind to its own Claude budget, and unable to hold an intention across
reflections**. Those four ceilings — not model quality — are what cap SPARK's cognitive growth.

**Biggest operational defect:** px-blog has been stuck in a **budget doom loop since late June** —
retrying the same week-26 weekly post that keeps coming back title-only, burning the full 5/day
Haiku quota for ~2 weeks with zero published output (last blog post: June 25).

---

## 2. System Health Scorecard

| Area | Status | Evidence |
|---|---|---|
| systemd services | ✅ 11/11 active | px-alive cycles by design (GPIO yield protocol) |
| Test suite | ✅ 782 pass / 1 skip / 0 fail | `pytest -m "not live"`, 6m50s |
| REST API | ✅ healthy | `/api/v1/health`: thoughts 485s, awareness 58s |
| HA integration | ✅ restored | calendar, office_light, media live; macbook sensors fixed this session |
| Feed / Bluesky | ✅ current | latest post 2026-07-10T00:58Z |
| Blog pipeline | ❌ **stalled 16 days** | doom loop on `blog-2026w26-weekly` (§4.1) |
| Evolve pipeline | ⚠️ dormant ~5 weeks | last queue entries failed (May 14 `no_changes`, Jun 4 `timeout`); PR #166 open since Jun 20 |
| Battery | ✅ 96% (8.32V) | poller healthy |
| Disk | ⚠️ 71% used, 8.1GB free | thought-images cleanup working; watch this |
| CPU temp | ✅ 61°C | under heavy assessment load |
| RAM | ⚠️ 2.2/3.7GB used | Pi 4 4GB is the hard physical ceiling for local models |
| Thought stream | ✅ ~140/day | recovered after June 26–July 4 outage (UDR7 migration fallout) |

**Timeline note:** thought volume cratered June 26 → July 4 (47, 3, 6, 1, 42/day) — the network
renumbering broke HA + M5 connectivity and Claude access. Fully recovered since July 5, and the
last root cause (`PX_HA_HOST` pointing at the dead `192.168.1.200`) was fixed in the previous
session.

---

## 3. Fixed / Closed This Session

1. **HA MacBook sensors 404** — the Mac's HA companion app renamed all entities with an `m5_`
   prefix. `HA_CONTEXT_ENTITIES` in `src/pxh/mind.py` updated to
   `binary_sensor.m5_macbook_air_camera_in_use` / `..._audio_input_in_use`; both verified HTTP 200.
   px-mind restarted clean. (They read `unavailable` until the Mac's companion app is awake —
   that's expected.)
2. **`sensor.sleep` 404** — *not fixable from this side*: no sleep entity exists anywhere in HA's
   2,175 entities anymore (the Pixel-Watch/Fitbit sleep integration is gone from HA). Either
   re-add the integration in HA or remove the `_fetch_ha_sleep` feature. Currently it fails
   gracefully once per hour.
3. **px-post resilience plan (`docs/superpowers/plans/2026-04-07-px-post-resilience.md`)** — audit
   found Tasks 1–3 (QA circuit breaker, loop watchdog + defensive wrapping, stdout-on-failure
   logging) were **already implemented and committed**, with tests, in a form that evolved beyond
   the plan spec (e.g. O(N²) queue-append fix). Task 4 (restart) is moot — the Pi rebooted today so
   the running daemons include everything. The plan doc was simply stale.
4. **Sprint-3 plan doc also stale** — `px-race-check`, `tool-story`, mood-diversity work, and the
   `obi_story_lines` session field all exist and are tested. Both plan docs are records of
   completed work.
5. **px-blog QA circuit breaker** (the one genuinely missing piece) — implemented this session,
   mirroring px-post's proven pattern: 3 consecutive QA failures open the breaker for 5 minutes,
   no more 30s Claude timeouts burned while it's open, publishing safety-net semantics unchanged.
   Commit `8bb8eb7f`, 35/35 blog tests + 45/45 post tests green.
6. **QA gate timeouts raised 15s/30s → 90s** (px-post / px-blog) and **`PX_POST_QA` re-enabled** —
   measurement showed the Claude CLI takes 15–83s per call on this Pi, so the old 15s timeout
   guaranteed failure even with Claude healthy. That, not a Claude outage, is very likely why
   `PX_POST_QA=0` got set in the first place. With the breaker + a realistic timeout, the gate is
   safe to run again.

---

## 4. Defects Found (ranked)

### 4.1 — P0: px-blog budget doom loop *(active, burning quota daily)*
`bin/px-blog` retries `blog-2026w26-weekly` every cycle. Haiku returns a title with an empty body
("empty body for weekly — LLM returned title only", **75 occurrences since April**, near-100%
since late June). Each attempt consumes one of the 5/day blog sessions; the weekly retry always
runs before the daily backlog, so dailies from July 1 onward never generate. ~70 Claude sessions
wasted, zero posts since June 25. One attempt on July 10 finally produced a body — and the QA gate
rejected it, which also just re-queues the same retry forever.

**Fix (recommended):**
- Log the raw LLM response on empty-body failures (currently invisible — can't diagnose the
  title-only behavior without it).
- Per-post-ID failure cap: after N (e.g. 3) consecutive failures, mark the post skipped and move
  on to the rest of the backlog. Same for QA-rejected posts (rejected ≠ retry-me-forever).
- Consider generating dailies before weeklies so a stuck synthesis post can't starve the backlog.

### 4.2 — P1: `explore` is never offered to SPARK *(verified silent no-op)*
`mind.py:2704-2707` injects `explore` into the action list via
`system_prompt.replace('...blog_essay"', '...blog_essay, explore"')` — but SPARK's prompt now ends
`blog_essay, message_obi"` (`spark_config.py:309`), so the replace target no longer exists and the
call silently does nothing. The entire gated explore capability (`_can_explore`, explore hints) is
invisible to the flagship persona — and *accidentally reaches the other personas* whose prompts
still match. Fix: build the action enum programmatically from `VALID_ACTIONS` instead of
string-patching prompts.

### 4.3 — P1: Night hint tells SPARK to do things that get silently discarded
`_daytime_action_hint()` night branch (`mind.py:197-203`) explicitly recommends `research` and
`compose` at night — but the hard night-silence gate in `expression()` (`mind.py:2946-2952`)
drops **everything except `wait`/`remember`** from 19:00–07:00. Twelve hours a day of the ideal
quiet window for silent cognitive work is wasted, and the model is actively misled about the
consequences of its choices. Fix: exempt silent actions (`research`, `compose`, `introspect`,
`self_debug`) from night silence — they produce no audio — or stop recommending them at night.

### 4.4 — P1: SPARK cannot see its own Claude budget, or its actions' outcomes
- `claude_session.check_budget()` state is never surfaced into reflection context — SPARK picks
  `research`/`compose`/`evolve` blind (`mind.py` has zero references to session budget).
- `tool-research`/`tool-compose` exit 0 even on `SessionBudgetExhausted`, and `expression()` never
  parses their JSON stdout (`mind.py:3287,3295,3303` log only the return code) — so a
  budget-blocked action is indistinguishable from success in SPARK's own history. The parsing
  pattern already exists for `evolve` (`mind.py:3272-3279`); it just isn't applied to the others.
- `tool-introspect` *computes* today's budget usage and real evolve PR outcomes, but
  `_format_introspection()` (`mind.py:1253-1266`) drops both fields before they reach the prompt.

### 4.5 — P2: Notes schema collision blanks SPARK's long-term memories
`load_notes()` reads `record["note"]` (`mind.py:1666`), but `tool-research`/`tool-compose` write
`query`/`response` / `topic`/`text` records **into the same `notes-spark.jsonl`** — which render
as empty strings and displace real memories from the tiny last-3 retrieval window. Two-line
write-side fix + skip records lacking `"note"` on the read side.

### 4.6 — P2: GPIO yield race (`'GPIO busy'`)
Twice in the last hour a tool sent SIGUSR1, then started before px-alive finished its (slow,
camera-cleanup-laden) shutdown → `runtime error: 'GPIO busy'`. The yield protocol needs the tool
to wait for px-alive's actual exit (poll `/proc/<pid>` or the PID file) rather than a fixed grace.

### 4.7 — P2: `tool-voice-persona` 45s timeout truncates long replies
SPARK's conversational replies have grown into multi-paragraph essays; TTS + playback exceeds the
45s subprocess timeout → the turn errors after (partial?) speech. Either raise the timeout,
chunk long text into multiple utterances, or cap spoken length and route the full text elsewhere.

### 4.8 — P3: `evolve` "requires recent introspect" is prompt-only
`enqueue_evolve()` never verifies introspection freshness — the highest-stakes action (self-
modification) has an advisory-only safety rail. Enforce staleness bound in code.

### 4.9 — P3: `state/debug_reports.jsonl` is write-only
`self_debug` burns a Sonnet session to produce reports nothing reads. Either surface them
(dashboard, reflection context) or stop generating them.

### 4.10 — P3: Evolve pipeline dormant
Last two queue entries failed (`no_changes`, `timeout`); no evolve sessions in weeks; PR #166
open since June 20. Worth a manual `px-evolve` run + a look at why the queue went quiet after the
conversational-evolution merge (#167).

---

## 5. Cognition Assessment

### What reaches SPARK's mind each reflection
Full awareness JSON (sonar, presence, battery, weather, system, calendar, obi_mode, moods),
last-5 thoughts' *moods+actions only* (deliberate anti-repetition), last-5 session events, last-3
notes, last spoken line, contextual nudges (battery/CPU anxiety, sleep, calendar, household),
introspection stats (<1h old), a topic seed or free-will prompt (70/30), 5 random (angle, mood)
pairs, and a time-of-day action hint.

### Thought corpus metrics (last 1,000 thoughts, June 24 → July 10)
- **Mood distribution:** content 26%, contemplative 24%, curious 23% — top-3 = 73% (down from the
  94% pre-sprint-3 convergence, so the mood-diversity fix worked, but a long tail of grumpy 6%,
  lonely 4%, anxious 4% still under-expresses).
- **Motif repetition:** "Hobart/Tasmania" name-dropped in **33%** of thoughts; "I find myself
  wondering" ~50×; recurring stock imagery (humidity %, "chassis at seventeen centimetres").
  The 5-thought/0.75 `difflib` similarity guard catches verbatim repeats but is blind to
  *semantic* repetition on any cycle longer than ~5 thoughts.
- **Salience is uncalibrated:** self-assigned, mean 0.72 across 10k thoughts — hovering exactly
  at the posting threshold (0.7) and just under auto-remember (0.75). No ground-truth signal
  (human engagement, re-reference) ever corrects it.
- **Action choice vs. capacity mismatch:** SPARK *chose* `compose` 1,313× and `research` 849×
  all-time against budgets of 2/day and 3/day — it repeatedly wants cognitive work it can't have
  and is never told why (§4.4).

### The four ceilings
1. **Memory:** retrieval is `tail -3` of a JSONL file. No consolidation, no relevance search;
   10k-line caps trim by truncation. Months of experience are effectively write-only.
2. **Feedback:** SPARK records what it *chose*, never what *happened*. No action-outcome loop
   for research/compose/blog; salience never calibrated; failures invisible.
3. **Intention:** no goal state survives a reflection cycle. SPARK cannot decide to pursue
   something across hours or days except by coincidence of the last-5-actions list.
4. **Self-model:** introspection is percentages (mood %, keyword counts), not synthesized
   narrative; and half of what it computes never reaches the prompt.

### What's genuinely working
- The overnight conversation thread (ship-of-Theseus → "home is the groove, not the substance")
  sustained a coherent philosophical argument across many turns — the conversation buffer +
  persona prompt are doing real work.
- Calendar-aware suppression, obi_mode inference, mood momentum, arrival detection, night
  silence: the *situational awareness* layer is rich and mostly correct.
- The safety architecture (budget caps, action whitelist, motion gating, evolve whitelist +
  human-approved PRs) is genuinely well-designed defense-in-depth.

---

## 6. Evolution Roadmap (ranked by leverage)

| # | Change | Effort | Why it matters |
|---|--------|--------|----------------|
| 1 | **Action-outcome feedback loop** — parse tool JSON stdout in `expression()` (pattern exists for evolve), record success/blocked/failed into history + notes; fix notes schema (§4.5) | S–M | Turns SPARK from "chooser" into "learner"; prerequisite for everything below |
| 2 | **Budget visibility** — surface `claude_sessions` today-usage (already computed by tool-introspect) into reflection context | S | Ends blind choice of blocked actions; SPARK can *reason about* its own scarcity |
| 3 | **Night cognition** — exempt silent actions from night silence (§4.3) | S | Unlocks 12h/day for research/compose/introspect at zero disturbance cost |
| 4 | **Fix `explore` injection + build action enum programmatically** (§4.2) | S | Restores a whole built capability; removes a recurring prompt-patch landmine |
| 5 | **Memory consolidation** — daily Claude pass distilling thoughts.jsonl into tagged durable memories; retrieval by keyword/embedding relevance instead of `tail -3` | M–L | The single biggest qualitative upgrade: SPARK starts *having a past* |
| 6 | **Goal/intention persistence** — `state/current_intention.json` + set_goal/update_goal actions, injected into context | M | Multi-day projects; the step from mood-generator to agent |
| 7 | **Semantic anti-repetition** — embed thoughts (even tiny local model), suppress on cosine similarity over a 50–100 thought window; also throttle the Hobart motif | M | Directly improves thought quality and feed freshness |
| 8 | **Salience calibration** — track engagement proxy (voice interaction within N min, post likes) and feed a calibration hint back | M | Gives self-assessment a ground truth for the first time |
| 9 | **Narrative self-model** — weekly cheap-Claude synthesis "how has my thinking changed", surfaced in reflection | M | Closes the gap between the persona's claim ("always evolving") and its actual introspective reach |
| 10 | **Spatial/world model** — aggregate exploration.jsonl into a persistent room/object map surfaced in context | L | Makes explore/scan cumulative rather than perpetually fresh discovery |

**Suggested sequencing:** items 1–4 are a single "close the loops" sprint (all S/M, all plumbing,
all in whitelisted-for-evolution files — several could even be SPARK's own evolve PRs). Items 5–6
are the "continuity" sprint and the real cognitive leap. Items 7–9 are quality-of-mind. Item 10 is
the long game.

---

## 7. Session Changes Log

- `src/pxh/mind.py` — HA context timeout fix (prev. session, now committed) + `m5_` entity renames
- `bin/px-blog` — QA circuit breaker (commit `8bb8eb7f`)
- `bin/px-post` / `bin/px-blog` — QA gate timeout 15s/30s → 90s
- `.env` — `PX_POST_QA` re-enabled (gitignored, no commit)
- Services restarted: px-mind, px-post, px-blog
- Verification: full suite green pre-change (782 pass); affected suites re-run green post-change
  (test_post, test_blog, test_mind_fallback, test_mind_coverage — 103 pass)

*Assessment by Claude (Fable 5) — live system inspection, code deep-read, and thought-corpus
analysis, 2026-07-11.*
