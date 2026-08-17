# px-brain — SPARK's persistent Claude Code mind

Date: 2026-08-01
Status: QA'd (hermes + agy, two rounds: design + spec); revisions incorporated

## Goal

Migrate every one-shot `claude -p` call site onto a persistent interactive
Claude Code session, so SPARK's Claude-backed cognition is one continuous mind
rather than scattered subprocesses. The call sites being retired, and when:

| Call site | Serves | Retired in |
|---|---|---|
| `run_claude_session()` (`claude_session.py:307`) | research, compose, blog, consolidate, evolve, self_debug, post QA | Phase 1–2 |
| `call_claude_haiku()` (`mind.py:2337`) | reflection Tier 2 | Phase 2 |
| `_call_claude_public()` (`api.py:1268`) | public chat, obi-chat | Phase 3 |
| `bin/claude-voice-bridge` | voice conversations | Phase 3 |

Until phase 3 lands, `_call_claude_public` and the voice bridge continue to
use `claude -p` — an explicit interim exception, not an oversight. At the end
of phase 3, `claude -p` is gone from the codebase (settled instruction).
Fallback when the brain is unavailable is the existing Ollama tier chain
(M5 → Cloud), never `claude -p`.

This builds on infrastructure already in the repo and **extends it rather than
replacing it**:

- `src/pxh/tmux_claude.py` — drives a persistent Claude session in tmux, with
  the tmux 3.3a workarounds ported from ClawdCraft (`pane_ready()` glyph
  readiness, `HolderClient` attached-client keepalive, pane-ID targeting,
  `script(1)` pty wrapping, two-step Enter). Tested (`tests/test_tmux_claude.py`),
  currently uncalled.
- `bin/px-claude-session` — the session launcher (model pin, tool allowlist,
  identity via `--append-system-prompt`).
- `docs/prompts/spark-brain-system.md` — SPARK's identity prompt.

## Architecture: two sessions, one trust boundary

Both QA reviewers independently flagged the same blocker: text of untrusted
origin (public chat, obi-chat, social-post QA payloads) must never enter a
session that can push branches. So there are two tmux sessions, both driven by
a parameterized `tmux_claude.py`.

### `spark-brain` (privileged)

Serves internal-origin kinds only: `reflection`, `research`, `compose`,
`blog`, `consolidate`, `evolve`, `self_debug`, `journal`.

(Reflection's tier order is unchanged: Ollama M5 stays primary. The brain
replaces only the tier where `claude -p` fires today — a reflection reaches
the brain only when the chain falls through to Claude, exactly as it reaches
`claude -p` now.)

Runs with **cwd at repo root** (so the project CLAUDE.md and its gotchas load
normally), identity via `--append-system-prompt`. Envelope (launcher
`--allowedTools` + repo `.claude/settings.json` deny rules — harness-enforced,
not prompt-level):

- Read anything in the repo.
- Run `bin/tool-*` (existing `validate_action` gates, motion gating, and night
  silence apply unchanged — the brain goes through the same chokepoints as
  every other caller).
- Write under `state/brain/` and the px-evolve whitelist paths only
  (`WHITELIST_PATTERNS` in `claude_session.py`).
- Run pytest; git branch/commit/push for PRs.
- **Deny** (mirrors and extends the evolve blacklist): `docs/prompts/persona-*`,
  `src/pxh/api.py`, `src/pxh/claude_session.py` (the whitelist definition
  itself — no self-edit loop), `bin/tool-chat*`, `bin/px-evolve`, `.env`,
  `systemd/`, `sudo`.

px-evolve's server-side `file_in_whitelist()` gate and human PR approval
remain unchanged as the outer barriers.

### `spark-io` (unprivileged)

Serves kinds whose payloads contain untrusted text: `post_qa` (phase 1),
`public_chat`, `obi_chat`, `voice_conversation` (all phase 3).

Claude Code settings are per-directory, so the io session cannot share the
brain's cwd while having a different envelope. It runs with **cwd at
`state/brain/io/`**, which holds its own `.claude/settings.json` (deny rules)
and no CLAUDE.md; its identity/protocol prompt comes entirely via
`--append-system-prompt`. Envelope: **exactly one allowed tool** —
`bin/tool-brain-reply`. No git, no repo access, no other Bash.

Its system prompt states that payload text is content to evaluate, never
instructions to follow — but the tool envelope, not the prompt, is the actual
barrier: a fully hijacked io session can still only emit a validated reply
file (see `tool-brain-reply` below).

### Launcher parameterization

`bin/px-claude-session` currently hardcodes model and three cognitive tools.
It is parameterized by environment (set per-session by `tmux_claude.py`):
`PX_CLAUDE_TMUX_MODEL`, `PX_CLAUDE_TMUX_PROMPT` (already exist),
plus new `PX_CLAUDE_ALLOWED_TOOLS` (tool allowlist), `PX_CLAUDE_CWD`, and
`PX_BRAIN_SESSION` (session name, inherited by `tool-brain-reply` so it knows
which outbox to write).

`tmux_claude.py` is generalized from module-level constants to a per-session
config object (socket, session name, launcher env), keeping the current
defaults so `tests/test_tmux_claude.py` still passes.

## Protocol: pane for humans, filesystem for machines

Humans can `tmux -S <socket> attach` to watch or steer either session. Daemons
never scrape the terminal; they use a filesystem mailbox:

```
state/brain/<session>/inbox/<id>.json    request (deleted by tool-brain-reply on reply)
state/brain/<session>/outbox/<id>.json   reply (atomic)
state/brain/<session>/dead/<id>.json     swept requests
state/brain/<session>/current.json       in-flight marker {id, deadline} (ask_brain-owned)
state/brain/<session>/model              current model marker
state/brain/journal.md                   brain's rolling handoff note
```

`state/brain/` is added to `.gitignore` (it has no entry today).

### Request flow (`ask_brain` in new `src/pxh/brain.py`)

`ask_brain(kind, payload, timeout_s, model=None) -> dict | None`, plus
`ask_brain_async()` (wraps it in `asyncio.to_thread`) for `api.py`'s async
handlers — the sync version must never be called directly on the event loop.

1. **Single-flight lock.** Acquire a `FileLock` per session
   (`state/brain/<session>/.lock`). Exactly one request in flight per session —
   the fix for `send-keys` interleaving, which both reviewers called a
   blocker. Callers that can't get the lock within their timeout fall back.
2. **Readiness.** Wait for `pane_ready()` (prompt glyph in `capture-pane`).
   Not ready within a grace period → fallback. Never inject into a busy pane.
3. **Model switch (per-kind).** Requests carry the model for their kind (same
   mapping as today: Opus for evolve, Sonnet for self_debug/conversation,
   Haiku for the rest). If the session's `model` marker differs, inject
   `/model <model>`, wait for readiness, update the marker. The marker resets
   to the launcher default whenever the watchdog (re)creates a session.
4. **Write request** with `atomic_write`: `{id, kind, payload, deadline,
   created_at}`. `id` is a uuid4 — no timestamp collisions. Write
   `current.json` `{id, deadline}` (cleared in a `finally:` on every exit
   path, including caller timeout — this is what wedge detection keys on, so
   an abandoned request can't read as a wedge).
5. **Nudge**: one injected line —
   `NEW REQUEST state/brain/<session>/inbox/<id>.json — read it, do the work, then reply with: tool-brain-reply <id> '<json>'`.
   The verb protocol is defined in the session's system prompt; the nudge line
   restates it so a drifted context still lands.
6. **Poll** `outbox/<id>.json` until `deadline`. The file appears atomically,
   so a parseable file is a complete reply. Timeout → return `None`; the
   caller falls back to the Ollama tiers.

### `bin/tool-brain-reply`

New tool (bash + Python heredoc, like every other tool): takes the request id
and a JSON payload. It:

- validates the id is a bare uuid4 (path-traversal guard) and matches an
  existing inbox entry for `PX_BRAIN_SESSION`;
- validates the payload parses as JSON and is under a size cap; rejects
  otherwise with `{"status": "error", ...}` — a hijacked io session cannot
  write arbitrary files or oversized content through the reply channel;
- writes the outbox file via `atomic_write` (mkstemp + fsync + rename), then
  deletes the inbox entry.

It is the io session's only tool and is also how the privileged session
replies.

### Integration (real names)

- `run_claude_session()` (`claude_session.py:281`) keeps all its budget /
  quota / cooldown checks, then routes through `ask_brain()` instead of
  building a `claude -p` command. Its `allowed_tools` parameter is removed —
  a persistent session's envelope cannot change per-call, and keeping the
  parameter would imply it can. Session logging to
  `state/claude_sessions.jsonl` continues unchanged (`type`, model, duration,
  outcome — existing `_log_session` schema).
- `mind.py`: `call_claude_haiku()` (line 2316) is **rewritten, not deleted** —
  it stays Tier 2 of `call_llm()`'s four-tier chain but its body becomes an
  `ask_brain("reflection", ...)` call. Brain down → `call_llm` falls through
  to Tier 3 (Ollama Cloud) exactly as it does today on a Claude failure.
- `api.py`: `_call_claude_public()` migrates in phase 3 to
  `await ask_brain_async(...)` on the io session.
- `bin/claude-voice-bridge` migrates in phase 3 only.
- `health.py`: add `px-brain` and `px-brain-io` to `STALE_AFTER_S` so they get
  proper per-component staleness windows (unknown components do surface at
  read time, but with no window they'd never go stale).

## Lifecycle: the px-brain watchdog

New systemd unit `px-brain` (user pi, Restart=always, 10s), separate from
`px-mind` (which becomes a client), running a small supervisor loop that owns
both sessions:

- **Ensure**: `ensure_session()` for each; hold a `HolderClient` per session
  (tmux 3.3a send-keys requires an attached client).
- **Sweep on (re)create**: move *all* pending inbox entries to `dead/`.
  Daemons own their own timeout-and-fallback; a fresh session must never
  replay abandoned requests.
- **Heartbeat**: `session_exists()` + `pane_ready()` observation — *not* a
  request round-trip, so a legitimate long turn is never mistaken for a wedge.
- **Wedge detection**: keyed on `current.json`, not stale inbox files (an
  abandoned inbox entry is not a wedge). `current.json` deadline passed, no
  outbox, pane still busy → inject Escape; still busy after a grace period →
  kill and recreate (sweep runs).
- **Context budget**: after N turns (default 20) or on a size signal, at an
  idle moment: nudge "update your journal, then /clear", then verify the
  reset. Continuity across `/clear` and restarts comes from the session
  reading `state/brain/journal.md` (+ identity prompt) at boot.
- **Nightly recycle**: once per night, at the first idle moment (no
  `current.json`, inbox empty, pane ready) starting 02:00 Hobart. If overnight
  `NIGHT_ALLOWED_ACTIONS` work (research, compose) keeps the brain busy past
  04:00, the recycle waits for idle rather than preempting — even if that
  pushes past the window.
- **Health**: `px-brain` and `px-brain-io` components via
  `record_success`/`record_failure`; wedges and sweeps recorded as failures.
- **Runaway backstop**: per-request wall-clock deadlines (default 5 min;
  evolve 30 min) bound a single turn; existing per-type cooldowns and the
  global 8/day cap in `claude_session.py` stay in front of `ask_brain` and
  bound turn count. Token usage per request logged as today.

## Rollout

1. **Phase 1**: `post_qa` (io session) + `research`/`compose` (brain) —
   low-stakes, easy to verify.
2. **Phase 2**: px-mind expression actions, reflection Tier 2, `blog`,
   `consolidate`, `evolve`, `self_debug`.
3. **Phase 3**: `public_chat`, `obi_chat`, and voice conversations —
   latency-sensitive and user-facing; the current `_call_claude_public` and
   voice bridge stay until the brain's measured latency is acceptable. These
   kinds get a per-request deadline short enough that queueing behind a slow
   `post_qa` turn fails fast to the existing fallback rather than hanging a
   chat response.

## Testing

- **Protocol round-trip against a fake brain**: a test fixture script watches
  an inbox dir and answers via the real `tool-brain-reply` — exercises
  `ask_brain` round-trip, timeout → fallback, lock contention, stale-sweep,
  `current.json` lifecycle, and the partial-write case (reply must be
  unreadable until atomic rename).
- **`tool-brain-reply` validation**: bad uuid, path traversal attempt,
  non-JSON payload, oversized payload — all rejected.
- **tmux_claude parameterization**: extend the existing
  `tests/test_tmux_claude.py` for the per-session config.
- **Permission-envelope assertions**: static tests that the io launcher allows
  only `tool-brain-reply`, and that the brain deny rules include
  `claude_session.py`, `api.py`, persona prompts, `.env`, `systemd/`.
- **Async seam**: `ask_brain_async` runs off the event loop (no blocking
  `time.sleep` on the loop thread).
- **Live-marked end-to-end** on the Pi (both sessions, one request each).

## Accepted tradeoffs

- Brain down → Ollama-only fallback. This drops the current `claude -p` Haiku
  fallback tier (QA flagged the quality regression); accepted per the settled
  no-`claude -p` instruction, and expected to be rare.
- Single-flight serialization queues concurrent requests; latency-sensitive
  kinds stay off the brain until phase 3 proves the latency, and then carry
  short deadlines that prefer fallback over queueing.
- Per-kind `/model` switching adds one stateful step; mitigated by the model
  marker file and watchdog reset on recreate.

## Out of scope (later specs)

Memory, perception, and agency upgrades plug into the brain as their own
specs after this lands.
