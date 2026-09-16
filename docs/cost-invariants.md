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

### 1. No production model CLI at all

No production code may invoke Claude — not residently, not cold. #317 Phase 3
deleted the resident `spark-brain` session, its supervisor, its mailbox and its
reply tool; what is left is one direct API call to the cognition tier per kind.

**Enforcement:**
- `tools/check_resident_claude.py` — CI scanner that detects cold-start patterns
  (argv lists with `-p`, shell execs, forbidden helpers, fossil artifacts) **and
  fails if any retired transport path reappears** (`FORBIDDEN_PATHS`)
- `tests/test_resident_only_invariant.py` — pins the scanner and its canaries
- Both are blacklisted from px-evolve (`model_session.BLACKLIST_FILES`)

**Verify on the Pi:**
```bash
ps aux | grep claude    # no production process; a human's own session only
```

### 2. One route per kind, and no dial

There is no `PX_BRAIN_KINDS` and no second destination. A kind is either in
`model_session._COGNITION_KINDS` or it has no backend and raises
`ColdStartForbidden`. `evolve` is the one deliberate member of the second set.

**Verify on the Pi:**
```bash
python tools/check_resident_claude.py --list    # debt map, expect clean
```

### 3. One cognition tier, and it defers

Reflection, post QA, blog QA, public chat, Obi chat, direct voice, semantic
vision, `self_debug` and `consolidate` all run on the cognition tier —
**Ollama Cloud by default since #308** (`https://ollama.com`,
`deepseek-v4.1-flash:cloud`, `OLLAMA_API_KEY`); set `PX_M5_SPARK_HOST` to a LAN
daemon to run it locally instead. On failure these **defer** — they do not fall
through to Claude (there is none), and they do not reach a Pi-local model.

**Code paths:**
- `mind.py::call_llm()` → `m5.ask_m5()` → defer on failure
- `api.py::_call_tier_public()` → `m5.ask_m5()` → raise on failure (no fallback)
- `bin/px-post::run_qa_gate()` → `m5.ask_m5()` → defer on failure
- `bin/px-blog::_qa_gate()` → `m5.ask_m5()` → defer on failure
- `voice_loop.run_voice_turn()` → `m5.ask_m5()` → one retry on transport
  failure, then the deterministic acknowledgement
- `vision.describe_image()` → `m5.ask_m5()` (image inlined) → honest fallback

**Pinned by:** `tests/test_mind_fallback.py` (every deferral path),
`tests/test_dead_tier_functions.py` (the ladder stays deleted),
`tests/test_cognition_tier_routing.py` (the retired transport is *absent*,
not disabled)

### 4. No second destination

The old reflection ladder — `call_ollama()`, `LOCAL_OLLAMA_HOST`,
`OLLAMA_CLOUD_HOST` — was deleted in #308 rather than left dead, and the
resident session followed in #317 Phase 3. A local daemon is reachable
**only** by an explicit `PX_M5_SPARK_HOST`, and `resident` mode is rejected
outright against a hosted host (asking for a residency set from a cloud
endpoint is a 401, which is how #302 turned into a 22-hour silent outage).

### 5. Untrusted text has no tools anywhere

Untrusted text (public chat, Obi chat, post QA, blog QA) runs on the cognition
tier, which has no tools and no filesystem access. That was already true before
the retirement — it was the reason the tier existed — and it is now the only
boundary there is, rather than one of two.

**#308 caveat, stated rather than implied:** that text leaves the LAN (it goes
to Ollama Cloud) where it used to stay on it. The *privilege* boundary is
unchanged — no tools, no filesystem, no repository. The *disclosure* surface is
not, and it is the reason this is written down here. The same caveat now
applies to photos on `describe_scene`, which are inlined into the request
rather than read by a local session.

## Model call site inventory

| Call site | Route | Classification |
|---|---|---|
| `mind.py::reflection()` | `call_llm()` → `m5.ask_m5("reflection")` | Cognition tier, defers on failure |
| `mind.py::self_debug` action | `run_model_session("self_debug")` → tier | Cognition tier, budget-gated |
| `memory.py::consolidate()` | `run_model_session("consolidate")` → tier | Cognition tier, budget-gated; the nightly pass |
| `bin/px-blog::generate_post()` | `run_model_session("blog")` → tier | Cognition tier, budget-gated |
| `bin/tool-research` / `tool-compose` / `tool-blog` | `run_model_session(...)` → tier | Cognition tier, budget-gated |
| `bin/px-blog::_qa_gate()` | `m5.ask_m5("blog_qa")` | Cognition tier, defers on failure |
| `bin/px-post::run_qa_gate()` | `m5.ask_m5("post_qa")` | Cognition tier, defers on failure |
| `bin/px-cron-say::call_model()` | `m5.ask_m5("cron_say")` | Cognition tier, skips the slot on failure |
| `api.py::public_chat()` | `m5.ask_m5("public_chat")` | Cognition tier, raises on failure |
| `api.py::post_obi_chat()` | `m5.ask_m5("obi_chat")` | Cognition tier, raises on failure |
| `vision.py::describe_image()` | `m5.ask_m5("describe_scene")` | Cognition tier, honest fallback |
| `voice_loop.py` (voice turn) | `m5.ask_m5("voice_turn")` | Cognition tier, one bounded retry then the ack |
| `bin/px-evolve` | `run_model_session("evolve")` | **No backend** — raises `ColdStartForbidden` |

**Every model-bearing path goes through `pxh.m5.ask_m5()`.** `model_session`
is a budget/quota/logging wrapper in front of it, not a second transport:
`state/model_sessions.jsonl` records the budget-gated kinds, `state/m5/meter.json`
counts every tier request, and `provider` in the session log says which one
served the call. There is no path around both.

**Tier spend is metered, but not capped.** `state/m5/meter.json` counts requests
by kind/route/status. There is no daily cap on this tier, so "how much Ollama
Cloud are we using" is answered by that file and by `by_backend["ollama-m5"]`
in `state/token_usage.json`. Since #308 that label no longer means "free local
compute".**

## Budget controls

- `model_session.py`: per-kind cooldowns, daily quotas, global 8/day cap
- `m5.py::_record_request()`: per-kind per-route per-status meter (observability, not a cap)
- `token_log.log_usage()`: `by_backend` split in `state/token_usage.json`

## Operator checks

```bash
# Zero production model CLI processes of any kind
ps aux | grep "[c]laude.*-p"   # should be empty

# The retired transport is gone, and CI will fail if it comes back
python tools/check_resident_claude.py --list    # expect clean

# Cognition-tier meter (by kind, by route, by status)
cat state/m5/meter.json

# Token usage by backend
jq '.by_backend | keys' state/token_usage.json

# Budget-gated session log
tail -20 state/model_sessions.jsonl

# No scheduled tasks spawning a model CLI
crontab -l    # only px-cron-say, which calls pxh.m5
```