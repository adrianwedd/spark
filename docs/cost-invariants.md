# SPARK Cost Invariants — Operator Guide

**The rule:** SPARK cannot quietly cost money in normal operation — neither
Anthropic API billing nor Ollama Cloud usage beyond what an operator has
chosen.

This document is the operator-facing summary of the architectural invariants
that prevent unplanned metered spend. It was written for Anthropic billing
alone; since #308 the cognition tier is a metered cloud service too, so
invariant 3 covers both. It is intentionally short — the
code, tests, and CLAUDE.md are the authoritative references.

## The five invariants

### 1. No production `claude -p`

No production code may invoke Claude non-residently. The resident `spark-brain`
tmux session is SPARK's sole Claude execution substrate.

**Enforcement:**
- `tools/check_resident_claude.py` — CI scanner that detects cold-start patterns
  (argv lists with `-p`, shell execs, forbidden helpers, fossil artifacts)
- `tests/test_resident_only_invariant.py` — pins the scanner and its canaries
- Both are blacklisted from px-evolve (`claude_session.BLACKLIST_FILES`)

**Verify on the Pi:**
```bash
ps aux | grep claude    # exactly one process, no -p flag
```

### 2. One resident Claude session only

`spark-brain` is the only resident Claude session. The old `spark-io` session
was removed in #242 once M5 absorbed all its kinds. An unclassified kind
**fails closed** — `brain.ask_brain()` returns `None`, `claude_session.py`
raises `ColdStartForbidden`.

**Verify on the Pi:**
```bash
tmux -S /tmp/tmux-1000/px-mind list-sessions    # spark-brain only
```

### 3. One cognition tier, and it defers

Reflection, post QA, blog QA, public chat, and Obi chat run on the cognition
tier — **Ollama Cloud by default since #308** (`https://ollama.com`,
`deepseek-v4.1-flash:cloud`, `OLLAMA_API_KEY`); set `PX_M5_SPARK_HOST` to a LAN
daemon to run it locally instead. On failure these **defer** — they do not fall
through to Claude, and they do not reach a Pi-local model.

**Code paths:**
- `mind.py::call_llm()` → `m5.ask_m5()` → defer on failure (never `call_claude`)
- `api.py::_call_claude_public()` → `m5.ask_m5()` → raise on failure (no fallback)
- `bin/px-post::_qa_via_m5()` → defer on failure
- `bin/px-blog::_qa_gate()` → `m5.ask_m5()` → defer on failure

**Pinned by:** `tests/test_mind_fallback.py` (every deferral path),
`tests/test_dead_tier_functions.py` (the ladder stays deleted)

### 4. No second destination

The old reflection ladder — `call_ollama()`, `LOCAL_OLLAMA_HOST`,
`OLLAMA_CLOUD_HOST` — was deleted in #308 rather than left dead. `call_llm()`
reaches `m5.ask_m5()` and defers; nothing else is tried. A local daemon is
reachable **only** by an explicit `PX_M5_SPARK_HOST`, and `resident` mode is
rejected outright against a hosted host (asking for a residency set from a
cloud endpoint is a 401, which is how #302 turned into a 22-hour silent
outage).

### 5. Raw public/Obi/post text never reaches privileged spark-brain

Untrusted text (public chat, Obi chat, post QA, blog QA) runs on the cognition
tier, which has no tools and no filesystem access — a stronger boundary than
any scoped Claude session. The `spark-brain` session's `Read` grant is narrowed
in code (`vision._within_photos`) and pinned by `tests/test_brain_envelope.py`.

**#308 caveat, stated rather than implied:** that text now leaves the LAN (it
goes to Ollama Cloud) where it used to stay on it. The *privilege* boundary is
unchanged — no tools, no filesystem, no repository. The *disclosure* surface is
not, and it is the reason this is written down here.

## Claude call site inventory

| Call site | Route | Classification |
|---|---|---|
| `mind.py::reflection()` | `call_llm()` → `m5.ask_m5("reflection")` | Cognition tier (Ollama Cloud by default), defers on failure |
| `mind.py::self_debug` action | `claude_session.run_claude_session("self_debug")` | Brain, budget-gated |
| `memory.py::consolidate()` | `claude_session.run_claude_session("consolidate")` | Brain, budget-gated |
| `bin/px-blog::generate_post()` | `claude_session.run_claude_session("blog")` | Brain, budget-gated |
| `bin/px-blog::_qa_gate()` | `m5.ask_m5("blog_qa")` | Cognition tier, defers on failure |
| `bin/px-post::run_qa_gate()` | `m5.ask_m5("post_qa")` | Cognition tier, defers on failure |
| `bin/px-cron-say::call_claude()` | `brain.ask_brain("cron_say")` | Brain, metered |
| `bin/px-evolve` | `claude_session.run_claude_session("evolve")` | Brain, raises `ColdStartForbidden` |
| `api.py::public_chat()` | `m5.ask_m5("public_chat")` | Cognition tier, raises on failure |
| `api.py::post_obi_chat()` | `m5.ask_m5("obi_chat")` | Cognition tier, raises on failure |
| `vision.py::describe_image()` | `brain.ask_brain("describe_scene")` | Brain, metered |
| `voice_loop.py` (voice turn) | `brain.ask_brain("voice_turn")` | Brain, metered |

**Every Claude-bearing path goes through either `brain.ask_brain()` (metered
at `state/brain/meter.json`) or `claude_session.run_claude_session()` (budget-
gated at `state/claude_sessions.jsonl`). There is no path around both.**

**Cognition-tier spend is metered too, but not capped.** `state/m5/meter.json`
counts requests by kind/route/status. There is no daily cap on this tier, so
"how much Ollama Cloud are we using" is answered by that file and by
`by_backend["ollama-m5"]` in `state/token_usage.json`. Since #308 that label no
longer means "free local compute".**

## Budget controls

- `claude_session.py`: per-type cooldowns, daily quotas, global 8/day cap
- `brain.py::record_request()`: per-kind per-day meter (observability, not a cap)
- `token_log.log_usage()`: `by_backend` split in `state/token_usage.json`

## Operator checks

```bash
# Zero cold claude -p processes
ps aux | grep "[c]laude.*-p"   # should be empty

# Exactly one resident session
tmux -S /tmp/tmux-1000/px-mind list-sessions   # spark-brain only

# Brain meter (today's request counts by kind)
cat state/brain/meter.json

# Cognition-tier meter (by kind, by route, by status)
cat state/m5/meter.json

# Token usage by backend
jq '.by_backend | keys' state/token_usage.json

# Claude session log (budget-gated sessions)
tail -20 state/claude_sessions.jsonl

# Run the invariant scanner
python tools/check_resident_claude.py --list

# No scheduled tasks hitting Claude
crontab -l    # only px-cron-say, which uses brain.ask_brain
```