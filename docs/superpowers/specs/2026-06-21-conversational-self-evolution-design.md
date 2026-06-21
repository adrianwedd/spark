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

### C1 — `enqueue_evolve` helper
A single reusable function (module-level, likely `src/pxh/evolve_queue.py` or extracted from `bin/tool-evolve`) that appends a well-formed entry to `state/evolve_queue.jsonl`:
```
{"id": <unique>, "intent": <feature description>, "requester": "obi"|"adrian",
 "source": "obi-chat"|"cli", "ts": <iso>}
```
- **What it does:** validate/format intent, generate id, append under `FileLock`.
- **Interface:** `enqueue_evolve(intent: str, requester: str, source: str) -> dict` (returns the entry) or raises `EvolveQuotaError` / `ValueError`.
- **Depends on:** the existing evolve_queue format/path; `claude_session` daily-quota check for the `evolve` session type (1/day). If the quota is spent, raise `EvolveQuotaError` so callers can speak a friendly message.
- Reuse, don't duplicate: if `bin/tool-evolve` already builds entries, factor the shared builder into this helper so both use one format.

### C2 — obi-chat build-intent detection
Change the obi-chat Claude call from plain-text reply to **structured output** `{reply: str, evolve_intent: str | null}`:
- `_OBI_CHAT_SYSTEM_PROMPT` gains rules: when Obi expresses a wish for a new capability, **propose it and ask to confirm**, leaving `evolve_intent` null. Only when Obi explicitly agrees in the next turn, set `evolve_intent` to a concise feature description. Never set it without an explicit yes.
- The handler: speaks `reply`; if `evolve_intent` is non-null, calls `enqueue_evolve(evolve_intent, "obi", "obi-chat")`. On `EvolveQuotaError`, SPARK's spoken reply already covers it (the prompt is told the daily limit) OR the handler appends a friendly note.
- Two-turn confirmation memory uses the existing obi_chat.jsonl history injection — no new state.
- Input is already cleaned by `_sanitize_chat_text()`.

### C3 — "My Projects" status
- `GET /api/v1/obi/projects` (auth required) merges `evolve_queue.jsonl` (pending/building) + `evolve_log.jsonl` (completed, with PR url + state), filtered to `requester == "obi"`, newest first. Shape: `[{id, intent, state: "pending"|"building"|"ready"|"merged"|"failed", pr_url?, ts}]`.
- Dashboard **"My Projects"** panel (admin or a kid-facing area) renders the list via the existing `api()` helper.
- obi-chat injects a compact projects summary (e.g. last 5 items + state) into the obi-chat context so "is my joke tool ready?" is answered from real data, not hallucinated.

### C4 — Requester attribution
Evolve entries carry `requester`/`source` (C1). px-evolve passes them through to `evolve_log.jsonl` and, ideally, notes "requested by Obi" in the generated PR body. C3's status read depends on this field existing on both queue and log records.

## Data flow & state

- **New/changed state:** `evolve_queue.jsonl` entries gain `requester`/`source` (additive; px-evolve must tolerate their absence for legacy/CLI entries). `evolve_log.jsonl` gains the same passthrough. No new long-lived state files.
- **Quota:** one evolve/day (existing). The conversational path must surface "spent for today" gracefully.
- **Dedup:** if an identical pending intent from the same requester already exists, the helper should no-op (or return the existing entry) rather than stack duplicates.

## Error handling

| Condition | Behavior |
|---|---|
| Quota spent (1/day used) | `enqueue_evolve` raises `EvolveQuotaError`; SPARK says it can take one project a day, try tomorrow. No queue write. |
| Empty/garbage intent | `ValueError`; SPARK asks Obi to describe it again. No write. |
| Duplicate pending intent | No-op; SPARK says it's already on the list. |
| px-evolve worker fails (pytest/whitelist) | Recorded in `evolve_log` as `failed`; "My Projects" shows failed; Adrian sees it. Conversation path is decoupled, so no user-facing crash. |
| obi-chat structured-output parse failure | Fall back to treating the response as a plain reply with `evolve_intent=null` (never enqueue on ambiguous parse). |

## Security model

- **Structural gate:** enqueue is only invoked from auth-required endpoints (obi-chat, dashboard) / local CLI. The public `/chat` and voice loop have no path to `enqueue_evolve`.
- **No new execution surface:** the conversation LLM cannot read/write files or run shell; it only emits a text intent that becomes a queue row.
- **Inherited px-evolve safety (unchanged):** file whitelist + blacklist, max-3-files, pytest-must-pass, Opus, and **mandatory human PR approval**. An adversarially-phrased intent still cannot escape the whitelist or self-merge.
- **Input sanitisation:** `_sanitize_chat_text()` strips `<>`, newlines, NUL before the intent is stored or interpolated.
- **Quota** doubles as an abuse-rate limit on the conversational trigger.

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

## Open items for the plan

- Confirm the exact current `evolve_queue.jsonl` entry schema produced by `bin/tool-evolve` and consumed by `bin/px-evolve` (fields: `id`, `intent`, optional `introspection`) and whether a shared builder exists to extract.
- Confirm the `claude_session` API for checking/decrementing the `evolve` daily quota from the API process (it must be callable from `api.py`, not only the px-evolve worker).
- Decide where the structured-output contract for obi-chat is enforced (JSON schema vs. a parsed convention) and the fallback.
