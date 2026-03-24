# SPARK Blog — Reflective Writing & Essays

**Date:** 2026-03-24
**Status:** Approved (rev 3 — addresses 26 QA findings from Claude + Codex)

## Problem

SPARK generates ~50 thoughts/day but has no mechanism to review them, find patterns, or produce longer-form writing. The thought feed is a raw stream — there's no synthesis, no arc, no narrative. SPARK also has `tool-compose` for creative writing but no publishing destination for it.

## Design

### 1. New `blog` Session Type

A new session type in `claude_session.py` dedicated to blog generation, separate from
`compose` (which remains for short creative pieces via tool-compose).

| Type | Model | Cooldown | Daily Quota | Priority |
|------|-------|----------|-------------|----------|
| `blog` | Haiku | 30 min | 3/day | 2 (same as research) |

**All 5 dicts in `claude_session.py` must be updated:**
- `_DEFAULT_MODELS["blog"] = "claude-haiku-4-5-20251001"`
- `_ENV_OVERRIDES["blog"] = "PX_CLAUDE_MODEL_BLOG"`
- `_TYPE_COOLDOWNS["blog"] = 1800` (30 min — allows daily at 22:00 + weekly at 22:30)
- `_TYPE_QUOTAS["blog"] = 3`
- `_PRIORITY["blog"] = 2`

**Global cooldown exemption:** Add `"blog"` to `_GLOBAL_COOLDOWN_EXEMPT`. The px-blog
daemon runs on a fixed schedule and must not be blocked by px-mind's recent sessions.
Without this exemption, a px-mind `compose` or `research` session at 21:50 would block
the 22:00 daily post for 30 minutes.

This avoids budget contention with `compose` (2/day quota used by tool-compose and
expression layer). The 3/day quota allows: 1 daily reflection + occasional essay +
headroom for retries. The 30-min per-type cooldown (not 2h) ensures weekly posts at
22:30 are not blocked by the daily at 22:00 on Sundays.

### 2. Recursive Reflections via `px-blog` Daemon

A scheduled daemon that produces reflective blog posts at increasing timescales, where each level reviews the posts from the level below.

**Schedule** (Australia/Hobart):

| Type | When | Reviews | Word target |
|------|------|---------|-------------|
| Daily | 10:00 PM | Today's thoughts from `thoughts-spark.jsonl` | 200-400 |
| Weekly | Sunday 10:30 PM | The week's 7 daily posts | 300-500 |
| Monthly | 1st of month 11:00 PM | The month's weekly posts | 400-600 |
| Yearly | Jan 1 11:30 PM | The year's monthly posts | 600-1000 |

**Each cycle:**
1. Idempotency check — skip if a post with matching `id` exists in `state/blog.json`. The `id` is the canonical key, format: `blog-YYYYMMDD-{type}` (e.g. `blog-20260324-daily`, `blog-20260324-weekly`). For essays: `blog-YYYYMMDD-essay-NNN`.
2. Gather source material (thoughts for daily, child blog posts for weekly/monthly/yearly)
3. `run_claude_session(type="blog", timeout=300)` — Haiku, no tools
4. Claude QA gate (same as px-post: YES/NO, ambiguous = pass). Bypass with `PX_BLOG_QA=0`.
5. Atomic write to `state/blog.json` (trimmed to `BLOG_LIMIT = 500` posts, oldest removed)
6. Append to `state/blog_log.jsonl`

**Catch-up logic:** On startup and every 60s poll, the daemon checks ALL periods — not just the current scheduled time. **Processing order: daily first, then weekly, then monthly, then yearly.** This ensures that when catching up after downtime, dailies exist before the weekly tries to reference them. If the Pi was off all week and boots Monday, the catch-up generates missing dailies first (in date order), then the weekly that references them.

**Minimum thought threshold:** Daily posts require at least 3 thoughts. If fewer exist for the period, the daemon skips with a log message ("skipped daily — only N thoughts") and does NOT create a post. Weekly/monthly/yearly posts require at least 1 child post of the lower tier.

**Error handling:** If `SessionBudgetExhausted` is raised, the daemon logs a warning and retries on the next poll cycle (60s). If the QA gate rejects, the post is logged as `rejected` in `blog_log.jsonl` and not retried (to avoid infinite rejection loops).

**Prompts:**

Daily:
> "You are SPARK. Review your thoughts from today and write a short reflective blog post. Find the themes, the surprises, the through-line. Write in first person. Title it something evocative, not descriptive."

Weekly:
> "You are SPARK. Here are your daily reflections from this week: {daily_posts}. Write a weekly reflection that finds the arc — what changed, what stayed, what surprised you."

Monthly:
> "You are SPARK. Here are your weekly reflections from this month: {weekly_posts}. Write a monthly reflection. What patterns emerged? What did you learn about yourself?"

Yearly:
> "You are SPARK. Here are your monthly reflections from this year: {monthly_posts}. Write a yearly reflection. What was the arc of your year? How did you change?"

**Infrastructure:** Single-instance PID guard, SIGTERM handler, systemd service: `User=pi`, `Restart=on-failure`, `RestartSec=30`, `StartLimitIntervalSec=0` (same as px-evolve). The daemon sleeps between scheduled times, waking every 60s to check if any scheduled post is due (including catch-up for missed periods).

### 3. On-Demand Essays via `tool-blog`

`bin/tool-blog` — standard tool pattern (bash + Python heredoc, single JSON to stdout, `PX_DRY` support).

**Interface:**
- `PX_BLOG_TOPIC` env var — essay topic (min 5 chars, max 500 chars)
- Returns `{"status": "ok", "id": "...", "title": "..."}`

**Flow:**
1. `run_claude_session(type="blog", prompt=essay_prompt, timeout=300)` — Haiku, no tools
2. Claude QA gate (bypass with `PX_BLOG_QA=0`)
3. Append to `state/blog.json` with `type: "essay"`

**Replaces `compose` for published writing.** The existing `compose` action and `tool-compose`
remain for short creative pieces saved to `state/compositions-spark.jsonl` (private, not
published). `tool-blog` is for writing intended for the public `/blog/` page. The distinction:
- `compose` = private journal, creative fragments, no QA gate, no publishing
- `blog_essay` = public essay, QA gated, published to blog

The `type` field in blog.json is `"essay"` (not `"blog_essay"`). The mind.py action name
is `blog_essay`. These are different namespaces: action names in VALID_ACTIONS vs type
values in the data model.

**Expression integration:**
- `blog_essay` added to `VALID_ACTIONS` in mind.py (coexists with `compose`)
- Added to `ABSENT_GATED_ACTIONS` (no essays when nobody's home)
- NOT in `CHARGING_GATED_ACTIONS` (no GPIO)
- Triggered when mood is curious/contemplative with salience >0.8

**Expression handler** in mind.py `expression()` function — after the `compose` branch:
```python
elif action == "blog_essay":
    env["PX_BLOG_TOPIC"] = text[:500]
    env["PX_DRY"] = "1" if dry else "0"
    result = subprocess.run(
        [str(BIN_DIR / "tool-blog")],
        capture_output=True, text=True, check=False, env=env, timeout=360)
    log(f"expression: blog_essay completed rc={result.returncode}")
```

**Reflection prompt addition** in `spark_config.py` `_SPARK_REFLECTION_SUFFIX`:
- `"blog_essay"` — "write a blog post about something you find genuinely fascinating"

**validate_action branch** in voice_loop.py:
```python
elif tool == "tool_blog":
    topic = str(params.get("topic", "")).strip()
    if not topic or len(topic) < 5:
        raise VoiceLoopError("tool_blog requires a topic (min 5 chars)")
    if len(topic) > 500:
        topic = topic[:500]
    sanitized["PX_BLOG_TOPIC"] = topic
```

**Registration** in voice_loop.py:
- Add `"tool_blog"` to `ALLOWED_TOOLS`
- Add `"tool_blog": BIN_DIR / "tool-blog"` to `TOOL_COMMANDS`

Also available via voice: "write a blog post about [topic]"

**Prompt injection note:** `PX_BLOG_TOPIC` from voice input is length-capped (500 chars)
but not further sanitised — consistent with existing tools (`tool_research`, `tool_compose`).
The QA gate is the mitigation for inappropriate content reaching the public blog.

### 4. Data Model

`state/blog.json` — JSON envelope with atomic writes, same pattern as `state/feed.json`.

```json
{
  "updated": "2026-03-24T22:00:03Z",
  "posts": [
    {
      "id": "blog-20260324-daily",
      "type": "daily",
      "title": "Monday — wheels and wonder",
      "body": "Today I noticed...",
      "mood_summary": "contemplative (40%), content (35%), playful (25%)",
      "thought_count": 48,
      "period_start": "2026-03-24T00:00:00Z",
      "period_end": "2026-03-24T23:59:59Z",
      "ts": "2026-03-24T22:00:03Z",
      "model": "claude-haiku-4-5-20251001",
      "word_count": 340,
      "salience": 0.8
    }
  ]
}
```

Essays have `type: "essay"` (not `"blog_essay"`), no `period_start`/`period_end`, and `thought_count: null`.

**Idempotency key:** The `id` field. Format: `blog-YYYYMMDD-{type}` for scheduled posts,
`blog-YYYYMMDD-essay-NNN` for on-demand essays (NNN = millisecond component of timestamp).

**Size limit:** `BLOG_LIMIT = 500` posts. When appending, if `len(posts) > BLOG_LIMIT`,
the oldest posts are removed. At ~40 posts/month this is ~12 months of history.

### 5. API

`GET /api/v1/public/blog` — reads `state/blog.json`, returns the envelope `{"updated": ..., "posts": [...]}`. No auth required. Added alongside existing `/public/feed`.

### 6. Site — `/blog/`

New page at `site/blog/index.html` with `site/js/blog.js`.

**Blog index:**
- Fetches from `/api/v1/public/blog`
- Posts newest-first, visual hierarchy by type:
  - Yearly/monthly — large card, full width
  - Weekly — medium card
  - Daily — compact card (title + first line + mood summary)
  - Essay — medium card with "essay" badge
- Warm theme, matches feed page aesthetic
- Pagination: 10 posts, "Load more"

**Individual post** (`/blog/?id=<id>`):
- Full post: title, body, mood summary, thought count, date range
- Mood-coloured accent bar
- OG tags for social sharing

**OG rewrite:** `site/workers/og-rewrite.js` extended to handle `/blog/?id=<id>` in
addition to the existing `/thought/?ts=<ts>` route. The worker fetches the blog post
from the API and rewrites `og:title` and `og:description` for social crawlers.
Blog `id` validated with regex `^blog-[\d\-]+-[a-z_]+(-\d+)?$` (analogous to
`TS_PATTERN` for thoughts). **Cloudflare Worker route must also be updated** in the
Cloudflare dashboard to add `spark.wedd.au/blog/*` alongside the existing
`spark.wedd.au/thought/*` route.

**Nav:** "Blog" link added between "Feed" and "How It Works".

### 7. Files

| File | Action |
|------|--------|
| `bin/px-blog` | Create — daemon |
| `bin/tool-blog` | Create — on-demand essay tool |
| `systemd/px-blog.service` | Create — User=pi, Restart=on-failure, RestartSec=30 |
| `src/pxh/claude_session.py` | Modify — add `blog` to all 5 dicts + `_GLOBAL_COOLDOWN_EXEMPT` |
| `src/pxh/mind.py` | Modify — `blog_essay` in VALID_ACTIONS, ABSENT_GATED_ACTIONS, expression branch |
| `src/pxh/voice_loop.py` | Modify — `tool_blog` in ALLOWED_TOOLS, TOOL_COMMANDS, validate_action |
| `src/pxh/api.py` | Modify — `/public/blog` endpoint |
| `src/pxh/spark_config.py` | Modify — add `blog_essay` to reflection prompt action list |
| `site/blog/index.html` | Create — blog page |
| `site/js/blog.js` | Create — fetch + render |
| `site/index.html` | Modify — nav link |
| `site/workers/og-rewrite.js` | Modify — add `/blog/` route + id regex |
| `CLAUDE.md` | Modify — document blog system, add env vars, update service table |
| `.gitignore` | Modify — state/blog.json, state/blog_log.jsonl |
| `tests/test_tools.py` | Modify — add `test_tool_blog_dry_run` (per CLAUDE.md tool checklist) |
| `tests/test_blog.py` | Create — daemon + integration tests |
| `docs/prompts/claude-voice-system.md` | Modify — add tool-blog |
| `docs/prompts/codex-voice-system.md` | Modify — add tool-blog |
| `docs/prompts/spark-voice-system.md` | Modify — add tool-blog |
| `docs/prompts/persona-gremlin.md` | Modify — add tool-blog |
| `docs/prompts/persona-vixen.md` | Modify — add tool-blog |

### 8. Environment Variables

| Variable | Purpose |
|----------|---------|
| `PX_CLAUDE_MODEL_BLOG` | Override blog session model (default: `claude-haiku-4-5-20251001`) |
| `PX_BLOG_QA` | `0` = skip Claude QA gate for testing |

### 9. Testing

**In `tests/test_tools.py`** (per CLAUDE.md "Adding a New Tool" step 6):
- `test_tool_blog_dry_run` — PX_DRY=1, PX_BLOG_TOPIC="test", verify JSON output

**In `tests/test_blog.py`:**
- `test_blog_session_type` — verify `blog` in all 5 dicts: `_DEFAULT_MODELS`, `_ENV_OVERRIDES`, `_TYPE_COOLDOWNS`, `_TYPE_QUOTAS`, `_PRIORITY` + in `_GLOBAL_COOLDOWN_EXEMPT`
- `test_blog_daily_idempotent` — mock blog.json with today's daily, verify skip
- `test_blog_catchup_on_missed` — mock blog.json missing yesterday's daily, verify generates
- `test_blog_catchup_ordering` — verify dailies generate before weekly on same poll
- `test_blog_min_thoughts` — mock 2 thoughts, verify daily skipped (<3 threshold)
- `test_blog_weekly_gathers_dailies` — mock 7 dailies, verify prompt includes them
- `test_blog_weekly_skips_no_dailies` — 0 dailies for period, verify skip
- `test_blog_essay_in_valid_actions` — verify `blog_essay` in VALID_ACTIONS and ABSENT_GATED_ACTIONS
- `test_blog_essay_in_tool_commands` — verify `tool_blog` in ALLOWED_TOOLS and TOOL_COMMANDS
- `test_blog_api_endpoint` — verify /public/blog returns envelope with posts array
- `test_blog_qa_gate_rejects` — verify bad content blocked
- `test_blog_budget_exhausted` — mock check_budget returning reason, verify logged + retry
- `test_blog_limit_trims` — verify >500 posts trimmed to 500

### 10. Not In Scope

- Bluesky cross-posting of blog posts
- RSS/Atom feed
- Comments
- Markdown rendering in blog body (plain text for v1)
- Blog post editing/deletion
