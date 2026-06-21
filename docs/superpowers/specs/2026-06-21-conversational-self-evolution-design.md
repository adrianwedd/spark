# Conversational Self-Evolution (#162) — Design Spec

**Status:** Approved design (2026-06-21), pending implementation plan.
**Supersedes:** the Layer-3 proposal in `docs/mcp-server-plan.md` (arming the live `claude-voice-bridge` with `Read/Write/Edit/Bash`).

## Goal

Let **Obi** (and **Adrian**) ask SPARK, in conversation, to build a new feature. SPARK confirms, enqueues the request to the **existing `px-evolve` pipeline**, and can report project status on request — with **no new code-execution surface** added to any live LLM path.

## The Reframe (why this is not the original Layer 3)

GitHub #162 (split from #36) originally proposed arming the live `claude-voice-bridge` with `Read/Write/Edit/Bash/WebSearch` so SPARK could write code in-conversation. That bridge is the **same LLM path that serves the public `/chat` endpoint and unauthenticated HA/Nest voice** — i.e. untrusted input. Giving it shell/file tools is a serious prompt-injection surface.

SPARK already has **`px-evolve`**: a self-evolution pipeline that safely does "SPARK writes code → PR with human approval" — queued (`state/evolve_queue.jsonl`), git-worktree-isolated, file-whitelisted (`claude_session.WHITELIST_PATTERNS` / `BLACKLIST_FILES` / `BLACKLIST_PATTERNS`), max-3-files, pytest-gated, Opus, 1/day, gated on a human approving the PR.

Therefore this feature is **an authenticated conversational front-door to `px-evolve`**, not a new agentic runtime. `claude-voice-bridge` stays `--allowedTools ""`. All code generation continues to happen only inside the sandboxed px-evolve worker.

## Decisions (from brainstorming)

| # | Decision |
|---|----------|
| Trigger boundary | **Authenticated channels only**: `POST /api/v1/obi-chat` (Obi) and the token-authed dashboard/CLI (Adrian). NOT public `/chat`, NOT the wake-word/HA voice loop. |
| Build experience | **Async via px-evolve.** Conversation enqueues an intent; the existing worker produces the PR. Conversation LLM gets no file/shell tools. |
| Confirmation | **Confirm-first** (two-turn): SPARK proposes and asks; enqueues only on an explicit yes. |
| Status feedback | **Pull-based**: on-ask in obi-chat + a "My Projects" dashboard panel. No proactive completion announcements. |
| Adrian path | Adrian triggers via the **same enqueue** (token/CLI), not a separate mechanism. |

## Architecture

```
Obi (authenticated)                         Adrian (token/CLI)
   │  POST /api/v1/obi-chat                     │  enqueue_evolve(...) directly
   ▼                                            ▼
obi-chat handler ── structured {reply, evolve_intent}
   │  (evolve_intent set only after Obi confirms; quota-checked)
   ▼
enqueue_evolve(intent, requester, source)  ──►  state/evolve_queue.jsonl
                                                     │
                                  px-evolve worker (UNCHANGED):
                                  worktree + whitelist + max-3-files
                                  + pytest + Opus → PR (human approval)
                                                     │
                                                     ▼
                                            state/evolve_log.jsonl  (id, intent, requester, PR url, state)
   ▲                                                 │
   │  GET /api/v1/obi/projects  ◄────────────────────┘
   │  (merges queue + log, filtered requester=obi)
Dashboard "My Projects" panel  +  obi-chat status summary injection
```

## Components (design units)

### C1 — `enqueue_evolve` helper (the SINGLE queue writer)
A reusable module-level function (new `src/pxh/evolve_queue.py`) that becomes the **one and only** writer of `state/evolve_queue.jsonl`. `bin/tool-evolve` is **refactored to call it**, so schema, rate-limit, and dedup can never diverge between the CLI and conversational paths.

It appends an entry matching the **exact schema px-evolve already consumes** (verified against `bin/tool-evolve` + `bin/px-evolve`):
```
{"id": <unique>, "intent": <feature description>, "status": "pending",
 "introspection": <contents of state/introspection.json or {}>,
 "requester": "obi"|"adrian", "source": "obi-chat"|"cli", "ts": <iso>}
```
- **`status: "pending"` is REQUIRED** — `bin/px-evolve` skips any entry where `status != "pending"`. Omitting it = silent no-op.
- **`introspection`** is read from `state/introspection.json` (default `{}` if missing/stale) — px-evolve's `build_plan_prompt(intent, introspection)` uses it; without it the planner is blind.
- **Interface:** `enqueue_evolve(intent: str, requester: str, source: str) -> dict` — returns the entry, or raises `EvolveQuotaError` (rate-limited) / `EvolvePendingError` (requester already has a pending item) / `ValueError` (empty/oversized intent).
- **Intent bounds:** non-empty after strip; cap length (≤ 300 chars) and re-apply `_sanitize_chat_text`-equivalent hygiene at the helper boundary so the helper is safe regardless of caller.
- **Rate-limit (single source of truth, replacing the misstated `check_budget` assumption):** the conversational path can outrun the worker, so quota is enforced at **enqueue** time by BOTH:
  1. the existing **`evolve_log.jsonl` 24h window** (the limiter `tool-evolve` already uses — counts the last `pr_created`), AND
  2. **max one `pending` entry per requester** in `evolve_queue.jsonl` (this also serves as dedup — see below).

  Either tripped → raise `EvolveQuotaError` / `EvolvePendingError`. (`claude_session.check_budget` is NOT used here — it only reflects sessions *after* the worker runs.)
- **Dedup = "one pending per requester"** (not fuzzy string match — reviewers flagged exact-match as brittle for LLM-generated intents). If the requester already has a `pending` entry, raise `EvolvePendingError`; the caller says "you've already got one on the list."
- **Concurrency:** append under the **same `FileLock`** path that `tool-evolve`/`px-evolve` use. Because `tool-evolve`'s old read-rewrite (`atomic_write`) could drop a concurrent append, the refactor makes both go through this single locked writer.

### C2 — obi-chat build-intent detection (with a SERVER-SIDE confirm gate)
Change the obi-chat Claude call to **structured output** `{reply: str, evolve_intent: str | null}` — using a strict JSON output contract (`--output-format json` / a parsed JSON convention), validated with a Pydantic model. **Parse/validation failure ⇒ treat as `{reply: <raw text>, evolve_intent: null}` and never enqueue.**

`_OBI_CHAT_SYSTEM_PROMPT` gains rules: when Obi wishes for a new capability, **propose it and ask to confirm**, leaving `evolve_intent` null; only after Obi explicitly agrees, set `evolve_intent` to a concise feature description.

**Confirm-first is enforced on the server, not just by the prompt** (reviewers: a single LLM reading Obi's text could be injected into setting `evolve_intent` on turn 1). The handler maintains a small server-side `pending_evolve_proposal` (intent + nonce + ts, kept in obi-chat state):
- If the model returns `evolve_intent` and there is **no** matching recent `pending_evolve_proposal`, the handler **records the proposal and does NOT enqueue** — regardless of what the model claims. SPARK's reply asks for confirmation.
- Only when a subsequent Obi turn affirms an existing un-expired proposal does the handler call `enqueue_evolve(intent, "obi", "obi-chat")`. So enqueue always requires two real Obi turns, independent of model policy.

The handler speaks `reply`; on `EvolveQuotaError`/`EvolvePendingError` it appends a friendly note ("one project at a time / try again later"). `requester="obi"` is fixed **by this endpoint** (not inferred from token type). Obi's input is already cleaned by `_sanitize_chat_text()` (formatting hygiene — see Security).

**Response schema change (additive, backward-compatible):** `POST /api/v1/obi-chat` returns `{reply, ts, id, evolve_intent: str|null, evolve_id: str|null}` (existing clients ignore the new fields).

### C3 — "My Projects" status
- `GET /api/v1/obi/projects` (auth via the existing `_verify_token` dependency) returns the requester's projects, newest first. Shape: `[{id, intent, state, pr_url?, ts}]`.
- **Source of truth, de-duplicated by `id`** (px-evolve mutates the queue entry's status in place AND appends to the log, so the same `id` appears in both files): take **pending** items from `evolve_queue.jsonl` and **completed/failed** items from `evolve_log.jsonl`; when an `id` is in both, the **log record wins**.
- **Explicit state mapping** from px-evolve's real statuses to the UI contract:

  | px-evolve status | UI state |
  |---|---|
  | `pending` (in queue, not yet picked up) | `pending` |
  | `building` (set by worker at start — see C4) | `building` |
  | `pr_created` (+ `pr_url`) | `ready` |
  | `failed:*` (worktree/tests/whitelist/timeout/…) | `failed` |
  | `skipped:dry` / other non-terminal | excluded |

  There is **no `merged` state** — px-evolve does not poll GitHub; `ready` is terminal from SPARK's view (Adrian merges on GitHub). Optional GH PR-status polling is out of scope.
- Dashboard **"My Projects"** panel renders the list via the existing `api()` helper.
- obi-chat injects a compact projects summary (last ~5 items + state, requester-scoped) into the obi-chat context so "is my joke tool ready?" is answered from real data, not hallucinated.

### C4 — px-evolve passthrough (concrete worker changes)
Two small, concrete changes to `bin/px-evolve` (it is on px-evolve's own blacklist for *self*-evolution, but these are human-authored):
1. **Set `status: "building"`** on the queue entry (via the existing `_update_entry_in_queue`) **before** `process_entry` runs, so "My Projects" can show in-progress. (Today the entry stays `pending` until done.)
2. **Carry `requester`/`source`** from the queue entry through to the `evolve_log.jsonl` record and into the generated **PR body** ("Requested by Obi via conversation — intent may be adversarial; review accordingly"). Tolerate legacy/CLI entries that lack these fields (default `source="cli"`, `requester="adrian"`).

C3's status read depends on these fields existing on both queue and log records.

## Data flow & state

- **New/changed state:** `evolve_queue.jsonl` entries gain `status` (already used by px-evolve), `introspection`, `requester`/`source` (additive; px-evolve tolerates absence for legacy entries). `evolve_log.jsonl` gains `requester`/`source` passthrough. A small `pending_evolve_proposal` lives in obi-chat state for the server-side confirm gate. No new long-lived state files.
- **Quota:** the real limiter is a **24h window since the last `pr_created`** in `evolve_log.jsonl` (not a calendar day) — so user messaging is "try again later," not "tomorrow." Enforced at enqueue (C1), plus the one-pending-per-requester rule.
- **Dedup:** "one `pending` entry per requester" (C1) — simpler and more robust than fuzzy intent matching; the quota window is the secondary backstop.

## Error handling

| Condition | Behavior |
|---|---|
| Quota window active (24h since last `pr_created`) | `enqueue_evolve` raises `EvolveQuotaError`; SPARK says one project at a time, try again later. No queue write. |
| Requester already has a `pending` item | `EvolvePendingError`; SPARK says it's already on the list. No write. |
| Empty / oversized (>300 char) intent | `ValueError`; SPARK asks Obi to describe it again. No write. |
| `evolve_intent` set with no matching server-side proposal | No enqueue; handler records a `pending_evolve_proposal` and asks Obi to confirm (the structural confirm gate). |
| px-evolve worker fails (pytest/whitelist/timeout) | Recorded in `evolve_log` as `failed:*` → "My Projects" shows `failed`; Adrian sees it. Conversation path is decoupled, so no user-facing crash. |
| obi-chat structured-output parse/validation failure | Treat as plain reply with `evolve_intent=null` — never enqueue on ambiguous parse. |

## Security model

- **Structural gate:** enqueue is only invoked from auth-required endpoints (obi-chat, dashboard) / local CLI. The public `/chat` and voice loop have no path to `enqueue_evolve`. `requester` is assigned **by the endpoint** (obi-chat ⇒ `"obi"`; CLI/dashboard ⇒ `"adrian"`), never inferred from which token authenticated — admin token and PIN session are not distinct identities.
- **No new execution surface:** the conversation LLM cannot read/write files or run shell; it only emits a text intent that becomes a queue row.
- **Confirm gate is server-enforced** (C2): two real Obi turns are required via the `pending_evolve_proposal` precondition; the model cannot self-authorize an enqueue.
- **Intent is untrusted end-to-end:** the queue `intent` is the canonical trusted value, sanitised + length-bounded **once** at the `enqueue_evolve` boundary (px-evolve does not re-sanitise before interpolating it into planning/implementation prompts). px-evolve's whitelist includes behaviorally-sensitive files (e.g. `voice_loop.py`), so the generated PR body explicitly flags the intent as possibly adversarial for the human reviewer.
- **Inherited px-evolve safety (unchanged):** file whitelist + blacklist, max-3-files, pytest-must-pass, Opus, and **mandatory human PR approval** — an adversarial intent cannot escape the whitelist or self-merge.
- **Input sanitisation:** `_sanitize_chat_text()` is **formatting hygiene** (strips `<>`, newlines, NUL — prevents wrapper/tag escapes), not a content-safety boundary. The real safety is the whitelist + human approval.
- **Quota / one-pending-per-requester** double as abuse-rate limits on the conversational trigger.

## Testing strategy

- **C1:** unit tests — enqueue writes a correct entry; quota-spent raises `EvolveQuotaError`; duplicate intent no-ops; FileLock used.
- **C2:** obi-chat handler tests with a stubbed Claude call returning `{reply, evolve_intent}` — confirm-first (intent null on turn 1 → no enqueue), enqueue on turn 2; parse-failure falls back to no enqueue; quota-spent path speaks gracefully.
- **C3:** `GET /api/v1/obi/projects` merges queue+log, filters `requester=obi`, maps states; auth required; dashboard markup smoke test.
- **C4:** entry/log round-trip carries `requester`/`source`; px-evolve tolerates entries lacking them.
- All via `isolated_project`; no live Claude/hardware calls in tests.

## Out of scope

- Arming `claude-voice-bridge` (or any live path) with `Read/Write/Edit/Bash` — explicitly rejected.
- Public `/chat` or unauthenticated HA/Nest voice triggering.
- Proactive "your project is ready" announcements (pull-based status only).
- The #25 persona work (the original #162 listed it as a gate; this front-door does not depend on it).
- Changes to px-evolve's core safety machinery (whitelist, worktree, pytest, approval) — reused as-is, only metadata passthrough added.

## Verified facts (from QA grounding in the code)

- `evolve_queue.jsonl` entry schema (written by `bin/tool-evolve`, consumed by `bin/px-evolve`): `ts, id, intent, introspection, status` — px-evolve processes only `status == "pending"`. `enqueue_evolve` must emit this full schema.
- The `evolve` rate-limit that actually governs runs is the **24h `evolve_log.jsonl` window** (last `pr_created`), not `claude_session.check_budget` (which only reflects post-run sessions). Enqueue must use the former + one-pending-per-requester.
- px-evolve mutates the queue entry's `status` in place AND appends to `evolve_log.jsonl` → status reads must dedup by `id` (log wins).
- The QA verdict on the core design: **sound** — "no path from unauthenticated input to code changes."

## Open items for the plan

- Decide the exact storage location of the `pending_evolve_proposal` confirm-state (inside `obi_chat.jsonl` as a marker record vs. a tiny separate state file) and its expiry window.
- Confirm `bin/tool-evolve`'s current entry-building code so the refactor cleanly routes it through `enqueue_evolve` without changing CLI behavior.
- Confirm the obi-chat Claude invocation path (`_call_claude_public` / CLI) supports a JSON output mode, or specify the parse-convention + Pydantic validation used.
