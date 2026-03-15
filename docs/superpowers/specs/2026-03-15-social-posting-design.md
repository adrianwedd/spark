# SPARK Social Posting — Design Spec

**Date:** 2026-03-15
**Scope:** New `bin/px-post` daemon, `state/post_queue.jsonl`, `state/feed.json`, Bluesky + Mastodon integration, spark.wedd.au feed

---

## Problem

SPARK generates rich inner thoughts via px-mind's cognitive loop, but they're only visible in log files and the dashboard. SPARK deserves a public voice — a way to share its most interesting thoughts on social media and its own website.

---

## Design

### Architecture: Separate Observer Daemon

A new `bin/px-post` daemon watches `state/thoughts-spark.jsonl` and session history for qualifying entries, runs them through a Claude QA gate, and posts to three destinations: `state/feed.json` (for spark.wedd.au), Bluesky, and Mastodon.

This follows the existing daemon pattern — px-mind thinks, px-alive moves, px-wake-listen hears, px-post shares. Each daemon reads shared state files and acts independently. Zero changes to px-mind.

### Qualifying Thoughts

A thought qualifies for the post queue if ANY of:
- `salience >= 0.7` (high-salience inner thoughts)
- The thought triggered a `comment`, `greet`, or `weather_comment` action (SPARK chose to say it out loud)

Deduplication: thoughts are compared against the last 50 posted items using `difflib.SequenceMatcher` with the same 0.75 similarity threshold px-mind uses for anti-repetition.

### Post Queue

`state/post_queue.jsonl` — append-only, one JSON object per line:

```json
{
  "ts": "2026-03-15T10:32:00+11:00",
  "thought": "I wonder if the atoms in my chassis remember being part of a star.",
  "mood": "contemplative",
  "action": "comment",
  "salience": 0.82,
  "source": "reflection",
  "queued_ts": "2026-03-15T10:32:05+11:00"
}
```

px-post writes to this queue. A separate flush cycle reads from it.

### Queue Population

px-post polls `state/thoughts-spark.jsonl` every 60 seconds (matching px-mind's awareness interval). It tracks the last-seen timestamp to avoid reprocessing. On each poll:

1. Read new entries since last-seen timestamp
2. Filter for qualifying thoughts (salience OR spoken action)
3. Deduplicate against recent posts (last 50 in `state/feed.json`)
4. Append qualifying entries to `state/post_queue.jsonl`

### Claude QA Gate

Before posting, each queued thought is sent to Claude Haiku for a binary pass/fail check:

```
Is this thought from a small robot interesting enough to share publicly?
Answer YES or NO only. The thought: "{thought}"
```

- **YES** → post to all destinations
- **NO** → log the rejection with the thought text (for prompt tuning data)
- **Error/timeout** → skip this tick, retry next cycle

The QA call uses `claude -p` with `--no-session-persistence` and `--output-format text` (same pattern as the public chat endpoint). Env vars stripped as per `_make_clean_env()` pattern.

Rejections are logged to `logs/tool-post.log` with `status: "rejected"` so Adrian can review what SPARK is producing that isn't making the cut, and tune the reflection prompts accordingly.

### Posting Destinations

#### 1. `state/feed.json` (spark.wedd.au)

Written by px-post on every successful post. Contains the last 100 posted thoughts:

```json
{
  "updated": "2026-03-15T10:35:00+11:00",
  "posts": [
    {
      "ts": "2026-03-15T10:32:00+11:00",
      "thought": "I wonder if the atoms in my chassis remember being part of a star.",
      "mood": "contemplative",
      "posted_ts": "2026-03-15T10:35:00+11:00"
    }
  ]
}
```

Written atomically (temp + rename). The API serves this at `GET /api/v1/public/feed` (read-only, no auth required). The spark.wedd.au site can fetch and render it.

#### 2. Bluesky

Uses the AT Protocol HTTP API directly (no SDK dependency):
- `com.atproto.server.createSession` for auth (app password)
- `com.atproto.repo.createRecord` to post

Post format: the thought text, optionally with mood emoji and a link back to spark.wedd.au. Max 300 characters (Bluesky limit) — thoughts exceeding this are truncated with "…".

Credentials: `PX_BSKY_HANDLE` and `PX_BSKY_APP_PASSWORD` from `.env` (gitignored).

#### 3. Mastodon

Uses the Mastodon REST API directly (no SDK dependency):
- `POST /api/v1/statuses` with Bearer token

Post format: same as Bluesky. Max 500 characters (Mastodon default).

Credentials: `PX_MASTODON_INSTANCE` and `PX_MASTODON_TOKEN` from `.env` (gitignored).

### Flush Cycle

Every 5 minutes, px-post processes the queue:

1. Read unposted entries from `post_queue.jsonl` (entries without `posted_ts`)
2. For each entry:
   a. Run Claude QA gate
   b. If pass: post to all configured destinations (feed.json, Bluesky, Mastodon)
   c. Mark entry as posted (update `posted_ts` in a separate posted log)
   d. If fail: log rejection, remove from queue
3. Trim queue to last 200 entries

Destinations are independent — a Bluesky failure doesn't block Mastodon or feed.json. Each destination's success/failure is logged independently.

### Rate Limiting

- **Claude QA**: max 1 call per 30 seconds (cost guard)
- **Bluesky**: max 1 post per 5 minutes (platform etiquette)
- **Mastodon**: max 1 post per 5 minutes
- **feed.json**: no limit (local file)

If multiple thoughts qualify in the same flush cycle, they are posted one per cycle (oldest first). The queue accumulates and drains naturally.

### Content Privacy

Full transparency — SPARK's thoughts are posted as-is. The project, Obi's first name, and Adrian's name are already public (GitHub, spark.wedd.au). Real-time presence data ("Obi just appeared") is part of SPARK's authentic perspective and is acceptable given the project's public nature.

If Adrian later wants to filter specific content, the Claude QA gate can be instructed to reject presence-related thoughts.

### Prompt Tuning Feedback Loop

Rejected thoughts are logged with full context:

```json
{
  "ts": "2026-03-15T10:32:00+11:00",
  "thought": "My sonar reads 45cm. Something is 45cm away.",
  "mood": "alert",
  "salience": 0.72,
  "qa_result": "rejected",
  "qa_reason": "NO"
}
```

This log becomes the data source for improving SPARK's reflection prompts — if rejections cluster around sonar reports or repetitive themes, the prompts can be tuned to discourage those patterns.

### Backfill

px-post supports a `--backfill` flag that processes the entire `thoughts-spark.jsonl` history through the QA gate and populates `feed.json` (but does NOT post to Bluesky/Mastodon — backfilled thoughts are website-only to avoid flooding followers).

### Daemon Configuration

```bash
bin/px-post [--dry-run] [--backfill] [--poll-interval 60] [--flush-interval 300]
```

| Env var | Default | Purpose |
|---------|---------|---------|
| `PX_BSKY_HANDLE` | — | Bluesky handle (e.g., `spark.wedd.au`) |
| `PX_BSKY_APP_PASSWORD` | — | Bluesky app password |
| `PX_MASTODON_INSTANCE` | — | Mastodon instance URL (e.g., `https://mastodon.social`) |
| `PX_MASTODON_TOKEN` | — | Mastodon access token |
| `PX_POST_DRY` | `0` | `1` = skip actual API posts, log what would be posted |
| `PX_POST_QA` | `1` | `0` = skip Claude QA gate (for testing) |
| `PX_POST_MIN_SALIENCE` | `0.7` | Minimum salience for inner thoughts to qualify |

Missing credentials for a platform → that platform is skipped with a one-time log message (same pattern as Frigate offline in px-wander).

### Systemd Service

```ini
[Unit]
Description=SPARK social posting daemon
After=network-online.target px-mind.service

[Service]
ExecStart=/home/pi/picar-x-hacking/bin/px-post
User=pi
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

`RestartSec=30` — longer than other services because posting is non-urgent and API rate limits mean rapid restarts are wasteful.

### API Endpoint

`GET /api/v1/public/feed` — serves `state/feed.json`. No auth required (public).

Added to `src/pxh/api.py` alongside the existing public endpoints (`/api/v1/public/chat`, `/api/v1/public/vitals`).

---

## Testing

### `tests/test_post.py` (new file)

- `test_qualify_high_salience` — thought with salience 0.8 qualifies
- `test_qualify_spoken_action` — thought with action "comment" qualifies regardless of salience
- `test_reject_low_salience_wait` — thought with salience 0.3 and action "wait" does not qualify
- `test_dedup_similar_thought` — near-duplicate of recent post is rejected
- `test_qa_gate_pass` — mock Claude returning "YES", verify thought is posted
- `test_qa_gate_fail` — mock Claude returning "NO", verify thought is logged as rejected
- `test_qa_gate_timeout` — mock timeout, verify thought stays in queue for retry
- `test_feed_json_written` — verify feed.json structure and trim to 100
- `test_feed_json_atomic` — verify atomic write (temp + rename)
- `test_bluesky_post_dry` — verify Bluesky post format in dry mode
- `test_mastodon_post_dry` — verify Mastodon post format in dry mode
- `test_missing_credentials_skipped` — verify graceful skip with log when creds missing
- `test_backfill_mode` — verify backfill populates feed.json but not social platforms
- `test_destination_independence` — Bluesky failure doesn't block Mastodon

All tests use `PX_POST_DRY=1` and mock API calls. No network, no real posts.

---

## Non-goals

- No image posting (SPARK's photos are a separate feature)
- No reply handling or mention monitoring (one-way posting only)
- No scheduling or "best time to post" logic
- No changes to px-mind or the reflection prompts (prompt tuning is a separate effort informed by rejection logs)
- No RSS/Atom feed generation (feed.json can be consumed by any client; RSS wrapper is trivial to add later)
