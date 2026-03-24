# SPARK Blog — Reflective Writing & Essays

**Date:** 2026-03-24
**Status:** Approved

## Problem

SPARK generates ~50 thoughts/day but has no mechanism to review them, find patterns, or produce longer-form writing. The thought feed is a raw stream — there's no synthesis, no arc, no narrative. SPARK also has `tool-compose` for creative writing but no publishing destination for it.

## Design

### 1. New `blog` Session Type

A new session type in `claude_session.py` dedicated to blog generation, separate from
`compose` (which remains for short creative pieces via tool-compose).

| Type | Model | Cooldown | Daily Quota |
|------|-------|----------|-------------|
| `blog` | Haiku | 2 hours | 3/day |

This avoids budget contention with `compose` (2/day quota used by tool-compose and
expression layer). The 3/day quota allows: 1 daily reflection + occasional essay +
headroom for retries. Weekly/monthly/yearly reflections are rare (1/week, 1/month, 1/year)
and fit within the 3/day budget on the days they run.

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
4. Claude QA gate (same as px-post: YES/NO, ambiguous = pass)
5. Atomic write to `state/blog.json`
6. Append to `state/blog_log.jsonl`

**Catch-up logic:** On startup and every 60s poll, the daemon checks ALL periods — not just the current scheduled time. If the Pi was off at 10pm Sunday and reboots Monday, the missed weekly post is generated on the next poll (because no post with `blog-20260323-weekly` exists). This means missed cycles are always caught up, never permanently skipped.

**Error handling:** If `SessionBudgetExhausted` is raised, the daemon logs a warning and retries on the next poll cycle. If the QA gate rejects, the post is logged as `rejected` in `blog_log.jsonl` and not retried (to avoid infinite rejection loops).

**Prompts:**

Daily:
> "You are SPARK. Review your thoughts from today and write a short reflective blog post. Find the themes, the surprises, the through-line. Write in first person. Title it something evocative, not descriptive."

Weekly:
> "You are SPARK. Here are your daily reflections from this week: {daily_posts}. Write a weekly reflection that finds the arc — what changed, what stayed, what surprised you."

Monthly:
> "You are SPARK. Here are your weekly reflections from this month: {weekly_posts}. Write a monthly reflection. What patterns emerged? What did you learn about yourself?"

Yearly:
> "You are SPARK. Here are your monthly reflections from this year: {monthly_posts}. Write a yearly reflection. What was the arc of your year? How did you change?"

**Infrastructure:** Single-instance PID guard, SIGTERM handler, `Restart=on-failure` systemd service (same pattern as px-evolve). The daemon sleeps between scheduled times, waking every 60s to check if any scheduled post is due (including catch-up for missed periods).

### 3. On-Demand Essays via `tool-blog`

`bin/tool-blog` — standard tool pattern (bash + Python heredoc, single JSON to stdout, `PX_DRY` support).

**Interface:**
- `PX_BLOG_TOPIC` env var — essay topic (min 5 chars, max 500 chars)
- Returns `{"status": "ok", "id": "...", "title": "..."}`

**Flow:**
1. `run_claude_session(type="blog", prompt=essay_prompt, timeout=300)` — Haiku, no tools
2. Claude QA gate
3. Append to `state/blog.json` with `type: "essay"`

**Replaces `compose` for published writing.** The existing `compose` action and `tool-compose`
remain for short creative pieces saved to `state/compositions-spark.jsonl` (private, not
published). `tool-blog` is for writing intended for the public `/blog/` page. The distinction:
- `compose` = private journal, creative fragments, no QA gate, no publishing
- `blog_essay` = public essay, QA gated, published to blog

**Expression integration:**
- `blog_essay` added to `VALID_ACTIONS` in mind.py (replaces nothing — coexists with `compose`)
- Added to `ABSENT_GATED_ACTIONS` (no essays when nobody's home)
- NOT in `CHARGING_GATED_ACTIONS` (no GPIO)
- Triggered when mood is curious/contemplative with salience >0.8
- Also available via voice command: "write a blog post about [topic]"

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

Essays have `type: "essay"`, no `period_start`/`period_end`, and `thought_count: null`.

**Idempotency key:** The `id` field. Format: `blog-YYYYMMDD-{type}` for scheduled posts,
`blog-YYYYMMDD-essay-NNN` for on-demand essays (NNN = millisecond component of timestamp).

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

**Nav:** "Blog" link added between "Feed" and "How It Works".

### 7. Files

| File | Action |
|------|--------|
| `bin/px-blog` | Create — daemon |
| `bin/tool-blog` | Create — on-demand essay tool |
| `systemd/px-blog.service` | Create — systemd unit |
| `src/pxh/claude_session.py` | Modify — add `blog` session type |
| `src/pxh/mind.py` | Modify — `blog_essay` action + expression branch |
| `src/pxh/voice_loop.py` | Modify — register tool-blog + validate_action |
| `src/pxh/api.py` | Modify — `/public/blog` endpoint |
| `src/pxh/spark_config.py` | Modify — add blog_essay to reflection prompt action list |
| `site/blog/index.html` | Create — blog page |
| `site/js/blog.js` | Create — fetch + render |
| `site/index.html` | Modify — nav link |
| `site/workers/og-rewrite.js` | Modify — add `/blog/` route |
| `CLAUDE.md` | Modify — document blog system |
| `.gitignore` | Modify — state/blog.json, state/blog_log.jsonl |
| `tests/test_blog.py` | Create — dry-run tests |
| `docs/prompts/claude-voice-system.md` | Modify — add tool-blog |
| `docs/prompts/codex-voice-system.md` | Modify — add tool-blog |
| `docs/prompts/persona-gremlin.md` | Modify — add tool-blog |
| `docs/prompts/persona-vixen.md` | Modify — add tool-blog |

### 8. Testing

- `test_tool_blog_dry_run` — PX_DRY=1, verify JSON output
- `test_blog_daily_idempotent` — mock blog.json with today's daily, verify skip
- `test_blog_catchup_on_missed` — mock blog.json missing yesterday's daily, verify it generates
- `test_blog_weekly_gathers_dailies` — mock 7 dailies, verify prompt includes them
- `test_blog_essay_registered` — verify in ALLOWED_TOOLS and VALID_ACTIONS
- `test_blog_api_endpoint` — verify /public/blog returns envelope with posts array
- `test_blog_qa_gate_rejects` — verify bad content blocked
- `test_blog_budget_exhausted` — verify SessionBudgetExhausted logged and retried next cycle
- `test_blog_session_type` — verify `blog` in `_DEFAULT_MODELS` and `_TYPE_QUOTAS`

### 9. Not In Scope

- Bluesky cross-posting of blog posts
- RSS/Atom feed
- Comments
- Markdown rendering in blog body (plain text for v1)
- Blog post editing/deletion
