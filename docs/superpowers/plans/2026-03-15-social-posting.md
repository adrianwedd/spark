# SPARK Social Posting Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give SPARK a public voice — post qualifying thoughts to spark.wedd.au feed, Bluesky, and Mastodon via a new `px-post` daemon.

**Architecture:** Separate observer daemon (`bin/px-post`) watches `thoughts-spark.jsonl`, filters by salience/action, runs a Claude QA gate, posts to three independent destinations. Queue-based with per-destination retry. Single-threaded, flock-guarded.

**Tech Stack:** Python 3.11, AT Protocol (Bluesky), Mastodon REST API, Claude CLI, JSONL state files, systemd

**Spec:** `docs/superpowers/specs/2026-03-15-social-posting-design.md`

---

## File Structure

| File | Responsibility |
|------|---------------|
| `bin/px-post` | Daemon script (bash + Python heredoc, same pattern as px-mind) |
| `src/pxh/api.py` | Add `GET /api/v1/public/feed` endpoint |
| `tests/test_post.py` | All 38 tests for the posting pipeline |
| `systemd/px-post.service` | Systemd unit file |
| `state/post_queue.jsonl` | Queue (runtime, gitignored) |
| `state/feed.json` | Public feed (runtime, gitignored) |
| `state/px-post-cursor.json` | File offset cursor (runtime, gitignored) |
| `state/px-post-status.json` | Health status (runtime, gitignored) |
| `state/px-post.lock` | flock file (runtime, gitignored) |

---

## Chunk 1: Core Pipeline (qualify, dedup, cursor, QA gate)

### Task 1: Thought Qualification and Deduplication

**Files:**
- Create: `tests/test_post.py`
- Create: `bin/px-post` (initial scaffold)

- [ ] **Step 1: Write qualifying tests**

  In `tests/test_post.py`, use the `exec()` + heredoc extraction pattern from `tests/test_mind_utils.py` to import functions from `bin/px-post`. Write tests:
  - `test_qualify_high_salience` — `{"thought": "x", "salience": 0.8, "action": "wait"}` qualifies
  - `test_qualify_spoken_action` — `{"thought": "x", "salience": 0.3, "action": "comment"}` qualifies
  - `test_reject_low_salience_wait` — `{"thought": "x", "salience": 0.3, "action": "wait"}` rejected
  - `test_suppressed_expression_qualifies` — action "comment" qualifies regardless of expression suppression
  - `test_dedup_similar_thought` — thought 75%+ similar to recent post is rejected
  - `test_malformed_thought_entry` — `{"random": "data"}` missing required fields is skipped

- [ ] **Step 2: Run tests — expect FAIL** (bin/px-post doesn't exist yet)

  Run: `python -m pytest tests/test_post.py -v -k "qualify or dedup or malformed"`

- [ ] **Step 3: Create `bin/px-post` with qualification logic**

  Create `bin/px-post` with bash wrapper sourcing `px-env`, then Python heredoc. Define:
  - `MIN_SALIENCE`, `SPOKEN_ACTIONS`, `SIMILARITY_THRESHOLD`, `REQUIRED_FIELDS` constants
  - `qualifies(entry: dict) -> bool` — checks salience >= threshold OR action in spoken set
  - `is_duplicate(thought: str, recent_posts: list[str]) -> bool` — SequenceMatcher ratio >= 0.75
  - Both check `REQUIRED_FIELDS` presence

- [ ] **Step 4: Run tests — expect PASS**

  Run: `python -m pytest tests/test_post.py -v -k "qualify or dedup or malformed"`

- [ ] **Step 5: Commit**

  ```
  git add bin/px-post tests/test_post.py
  git commit -m "feat(px-post): thought qualification and deduplication logic"
  ```

---

### Task 2: File Offset Cursor

**Files:**
- Modify: `bin/px-post`
- Modify: `tests/test_post.py`

- [ ] **Step 1: Write cursor tests**

  - `test_file_offset_cursor` — write 3 lines, poll, cursor advances; add 2 more, poll returns only new
  - `test_file_shrink_resets_cursor` — file truncated -> cursor resets to 0
  - `test_cursor_inode_change` — file replaced (different inode) -> cursor resets
  - `test_cursor_corrupt_resets` — corrupt cursor JSON -> resets to offset 0
  - `test_partial_line_not_consumed` — incomplete line (no `\n`) left for next poll
  - `test_corrupt_jsonl_skipped` — corrupt JSON line between valid lines -> skipped, valid lines returned

- [ ] **Step 2: Run tests — expect FAIL**

  Run: `python -m pytest tests/test_post.py -v -k "cursor or partial or corrupt_jsonl"`

- [ ] **Step 3: Implement cursor logic**

  In `bin/px-post`, add:
  - `_load_cursor() -> dict` — reads `px-post-cursor.json`, returns `{"offset": 0, "inode": 0}` on error
  - `_save_cursor(offset, inode)` — atomic write, logs on failure
  - `poll_new_thoughts(thoughts_file) -> list[dict]` — checks inode/size, seeks to offset, reads complete lines only, parses per-line with JSONDecodeError handling, skips malformed entries, updates cursor

- [ ] **Step 4: Run tests — expect PASS**

  Run: `python -m pytest tests/test_post.py -v -k "cursor or partial or corrupt_jsonl"`

- [ ] **Step 5: Commit**

  ```
  git add bin/px-post tests/test_post.py
  git commit -m "feat(px-post): file offset cursor with inode tracking"
  ```

---

### Task 3: Claude QA Gate

**Files:**
- Modify: `bin/px-post`
- Modify: `tests/test_post.py`

- [ ] **Step 1: Write QA gate tests**

  - `test_qa_gate_pass` — mock Claude stdout "YES" -> returns "pass"
  - `test_qa_gate_pass_verbose` — mock "Yes, this is wonderful" -> returns "pass"
  - `test_qa_gate_fail` — mock "NO" -> returns "rejected"
  - `test_qa_gate_ambiguous` — mock "Maybe" -> returns "ambiguous"
  - `test_qa_gate_timeout` — mock TimeoutExpired -> returns None
  - `test_qa_response_whitespace` — mock "  YES\n" -> returns "pass" after strip

- [ ] **Step 2: Run tests — expect FAIL**

  Run: `python -m pytest tests/test_post.py -v -k "qa_gate"`

- [ ] **Step 3: Implement QA gate**

  `run_qa_gate(thought: str) -> str | None` — resolves claude binary (PX_CLAUDE_BIN / shutil.which / hardcoded fallback), strips Claude Code env vars, runs `claude -p` with the QA prompt, parses response: strip -> lowercase -> startswith("yes") = "pass", startswith("no") = "rejected", else = "ambiguous". Returns None on error/timeout.

- [ ] **Step 4: Run tests — expect PASS**

  Run: `python -m pytest tests/test_post.py -v -k "qa_gate"`

- [ ] **Step 5: Commit**

  ```
  git add bin/px-post tests/test_post.py
  git commit -m "feat(px-post): Claude QA gate with fuzzy response parsing"
  ```

---

## Chunk 2: Destinations, Queue, Daemon, API

### Task 4: Feed.json Writer and Truncation

**Files:**
- Modify: `bin/px-post`
- Modify: `tests/test_post.py`

- [ ] **Step 1: Write feed and truncation tests**

  - `test_feed_json_written` — verify structure: `{updated, posts: [{ts, thought, mood, posted_ts}]}`, trim to 100
  - `test_feed_json_atomic` — verify temp file + os.replace pattern
  - `test_truncation_word_boundary` — 350-char text truncated at last word boundary before 297 for Bluesky, before 497 for Mastodon, with "..." appended

- [ ] **Step 2: Run tests — expect FAIL**

- [ ] **Step 3: Implement feed writer and truncation**

  - `write_feed(thought: dict) -> bool` — reads existing feed.json, appends, trims to 100, atomic write
  - `truncate_at_word(text: str, max_len: int) -> str` — finds last space before max_len-1, appends "..."

- [ ] **Step 4: Run tests — expect PASS**

- [ ] **Step 5: Commit**

  ```
  git add bin/px-post tests/test_post.py
  git commit -m "feat(px-post): feed.json writer with atomic writes and word-boundary truncation"
  ```

---

### Task 5: Bluesky and Mastodon Clients

**Files:**
- Modify: `bin/px-post`
- Modify: `tests/test_post.py`

- [ ] **Step 1: Write destination tests**

  - `test_bluesky_post_dry` — verify post payload format in dry mode
  - `test_bluesky_reauth_on_401` — mock 401 then 200 on retry
  - `test_bluesky_disable_after_3_auth_failures` — 3 failures -> disabled flag set
  - `test_bluesky_400_credentials` — mock 400 from createSession -> auth failure logged
  - `test_mastodon_post_dry` — verify post payload format
  - `test_missing_credentials_skipped` — no PX_BSKY_HANDLE -> Bluesky skipped with one-time log
  - `test_destination_independence` — Bluesky error does not prevent Mastodon success

- [ ] **Step 2: Run tests — expect FAIL**

- [ ] **Step 3: Implement BlueskyClient class**

  - `available() -> bool` — checks creds + disabled flag
  - `_auth() -> bool` — createSession, caches tokens, handles 400/401, tracks auth_failures, disables after 3
  - `post(text: str) -> str` — returns "ok", "error:reason", or "skipped". Handles 429 with Retry-After.
  - Uses `urllib.request` directly (no SDK)

- [ ] **Step 4: Implement MastodonClient class**

  - Same interface: `available()`, `post(text)`. Simpler — Bearer token auth only, no refresh logic.

- [ ] **Step 5: Run tests — expect PASS**

- [ ] **Step 6: Commit**

  ```
  git add bin/px-post tests/test_post.py
  git commit -m "feat(px-post): Bluesky + Mastodon clients with auth lifecycle"
  ```

---

### Task 6: Queue Management and Flush Cycle

**Files:**
- Modify: `bin/px-post`
- Modify: `tests/test_post.py`

- [ ] **Step 1: Write queue tests**

  - `test_per_destination_retry` — entry with bluesky "error:timeout" retried, feed "ok" not re-attempted
  - `test_flush_max_one_per_cycle` — 3 queued entries, flush processes only 1
  - `test_repost_guard_after_corruption` — thought in feed.json marked posted for all destinations

- [ ] **Step 2: Run tests — expect FAIL**

- [ ] **Step 3: Implement flush cycle**

  `flush_queue(bluesky, mastodon, dry)` — reads queue, filters pending entries, runs QA if needed, posts to each destination independently, updates per-destination status, atomic rewrite, trim to 200. Checks feed.json before social posts (re-post guard).

- [ ] **Step 4: Run tests — expect PASS**

- [ ] **Step 5: Commit**

  ```
  git add bin/px-post tests/test_post.py
  git commit -m "feat(px-post): queue flush with per-destination retry and repost guard"
  ```

---

### Task 7: Daemon Loop, Health, Locking, Backfill

**Files:**
- Modify: `bin/px-post`
- Modify: `tests/test_post.py`

- [ ] **Step 1: Write daemon and backfill tests**

  - `test_single_instance_lock` — second instance exits with code 1
  - `test_health_status_written` — px-post-status.json updated each flush
  - `test_health_status_atomic` — temp + rename
  - `test_backfill_mode` — processes all thoughts, writes feed.json, skips social platforms
  - `test_backfill_idempotent` — running twice does not duplicate

- [ ] **Step 2: Run tests — expect FAIL**

- [ ] **Step 3: Implement daemon main**

  - flock guard (STATE_DIR mkdir, open lock file in append, fcntl.flock LOCK_EX|LOCK_NB)
  - SIGTERM handler (sets threading.Event)
  - Main loop: poll every poll_interval, flush every flush_interval, write health status
  - Backfill mode: process entire file, feed.json only, then exit
  - `write_health_status()` — atomic write of status JSON

- [ ] **Step 4: Run tests — expect PASS**

- [ ] **Step 5: Run full test suite**

  Run: `python -m pytest -q` (expect all pass including new tests)

- [ ] **Step 6: Commit**

  ```
  git add bin/px-post tests/test_post.py
  git commit -m "feat(px-post): daemon loop with flock, health status, and backfill mode"
  ```

---

### Task 8: API Endpoint and Systemd Service

**Files:**
- Modify: `src/pxh/api.py`
- Create: `systemd/px-post.service`

- [ ] **Step 1: Add feed endpoint to API**

  In `src/pxh/api.py`, near the other public endpoints (after `public_thoughts` at ~line 635):

  ```python
  @app.get("/api/v1/public/feed")
  async def public_feed():
      """SPARK's public thought feed. No auth required."""
      feed_path = _public_state_dir() / "feed.json"
      try:
          return json.loads(feed_path.read_text())
      except (FileNotFoundError, json.JSONDecodeError):
          return {"updated": None, "posts": []}
  ```

- [ ] **Step 2: Create systemd service file**

  Create `systemd/px-post.service` with the unit definition from the spec.

- [ ] **Step 3: Run full test suite**

  Run: `python -m pytest -q`

- [ ] **Step 4: Commit**

  ```
  git add src/pxh/api.py systemd/px-post.service
  git commit -m "feat(px-post): API feed endpoint + systemd service"
  ```
