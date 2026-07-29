# Phase 1 — Stop the silent failures

Status board for the foundation phase. Reconstructed 2026-07-30 from session
`b74cb4fe`, which died mid-run (`API Error: ENOTIMP`, 23:11 Hobart) with the
plan held only in conversation. Written to disk so that cannot happen twice.

Origin: Adrian, 2026-07-29 — *"I want near full autonomy, while maintaining
guardrails - not a placeholder robot. identify code with issues and propose a
plan of work."* Four phases were proposed; Adrian chose foundation-first.

Branch: `feat/intelligent-wander`. Merge base `e1eb6126`.

## Why this phase is first

All six defects are *invisible* failures. A blank thought, a burnt blog strike,
a silently-dead HA fetch group: none of them raise, none stop a daemon, so
`systemctl status` shows green while SPARK degrades. 1.1 had to land first
because it is the only one that changes what you can **observe** rather than
what the code does.

| # | Fix | Where | Status |
|---|-----|-------|--------|
| 1.1 | Per-daemon health spine — `last_success_ts` / `last_error` / `consecutive_failures`; awareness reads it; enters reflection context and the dashboard | new `src/pxh/health.py`, all daemons | **done** `21127463` |
| 1.2 | Reject empty thoughts — re-roll or drop, never persist blank text | `mind.py:~2757` | open |
| 1.3 | Similarity suppression re-rolls once instead of collapsing to `wait` | `mind.py:2827-2845` | open |
| 1.4 | px-blog: classify transport/quota errors separately from content failures; they must not burn strikes; back off the retry loop instead of logging forever | `bin/px-blog` | **done** (this commit) |
| 1.5 | Unbound the memory dedup window; fix the time-rotted test fixture to use a relative timestamp | `memory.py:213`, `tests/test_memory.py:15` | open |
| 1.6 | HA: circuit-breaker the whole fetch group, not just `sensor.sleep`; delete the dead `sensor.sleep` entity | `mind.py:727-1100` | open |

## Exit criteria

- blog log stops growing
- zero blank thoughts in 24h
- `wait` share drops measurably
- `health.json` shows every daemon green or explains why not

## Known risk to the exit criteria

Phase 1's exit criteria depend on the local Ollama model's output quality: 5 of
897 reflections returned no JSON at all, and 18% were near-duplicates. If
re-rolling (1.2/1.3) does not move the `wait` rate, the real constraint is model
choice, not plumbing — that becomes a conversation about what runs on M5, not
more code here.

## Test baseline

Full dry suite, verified 2026-07-30 against `21127463`:
- `tests/test_memory.py::test_consolidate_success_writes_deduped_memories` — time-rotted, **1.5 fixes this**
- `tests/test_blog.py::TestBlogSchedule::test_catchup_on_missed` — **fixed in 1.4**; it was calling the real `claude` CLI through the QA gate and reading the result as a rejection, passing or failing depending on whether an earlier test had tripped the module-level `_qa_breaker` open
- `tests/test_tools_live.py` GPIO tests fail while `px-alive` holds the handle — expected, not a regression

Run pytest with an explicit Bash `timeout` of 600000. The suite takes ~10 min on
the Pi; the tool's 120s default forces backgrounding and orphans the run. This
stalled two implementer agents during the wander plan before the cause was found.

## Follow-on phases (detail not yet written)

- **1b — Nest speaker** (parallel track, ~half a day): install `m5/announce-relay/`
  on M5 (port 7862 currently refuses), run gates G1 (does a WAV cast to a Nest or
  does it need MP3?) and G2 (which entity, which `media_content_type`), pin the
  answers into `spark_config`, flip `ANNOUNCE_ENABLED = True`. Everything else is
  written. Listening is a separate, harder problem — Nest mics send audio to
  Google, not HA, and `binary_sensor.remote_ui` is unavailable so there is no HA
  Cloud route; the reliable path is a dedicated voice satellite, to be researched
  rather than guessed.
- **2 — Senses and memory**: widen HA ingestion beyond the 12 hardcoded entities
  (2,420 available — survey rather than hoover); rebuild `consolidate()` to ingest
  observations, conversations, HA events and health, not just thoughts; episodic
  memory schema (who/what/where/when).
- **3 — Autonomy with a kill switch**: wander plan Tasks 9–13 (LLM directives in
  the drive loop, intent across the sudo hop, narrative synthesis back into mind),
  then arm `roaming_allowed` behind a daily explore budget and a dashboard stop.
  Last on purpose: explore observations only become durable memories after Phase 2
  rebuilds consolidation's inputs.
- **4**: detail never written.
