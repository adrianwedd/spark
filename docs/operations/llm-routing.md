# LLM Routing, Budgets and Metering

**Owns:** which model answers, what it costs, and how that is counted.
`mind.call_llm`, `src/pxh/claude_session.py`, `src/pxh/token_log.py`,
`src/pxh/brain.py`'s meter.

---

## Invariant

### Reflection has a four-tier chain, and tier 1 is local

`mind.call_llm` (`src/pxh/mind.py`):

| Tier | Backend | When |
|---|---|---|
| 1 | Ollama on M5 (LAN) | primary for **all** personas, including SPARK |
| 2 | Claude Haiku | SPARK fallback when M5 is unreachable, or `PX_MIND_BACKEND=claude` |
| 3 | Ollama Cloud | when M5 is unreachable and Claude fails (needs `OLLAMA_CLOUD_API_KEY`) |
| 4 | Ollama on the Pi | opt-in via `PX_MIND_LOCAL_OLLAMA=1`; **off by default, OOM risk** |

`PX_MIND_BACKEND` selects the shape: `auto` (SPARK→Claude fallback,
others→Ollama only), `claude` (Claude primary), or `ollama`.

### Use `M5.local`, never the bare hostname `M5`

`OLLAMA_HOST` defaults to `http://M5.local:11434`. The UDR7 stopped serving the
bare `M5` hostname: `getent hosts M5` returns nothing, while
`getent hosts M5.local` resolves to 192.168.0.249 and answers `/api/tags` in
28 ms.

**A bare `M5` makes tier 1 fail instantly and silently spends money on tier 2.**
Treat any `192.168.1.x` address in an older document as stale — the network was
renumbered to `192.168.0.x`.

### Tier 2 asks the resident brain first

`mind.call_claude` → `call_brain_reflection` (kind `reflection`), and only
shells out to `claude -p` when `ask_brain` returns `None`. Warm context instead
of a cold process per thought, and **metered**.

Reflection reaches the meter via `ask_brain` **without** going through
`claude_session.py`'s per-type cooldowns. That is deliberate: reflection runs
every 5 minutes and a daily cap would simply stop it. The meter gives
visibility without a cap.

### `backend=` in the reflection log is the *configured* primary, not the tier that answered

To know which tier actually served, grep the log for `falling back`.
`call_llm()` also sets `result["backend"]` to the tier that served — use that
programmatically.

### Claude session types are budgeted

`src/pxh/claude_session.py`:

| Session type | Model | Cooldown | Daily quota |
|---|---|---|---|
| `evolve` | Opus | 24h | 1 |
| `self_debug` | Sonnet | 6h | 2 |
| `research` | Haiku | 2h | 3 |
| `compose` | Haiku | 4h | 2 |
| `conversation` | Sonnet | 15min | 4 |
| `blog` | Haiku | 30min | 5 |
| `consolidate` | Haiku | 20h | 1 |

Global: 30min between sessions (except `self_debug`/`blog`), 8/day cap. At ≤2
remaining, only `self_debug` and `evolve` are permitted. Bypass with
`PX_CLAUDE_BUDGET_DISABLED=1`. Log: `state/claude_sessions.jsonl`.

### Spend visibility requires `by_backend`

`token_log.log_usage()` takes a `backend` argument and splits totals under
`by_backend` in `state/token_usage.json`.

**The top-level totals mix free Ollama with paid Claude and cannot answer "what
am I spending".** Read `by_backend`.

### The remaining `claude -p` call sites are known and finite

`mind.py` (`call_claude_haiku`, now the *fallback* under the brain),
`api.py` (`_call_claude_public`), `bin/claude-voice-bridge`, `bin/px-blog`,
`bin/px-post` (legacy branch), `src/pxh/vision.py` (via
`bin/tool-describe-scene`), `bin/px-cron-say`.

The `claude -p` fallback under the brain meter is **still unmetered** — that is
the remaining hole in spend accounting.

Rollout is controlled by `PX_BRAIN_KINDS`; see
[architecture/resident-brain](../architecture/resident-brain.md).

### Personas need `think: false` on Ollama

Reasoning chains re-enable refusal in small models. `clean_response()` strips
scaffolding dividers before voice output.

---

## Why it looks like this

*History, not rule.*

Tier 1 was Claude for SPARK originally. Moving the LAN model to primary was a
cost decision, and the hostname bug then quietly undid it: a bare `M5` failed
tier 1 in milliseconds and fell through to paid Haiku on every single
reflection, which is a failure mode that looks exactly like working software.

The meter exists because reflection's tier 2 bypassed the session budget
entirely — 501 unbudgeted Claude calls in 19 days before anyone counted.
`state/token_usage.json`'s top-level totals did not reveal it, because they
counted free Ollama calls in the same number.
