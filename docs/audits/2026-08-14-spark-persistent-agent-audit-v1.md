# SPARK Persistent Embodied Agent Audit v1

**Audit date:** 2026-08-14

**Code root:** `master` at `2388aeab7a1bbc1fcf487500d7e072bb96dd5640`

**Issue root:** [#169](https://github.com/adrianwedd/spark/issues/169)
**Status:** Code-derived audit; no redesign from #170–#174 is implemented here.

## Method and evidence standard

This audit treats documentation and prompts as claims, then tries the cheapest way each claim could be false. A property is called an invariant only when deterministic code and a test or hard runtime gate enforce it. Prompt instructions are soft policy. Comments and design documents are hypotheses unless the executed path agrees.

Evidence was collected from the named functions and tests on the pinned commit. The baseline command was:

```sh
PATH=/Users/adrian/repos/spark/.venv/bin:$PATH \
PX_BYPASS_SUDO=1 LOG_DIR=logs_test python -m pytest
```

Result: **892 passed, 1 failed, 27 skipped**. The failure was `tests/test_memory.py::test_consolidate_success_writes_deduped_memories`: its existing-memory fixture is dated 2026-07-10 and `consolidate()` uses current wall time, so on 2026-08-14 it is outside `DEDUPE_WINDOW_DAYS = 14`. The identical candidate is therefore correctly accepted by current code but contradicts the unpinned test expectation. This is a baseline defect, not an audit change.

After the characterization changes, the full suite produced **896 passed, 27 skipped**. The verification run set `PX_CLAUDE_BIN` to a temporary executable that returns `YES` for otherwise-unmocked blog QA calls. This was necessary because `tests/test_blog.py::TestBlogSchedule::test_catchup_on_missed` mocks post generation but not `_qa_gate()` and therefore calls the installed external Claude CLI; during an unisolated verification run that external classifier returned `NO` for the fixed test body. Dedicated QA-gate tests still patched and exercised pass, rejection, timeout, and circuit-breaker paths. The temporary executable is not part of the repository.

## Executive finding: what SPARK has become

SPARK is a **persistent, event-enriched narrative control loop** attached to a robot and household data plane. It is not one model process and it is not yet an epistemically coherent knower.

Its continuity is assembled from several stores with different semantics:

- `session.json` supplies operational and conversational continuity across tools;
- `thoughts-*.jsonl`, `mood.json`, and in-process mood momentum supply a narrated inner life;
- `notes-*.jsonl`, `memories-*.jsonl`, intentions, conversation buffers, exploration records, and history supply multiple overlapping kinds of memory;
- awareness snapshots combine direct sensors, external services, cached data, and derived classifications;
- deterministic dispatch gates constrain many physical and audible actions after a model proposes them;
- self-evolution queues generated intent into a separate worktree/test/PR pipeline, but current reflection can reach that queue without a contemporaneous human command.

The intentional core is the separation of semantic proposal from deterministic execution. The accidental core is that generated prose, reported facts, inferred household state, observations, and durable autobiography are often represented as untyped strings. SPARK can explain a coherent story about itself more easily than it can explain why a stored claim should be believed.

## 1. Code-derived architecture and lifecycle

```text
robot/household/external sources
  sonar | battery | mic RMS/STT | robot/Frigate cameras | HA | calendars | Find Hub
                                  |
                                  v
src/pxh/mind.py::awareness_tick()
  caches service reads; derives transitions, obi_mode, activity and health
  writes awareness.json + frigate_presence.json
                                  |
                                  v
src/pxh/mind.py::reflection()
  adds recent session events, conversations, retrieved memories/notes,
  intention, persona prompt, time weighting, system health and random seed
                                  |
                                  v
LLM fallback chain: LAN Ollama -> Claude Haiku -> Ollama Cloud -> optional local Ollama
  returns {thought, mood, action, salience}
                                  |
              +-------------------+-------------------+
              |                                       |
              v                                       v
append_thought()                              apply_mood_momentum()
thoughts-{persona}.jsonl                     mood.json + process globals
              |
              +-- salience >= threshold --> auto_remember()
              |                              notes-{persona}.jsonl
              |
              v
mind_loop()::_should_express() -> expression()
  deterministic time/presence/calendar/call/charging gates
  then tool subprocess, in-process intention mutation, or evolve enqueue
              |
              v
session.json history {event=mind, action, thought, outcome?}
              |
              +--> later awareness/reflection/voice prompt
              |
nightly memory.maybe_consolidate()
  thoughts + action outcomes + intention + recent memories -> LLM
  -> memories-{persona}.jsonl -> retrieve_memories() -> later reflection
```

The conversational lifecycle is parallel rather than subordinate:

```text
STT/text/API input -> voice_loop.build_model_prompt()
  session highlights + last 3 events + persona conversation buffer
  + recent inner thoughts + Find Hub distances + exploration landmarks
  -> codex exec with no direct file/shell tool grant
  -> validate_action() -> deterministic parameter bounds/allowlist
  -> bin/tool-* -> physical/external effect + session update
  -> conversation-{persona}.jsonl -> later conversational prompt
```

### Every route by which generated material persists or changes later behaviour

1. Every successful reflection is appended to `thoughts-{persona}.jsonl`.
2. High-salience reflection text is copied to `notes-{persona}.jsonl` by `auto_remember()` with only a `[mind]` prefix.
3. An `action="remember"` writes generated reflection text through `bin/tool-remember`.
4. Nightly consolidation turns generated thoughts, generated intentions, and outcome strings into first-person `memories-*.jsonl` records.
5. Mood output updates process-level momentum and `mood.json`; this affects later prompt context and `px-alive`/emote behaviour.
6. Generated actions and outcomes enter `session.json.history`, which feeds later awareness and reflection.
7. Generated `set_goal`, `update_goal`, and `complete_goal` actions mutate `intention-spark.json` and later prompts.
8. `research` and `compose` append model-generated records to notes and dedicated JSONL stores.
9. `blog_essay`, public posting, thought cards, messages, and announcements create externally visible derivatives.
10. `evolve` enqueues the generated thought as a code-change intent.
11. Exploration creates generated landmark observations and a post-exploration thought, both of which feed later cognition.
12. Voice/API model output and user text enter per-persona conversation JSONL and later prompts.

## 2. Epistemic transition map

| Current origin | Current representation | Transition | Later treated as | Provenance retained? |
|---|---|---|---|---|
| Sonar hardware/tool | numeric `sonar_cm` or `null` | `awareness_tick()` derives `someone_nearby` and appearance/leave transitions | awareness fact and action gate input | Source is implicit in field name; sample quality/history absent |
| Frigate detections | label, score, camera, room, timestamps | `_fetch_frigate_presence()` derives person presence and room lists | presence evidence, prompt context, greeting confirmation | Partial sensor provenance survives in awareness snapshot |
| HA person entity | name/state/home booleans | `compute_obi_mode()` derives absent/calm/active | deterministic expression suppression and prompt claim | Entity/source identity is mostly flattened |
| Find Hub tracker | coordinates, timestamps, accuracy | `_enrich_tracker()` derives distance, home/place labels and arrival transitions | voice-only location context plus awareness state | Raw coordinates and accuracy persist in `findmyhub.json`; reflection excludes exact location but awareness persists it |
| Microphone audio | STT text and RMS level | wake listener emits transcript history and `ambient_sound.json` | human report/command and ambient classification | Speaker identity, confidence, and fact-vs-command type are not durable epistemic fields |
| Human speech: “Y” | user text/history/conversation buffer | prompt instruction may call `tool_remember` | durable note | “person X reported Y” is retained only if prompt convention inserts text such as `[fact from Obi]`; schema does not enforce reporter or truth status |
| Model reflection | `{thought,mood,action,salience}` | appended on every reflection | inner narrative and later prompt context | Model/backend may be in logs, not the thought record; no evidence refs/confidence |
| High-salience reflection | thought string | `auto_remember()` | long-term note | Only `[mind]` distinguishes generated narrative |
| Consolidation input | thoughts, outcomes, intention | consolidation LLM writes first-person past tense | autobiographical durable memory | Only `source="consolidation"`; input references and claim types are discarded |
| Research/compose output | generated text | appended to notes and special stores | long-term memory/context | Record type may be present in specialized entry, but `load_notes()` selects only `note`; verification is not represented |
| Externally verified fact | no general verification primitive | can be text in note/memory | indistinguishable claim | No durable verification metadata |

### Concrete epistemic failure cases

**EF-1 — speculation becomes durable self-memory.** A reflection such as “I think Obi avoids me because he is upset” with salience above `SALIENCE_THRESHOLD` is appended by `auto_remember()` as `{"note":"[mind] I think ..."}`. Nightly consolidation is explicitly asked to extract “realizations,” “decisions,” and “events,” then writes a first-person past-tense record with `source="consolidation"`. Nothing requires observational evidence or preserves the source thought IDs. This confirms the hypothesis in #170.

**EF-2 — report and truth collapse.** The persona prompt asks the model to store `[fact from Obi] Octopuses have three hearts`. The prefix preserves reporter information only by convention, but downstream retrieval presents the note as “Your long-term memories.” There is no executable distinction between “Obi reported Y” and “Y is externally verified.”

**EF-3 — derived state looks observationally authoritative.** `compute_obi_mode()` can return `absent`, `active`, `calm`, or `possibly-overloaded` from combinations of camera, sonar, ambient, HA, calendar, and time. The result is persisted as a scalar `obi_mode`; consumers need code knowledge to reconstruct which evidence and precedence produced it.

**Invariant required before redesign:** every durable claim must retain enough information to distinguish observation, report, inference, generated narrative, and verification. #170 owns schema design and migration; this audit does not prescribe the final fields.

## 3. Memory retrieval characterization

`retrieve_memories()` tokenizes the query, scores token overlap plus tag hits, adds recency only after a positive topical score, sorts positive matches, and then fills unused slots with records sorted by timestamp.

- Zero memories in the store returns `[]`.
- A non-empty store with zero topical matches returns up to `n` recent memories.
- When fewer than `n` memories match, recent non-matches fill remaining slots.
- `importance` is parsed, clamped, stored, and returned, but is not used by `score_memory()`, dedupe, consolidation selection, or prompt construction.
- Consolidation metadata that survives retrieval is `ts`, `date`, `text`, `tags`, `importance`, and coarse `source`; reflection prompt construction retains **text only**.
- Because reflection query bits often include generic time-period words and random topic seeds, a recency-padded unrelated memory can alter subsequent model output without any topical relationship.

Characterization probes added by this audit pin zero-match recency fill and the fact that equal-text ordering follows insertion order rather than importance. #171 owns changing the policy.

## 4. Persistent and semi-persistent state inventory

| Store | Schema / authority | Readers and writers | Integrity, retention, corruption | Continuity role |
|---|---|---|---|---|
| `state/session.json` | Explicit default keys plus arbitrary additive fields; operational authority | `state.py`, voice loop, API, mind, most tools, alive/wander/status | `FileLock` + temp/fsync/replace for standard writers; history capped at 100; parse corruption backed up (3) then resets to defaults; semantic types mostly unchecked | Primary cross-process “same SPARK now” contract |
| `state/conversation-{persona}.jsonl` | `{user,spark}`, implicit | voice loop reads/writes | Whole-file atomic rewrite; last 10 turns; malformed lines skipped | Short conversational continuity |
| `state/thoughts-{persona}.jsonl` | reflection dict, implicit | mind writes; mind, voice, memory, API, posting/introspection read | append under lock; trim 10,000; malformed lines skipped | Inner narrative, public persona and consolidation source |
| `state/mood.json` | `{ts,mood,valence,arousal}` | mind writes; alive/status read | atomic snapshot; overwritten forever | Cross-process affect/pose continuity; momentum itself is process-local and resets |
| `state/awareness.json` | large implicit snapshot | mind writes; mind, API, status, voice/expression read | atomic overwrite; stale fields can remain in in-process caches; parse failure degrades | Current world-model snapshot, not historical truth |
| `state/frigate_presence.json` | derived camera snapshot | mind writes; alive reads | atomic overwrite when Frigate responds; no built-in expiry in file | Human-presence confirmation |
| `state/findmyhub.json` | external writer schema with raw tracker data | external M5 cron writes; mind and voice read | freshness threshold 15 min; raw file retention/rotation not managed here | Location/arrival continuity |
| `state/ambient_sound.json` | `{ts,rms,level}` | wake listener writes; mind reads | temp+rename; usable for 60 s; file persists beyond semantic freshness | Ambient regulation signal |
| `state/battery.json` | sensor snapshot + charging | poller writes; mind/alive/status read | temp+rename; staleness checked by some consumers, not a single schema | Safety and embodied continuity |
| `state/notes-{persona}.jsonl` | heterogeneous records; durable note recognized by `note` key | remember, mind auto-memory, research/compose, recall/reflection | locks vary by writer; 10,000 trim only on some paths; malformed skipped | Legacy long-term memory |
| `state/memories-{persona}.jsonl` | `{ts,date,text,tags,importance,source}` | memory consolidation writes; reflection retrieves | lock on append; trim 5,000; malformed skipped | Consolidated autobiography |
| `state/intention-{persona}.json` | active goal + last 10 history | intention module | lock + atomic replace; corruption silently becomes empty on load | Multi-day self-directed continuity |
| `state/exploration.jsonl` | observations/actions, implicit | wander writes; mind/voice read | append paths; no general retention bound found | Spatial familiarity / later prompts |
| `state/exploration_meta.json`, `exploring.json` | operational snapshots | wander, mind, describe/announce/alive | atomic or direct per writer; resets/degrades on errors | Autonomous-roaming coordination |
| `state/introspection.json` | generated self-analysis snapshot | introspect writes; mind/evolve reads | atomic writer; freshness used in mind but queue accepts `{}` | Self-description and evolve context |
| `state/evolve_queue.jsonl`, `evolve_log.jsonl` | explicit workflow entries | shared enqueue helper + worker | file locks and atomic queue rewrite; malformed lines skipped; no rotation found | Development-project continuity and audit trail |
| `state/claude_sessions.jsonl`, `token_usage.json` | model-use accounting | Claude session/token modules, API/introspection | locked writers; cumulative/append; public aggregate exposure | Resource self-awareness |
| `state/obi_chat.jsonl`, `obi_chat_meta.json`, `obi_evolve_pending.json` | authenticated messaging/evolve proposal state | API and mind message path | bounded chat log; atomic metadata; proposal expiry in API logic | Relationship/project continuity |
| `state/feed.json`, `blog.json`, `post_queue.jsonl`, posting cursor/status | publication derivatives | post/blog daemons and public API/site | per-daemon queues/cursors; public retention extends beyond cognition | Public narrative identity |
| `state/thought-images/*.png`, `photos/*.jpg` | generated/captured media | post/photograph/describe tools; API/site | thought images deleted after 30 days by mind; photo retention has no audit-level bound found | Visual/public continuity and privacy risk |
| logs under `logs/` | heterogeneous JSONL/text evidence | nearly all processes | rotation inconsistent by subsystem; some structured, some excerpts | Operational auditability, not cognitive authority |

### `session.json`: mechanically strong, semantically weak

Mechanical integrity is comparatively strong: standard updates hold one lock across read-modify-write and use atomic replace. Corruption is preserved in bounded backups before reset.

Semantic integrity is weaker:

- `update_session(fields=...)` accepts arbitrary keys and values without schema validation;
- many independent processes attach meaning to fields such as `listening`, `persona`, `spark_quiet_mode`, motion flags, and heterogeneous `history` entries;
- `load_session_readonly()` turns missing/corrupt state into defaults without distinguishing those conditions to its caller;
- reset protects JSON validity but can erase continuity and safety toggles together;
- `schema_version="1.0"` is not used to validate or migrate field meaning.

Separation is justified only where failures differ. Safety-authoritative motion flags, ephemeral listening coordination, and autobiographical history currently share a corruption/reset domain; that is a demonstrated reason to consider narrower authority boundaries. Splitting other fields merely for neatness is not justified.

### Auxiliary and subsystem-local stores

These stores do not all contribute to autobiography, but they are part of the persistent embodied system and can affect action or disclosure:

| Store | Purpose / authority | Integrity and lifetime |
|---|---|---|
| `state/routines.json` | routine definitions consumed by `tool-routine`; configuration authority | Long-lived JSON; tool reads and session fields hold current step |
| `state/timers.jsonl`, `state/timers/*.pid` | timer audit rows and live timer processes | Append log plus process-scoped PID; completion speaks later even after initiating request ends |
| `state/race_calibration.json`, `race_track.json` | physical calibration and learned track profile | Atomic saves in race module; long-lived physical-policy input |
| `state/race_live.json`, `race_log.jsonl` | current and historical race telemetry | Snapshot plus append history; API/public race consumers |
| `state/compositions-spark.jsonl` | generated creative work | Append-only, no bound found in audited path |
| `state/debug_reports.jsonl` | generated self-diagnosis | Locked append; no rotation found |
| `state/consolidation_meta.json` | daily attempt/done gate | Atomic snapshot; parse failure resets effective gate state for the date |
| `state/blog_log.jsonl`, `blog_failures.json` | publication workflow evidence/backoff | Subsystem-managed; influences later publication attempts |
| `state/pin_lockout.json` (`pin_attempts.json` legacy) | authentication lockout authority | Persistent atomic state; security continuity rather than persona continuity |
| `state/sonar_live.json` | latest proximity sample written by alive | Overwritten snapshot; public/API/motion consumers must reason about freshness |
| `state/*.pid` and `logs/*.pid` | daemon/timer coordination | Process-lifetime hints; stale PID handling varies by subsystem |
| `*.lock`, `*.rotlock` | cross-process exclusion | Mechanical artifacts, generally indefinite but ownership is advisory through `filelock` |
| `photos/*.jpg`, line-follow/scan debug frames | captured environmental media | Long-lived unless externally cleaned; no common retention controller |
| `site/data/feed.json` and remote social posts | copied/published derivatives | Outside cognitive state authority; may outlive deletion/correction at source |

Semi-persistent process memory also matters. `mind.py` keeps service caches, HA offline backoff, Find Hub last-known state, mood momentum, last spoken text, morning-fact date, reflection failure count, and cooldown clocks in globals. `voice_loop.py` keeps tool debounce/watchdog state. `api.py` keeps rate-limit buckets, PIN session tokens, job processes, and its public history ring in memory. These reset on restart, so apparent personality, availability, rate limits, and cadence can change even when disk state is unchanged.

## 5. Personality: prose style versus executable behaviour

### Executable policy already present

- Night silence: `expression()` calls `_is_night_silence()` and permits only `NIGHT_ALLOWED_ACTIONS`.
- Expression cadence: `_should_express()` enforces a global cooldown with a separately cooled arrival-greeting bypass.
- Presence/calendar/call/charging suppression: deterministic gates in `expression()`.
- Day/night action weighting: `_daytime_action_hint()` is prompt-only weighting, while night silence is code.
- Mood-to-emote/sound: deterministic `MOOD_TO_EMOTE` and `MOOD_TO_SOUND` mappings.
- Exploration: `_can_explore()` plus a second pre-dispatch check; downstream wander gates re-check session/battery/listening.
- Quiet mode tool state: `tool_quiet` mutates `spark_quiet_mode`; ordinary voice action selection is still primarily prompt-governed.
- Conversation/persona separation: persona-specific prompt, voice environment, thought/note/conversation filenames.

### Load-bearing prompt-only commitments

- stop speaking immediately on detected meltdown;
- connection before direction, one instruction, two bounded choices;
- declarative/non-moralizing language;
- first-session greeting wording;
- always remember facts reported by Obi;
- never volunteer tracker location;
- use `tool_quiet` before anything else when overwhelmed;
- limits on initiative described as “rarely,” “sparingly,” or “only when it fits.”

If `docs/prompts/spark-voice-system.md` were paraphrased, these could silently disappear without a failing executable test. #174 should select only behavioural commitments with observable consequences; linguistic style appropriately remains prompt-owned.

## 6. Perception, privacy, retention, and disclosure

| Source | Derived/persisted data | Retention | Consumers | Disclosure surfaces / failure-first finding |
|---|---|---|---|---|
| Robot camera | JPEG photos; vision description; exploration landmarks | photos unbounded in audited code; thought cards 30 days | describe, exploration, dashboard | `/photos/{filename}` has no auth dependency; possession/guessing of filename is enough |
| Frigate cameras/events | objects, scores, camera-to-room mapping, person-present, rooms, timestamps | awareness/frigate snapshots overwritten; external Frigate retention out of repo | mind, alive, API, prompts | public awareness returns reduced Frigate data; authenticated awareness returns full snapshot; derived occupancy influences public `obi_mode` |
| Microphone/STT | raw in-memory audio, transcript, ambient RMS/level | transcripts enter history and conversation; ambient file persists but is semantically valid 60 s | voice model, mind, logs | public thoughts may be influenced by conversation; private DM redaction is explicit, general household speech provenance is not |
| Sonar | distance and proximity transitions | current snapshots plus sampled public history/session outcomes | mind, alive, movement gates, public API | `/public/sonar` and derived activity are unauthenticated |
| HA people/entities | names, home/away/zone, calendar, sleep hours/quality, meds/water, camera/mic call state, lights/media | awareness snapshot plus derived thoughts/history/memories | expression gates and reflection | stripped from `/public/awareness`, present in authenticated awareness/session-derived cognition; derived `obi_mode` may still disclose occupancy pattern |
| Google calendars | event summary, description excerpt, timing, derived modes | cached in process and awareness snapshot | reflection and routine gates | authenticated full awareness; generated thoughts/posts can carry event-derived material unless prompt/model avoids it |
| Find Hub | raw lat/lon, tracker timestamp, accuracy; distance, semantic place, arrival | externally written raw snapshot; no repo rotation | mind awareness, arrival greeting, voice prompt | exact locations excluded from reflection by code construction but raw data persists; voice prompt includes distance when fresh; authenticated awareness may expose raw enriched data |
| Weather/system stats | environmental and machine health | awareness/current history/public samples | prompt, safety, public API | intentionally public, but can combine with occupancy timing |
| Research/blog/feed/social | model derivatives of cognitive context | notes/blog/feed/platform retention | public API/site/social platforms | durable external disclosure can outlive corrected local state |

The system can infer more than “sensor status”: probable overload from proximity plus loudness, sleep-related gentleness, school/mum/absent status, arrivals, household call activity, and room occupancy. These inferences can affect actions and generated narrative even when raw fields are redacted from a public endpoint.

## 7. Autonomous actions and gates

| Autonomous path | Trigger/model role | Deterministic constraints | Side effect / evidence / kill |
|---|---|---|---|
| Reflection expression | transition or idle timer; model selects action | action enum, global cooldown, night/presence/calendar/call/charging gates | speech, servo, sound, camera, tools; mind/session logs; stop mind service or `bin/px-stop` for motion |
| Arrival greeting | Find Hub transition + model action | transition must be present for cooldown bypass; anti-flap cooldown; normal expression gates | audible greeting; logs/history |
| Battery warning/shutdown | deterministic sensor thresholds, no model | glitch filtering; two critical readings; charging suppresses shutdown | alarm/speech then `sudo shutdown`; dry-run; stop daemon before threshold action |
| Exploration | model selects only when injected | `_can_explore()` checks roaming, not listening, battery freshness/level, not charging, mode; expression re-check; wander re-check | physical roam, photos, landmarks; `px-stop`, disable roaming, listening/charging gates |
| Research/compose/blog | model-selected silent cognitive actions | Claude budget and tool-specific constraints; expression cadence/night allowlist | external model calls, notes/blog/public content; logs and state files |
| Message/announce | model selects | DM backoff/caps and redaction; announce enable, cooldown/target controls; night suppression in tool | dashboard message or household speaker; logs/session |
| Goal mutation | model selects | one active goal, bounded text/history, stale expiry | persistent intention changes later prompts |
| Self-debug | persistent reflection failures can influence model selection | read-only Claude tools; budget; report write | diagnostic model call/report |
| Memory consolidation | nightly window and persona | max attempts/day, minimum thoughts, Claude budget | durable autobiographical memory |
| Evolution request | model can select `evolve` | queue 1/24h after PR + one pending/building per requester | code-generation worker may create branch/PR; queue/log evidence; disable `px-evolve` worker or remove action |
| `px-alive` proximity behaviour | sensor/event loop | camera confirmation, proximity/cooldown/config gates | servo/greeting behaviours; service stop and emergency halt |
| Public/authenticated API jobs | external request, sometimes model | auth/rate limit/tool validation/device confirmations by endpoint | tools/services/device control; API logs and job state |

The design principle “semantic intelligence proposes; deterministic machinery constrains” is strongest for motion ranges, exploration, night silence, charging, and API tool validation. It is weakest where “rarely,” “do not volunteer,” “only when overwhelmed,” or “use evolve freely” live only in prompts.

## 8. Self-evolution trust boundary

```text
generated reflection OR authenticated Obi proposal OR local CLI
                         |
                         v
enqueue_evolve() -> locked evolve_queue.jsonl
                         |
                         v
px-evolve daemon -> isolated branch/worktree
  planner: Read/Glob/Grep
  implementer: Read/Write/Edit/Glob/Grep (no Bash)
  test-writer: test files, but timeout is fail-open
  QA: no tools, verdict required, but timeout is fail-open
                         |
 hard post-generation gates:
  changed-file whitelist + blacklist
  existing bin/tool-* modification rejection
  max 3 files
  full tests excluding live hardware; timeout/failure closed
                         |
                         v
commit -> push -> PR -> mandatory human merge/review
```

Current guarantees:

- **Worktree isolation:** hard operational separation; failure closes the run.
- **Whitelist/blacklist:** prompt guidance plus post-commit hard validation through `file_in_whitelist()`.
- **Maximum changed files:** hard post-generation gate, default 3.
- **Role separation:** separate model calls; planner and implementer failure close, test-writer and QA timeout do not.
- **Tests:** full non-live suite must pass; failure/timeout closes.
- **Rate limiting:** enqueue blocks after a recent `pr_created` and one active request per requester.
- **Requester/source recording:** schema exists, but `bin/tool-evolve` hard-codes Adrian/CLI even when called by autonomous reflection.
- **PR creation:** required for a successful terminal status.
- **Human review:** architectural/operational merge gate, not programmatically proven inside this repository; worker has no merge step.

### Boundary violations

**EV-1 — ordinary inner narrative can initiate evolution.** `_SPARK_REFLECTION_SUFFIX` includes `evolve`; `mind.expression()` directly invokes `bin/tool-evolve`. No immediate human command or confirm gate is required. This does not bypass code validation or human merge, but it violates the candidate invariant that SPARK’s desire and the code-changing mechanism have a human-authorized handoff.

**EV-2 — source attribution is false on the autonomous path.** `bin/tool-evolve` always calls `enqueue_evolve(... requester="adrian", source="cli")`. An autonomous reflection is therefore recorded and presented in the PR as Adrian’s CLI request.

**EV-3 — QA separation is not mandatory.** A QA rejection closes the run, but a QA timeout logs “proceeding without QA.” Test-author timeout similarly continues. The full deterministic pytest gate remains mandatory.

No conversational model is granted new tools by this audit. The earlier #36 proposal to arm the live bridge is explicitly superseded in the repository’s approved #162 design; current conversational Claude calls use `--allowedTools ""`/no-session persistence.

## 9. Development / ontogeny

Today, two instances with identical code/config but different histories can differ because retrieved notes/memories, conversation buffers, intention, session history, exploration landmarks, and mood context enter prompts. However, much of the resulting variation is not distinguishable from random topic selection, model sampling, wall-clock context, or accumulated factual recall.

### Falsifiable longitudinal criterion

> Given identical code, config, clock, sensor snapshot, model version, sampling controls, and a fixed reflection seed, instance A has at least three independently recorded, provenance-preserving interactions in which Obi prefers quiet science facts after school; instance B has at least three in which Obi prefers active movement. When offered the same bounded after-school choice, A should select the quiet option and B the active option at a predeclared rate above baseline, and each choice must cite the relationship evidence that caused the difference. Neither instance may generalize the preference to unrelated people or contexts.

This is development rather than recall because history changes a bounded decision policy; it is explainable, reversible by contradictory evidence, scoped to a relationship/context, and testable under controlled randomness.

Potential mechanisms are justified only by desired behaviour:

- learned preferences: justified for repeated bounded choices;
- relationship models: justified when the same behaviour should differ by person;
- revised beliefs/confidence: justified when later evidence should correct earlier inference;
- familiarity: justified when interaction style should gradually need less explanation;
- memory decay: justified to prevent obsolete preferences dominating;
- confidence calibration: justified to make weak history produce weaker adaptation.

Prompt edits, config tuning, model upgrades, random variation, and mere ability to quote an old fact do not satisfy this criterion. #172 owns measurement design.

## 10. Failure-first probe matrix

| Subsystem | Cheap falsification probe | Current result |
|---|---|---|
| Epistemic memory | Feed speculative high-salience thought; inspect notes and consolidation output | Can persist as `[mind]` note and later first-person consolidation without evidence chain |
| Retrieval | Query a non-empty store with disjoint tokens | Returns recent records; characterization test added |
| Importance | Store equal topical records with importance 1.0 and 0.0 | Later inserted record wins tie; importance unused; characterization test added |
| Consolidation dedupe | Run fixture after 14-day window without pinned `now` | Identical text is accepted; current master baseline test fails |
| Session semantics | Call `update_session({"listening":"yes"})` or add unknown field | JSON remains valid; truthiness/unknown field propagates because no schema validation |
| Corruption continuity | Corrupt `session.json`, call `load_session()` | Backup then whole-state default reset; mechanically recoverable, semantically discontinuous |
| Prompt personality | Remove/paraphrase quiet-mode prose while leaving tools unchanged | Key meltdown selection behaviour has no prompt-independent invariant test |
| Privacy | Capture a named photo, request `/photos/<name>` without token | Route has no auth dependency; file is served if name resolves under photos root |
| Derived privacy | Strip HA from public awareness but inspect `obi_mode` | Derived occupancy/activity classification remains public |
| Agency | Return reflection action `evolve`, execute expression | Enqueues without immediate human command; later hard gates still apply |
| Evolution attribution | Inspect queued row from autonomous `evolve` | Recorded as Adrian via CLI |
| Evolution QA | Force QA call timeout | Worker proceeds to hard file/test gates and PR path |
| Motion | Ask model for out-of-range drive/explore | voice validation clamps/range-checks; exploration and tool path re-check safety state |
| Ontogeny | Replay two histories under uncontrolled RNG | Apparent difference is not attributable to lived history; controlled criterion required |

## 11. Existing invariants versus accidental behaviour

### Existing hard invariants

- Model-selected voice tools are restricted to an allowlist and parameter bounds.
- The model cannot control `PX_DRY` through action parameters.
- Standard session updates are locked and atomic.
- Exploration has redundant deterministic gates.
- Night silence and several presence/calendar/call/charging suppressions are executable.
- Private `message_obi` text is replaced before thought/note/public persistence.
- Evolution cannot succeed without worktree creation, permitted file set, max-file limit, passing non-live tests, push, and PR creation.
- Evolution worker does not merge its own PR.
- Persona-specific thought/note/conversation names reduce cross-persona contamination.

### Accidental or soft behaviour

- A memory is “true enough” because it was salient or consolidated.
- Recent memory is equivalent to relevant memory when result slots remain.
- `importance` is meaningful; currently it is inert metadata.
- `session.json` field types and meanings are stable across processes.
- Mood continuity survives restart; the durable label survives but momentum globals reset.
- First-session greeting, quiet/meltdown selection, initiative restraint, and fact-report handling persist through prompt paraphrase.
- A generated evolution request is attributed to its real origin.
- Test-author and QA participation are mandatory; timeouts prove otherwise.
- Exact location is private merely because reflection prompt construction omits it; raw/enriched data still persists and enters voice/authenticated surfaces.
- SPARK “develops” merely because its outputs change over time.

## 12. Prioritized remediation sequence

1. **Freeze evidence and origin at persistence boundaries (#170).** Before adding adaptation, distinguish observation, report, inference, narrative, and verification; migrate legacy records as unknown, not trusted.
2. **Stop irrelevant retrieval from manufacturing context (#171).** Preserve current characterization tests, then make recency an explicit mode and decide whether importance has a real contract.
3. **Correct the evolution authorization/audit boundary.** Require explicit origin/source at enqueue, prevent autonomous reflection from impersonating Adrian, decide whether generated desire may enqueue or only propose, and make QA timeout policy explicit. Preserve all existing hard gates.
4. **Make demonstrated shared-state contracts testable (#169).** Validate safety-critical/session coordination field types, define corruption semantics, and separate stores only where mixed reset authority creates a proven failure.
5. **Publish the perception/retention/disclosure contract (#173).** Address unauthenticated photo serving and derived occupancy disclosure; define external Frigate/Find Hub/social retention dependencies.
6. **Extract only load-bearing behavioural policy (#174).** Start with quiet/no-speech, initiative/cadence, presence/night gates, and persona differences whose consequences must survive wording edits.
7. **Define controlled longitudinal measures (#172).** Do not add adaptation until history-caused differences can be separated from randomness and factual recall.
8. **Repair audit reliability.** Pin consolidation test time and retain the master baseline result so future audit suites start clean.

## 13. Candidate GitHub issues

Do not duplicate #169–#174. The existing issue list through #174 was checked on 2026-08-14.

1. **`security(evolve): preserve request origin and require an explicit authorization handoff`**

   Evidence: `mind.expression()` invokes `tool-evolve` from a generated action; `bin/tool-evolve` hard-codes `requester="adrian", source="cli"`. Acceptance should preserve generated desires as proposals, record the actual origin, and require the chosen human authorization boundary without weakening worktree/test/PR review.

2. **`safety(evolve): make QA timeout fail closed or explicitly remove QA as a claimed gate`**

   Evidence: `bin/px-evolve::_run_in_worktree()` proceeds after QA timeout. Acceptance should either fail closed or document/test that QA is advisory; deterministic pytest and whitelist gates remain mandatory.

The time-dependent consolidation test is a small maintenance fix suitable for #169 or a direct audit commit, not necessarily a separate issue. Photo/derived-occupancy findings belong under #173; epistemic and retrieval findings belong under #170/#171; prompt-policy findings belong under #174.

## Final answers

- **What does SPARK perceive?** Robot proximity/camera/battery/system signals, microphone-derived transcript and sound level, multi-camera Frigate detections, HA people/calendar/sleep/routine/context entities, Google calendar data, Find Hub location, weather, and prior system state—plus derived modes and transitions.
- **What does SPARK think?** Model-generated, persona-prompted reflections shaped by awareness, recent events, random seeds, memories, intentions, and mood momentum.
- **What does SPARK know?** Operational state is comparatively well grounded; autobiographical and relational “knowledge” is a mixture of reports, generated narrative, inference, and observations stored without a common epistemic type.
- **Why does it believe it?** Often the implementation cannot answer beyond “this string appeared in thoughts/notes/consolidation.” Sensor fields sometimes imply origin; durable claims rarely preserve an evidence chain.
- **What can it remember?** Conversation turns, session history, raw thoughts, notes, consolidated memories, intentions, exploration landmarks, projects, public writing, and selected media—with different retention and corruption rules.
- **What can it initiate?** Speech, pose/sound/camera activity, gated roaming, research/composition/blogging, messages/announcements, goal changes, consolidation, diagnostics, and an evolution request that may lead to a reviewed PR.
- **What constrains it?** Tool allowlists, validation, cooldowns, time/presence/calendar/call/charging gates, battery and roaming checks, locks/atomic writes, budgets/rate limits, evolution file/test/PR gates, operator service controls, and human merge review. Some important constraints remain prompt-only.
- **What survives time?** Many JSON/JSONL stores and public/external derivatives survive restarts; some apparently persistent qualities, especially mood momentum and cooldown clocks, reset with the process.
- **What changes because SPARK has lived?** Today, stored history can change later prompts and therefore behaviour, but the change is weakly typed and confounded by randomness. A controlled, provenance-backed, bounded history-dependent decision is the minimum falsifiable evidence of genuine development.
