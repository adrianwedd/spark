# px-post & px-mind Resilience Fixes

> **STATUS: IMPLEMENTED** — audit on 2026-07-11 verified all tasks complete and committed (Task 5 px-blog breaker added same day, commit 8bb8eb7f). Kept as historical record.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent SPARK from going silent when Claude CLI or Ollama is temporarily down, by adding a watchdog, QA circuit breaker, and better error logging.

**Architecture:** Three small, independent changes: (1) px-post gets a loop watchdog that self-exits on stall + a QA circuit breaker that stops hammering Claude when it's down, (2) px-post main loop gets defensive exception wrapping, (3) px-mind logs stdout on Claude failure for diagnostics.

**Tech Stack:** Python (embedded heredoc in bash scripts), pytest, unittest.mock

---

### Task 1: QA Gate Circuit Breaker in px-post

**Files:**
- Modify: `bin/px-post:318-360` (run_qa_gate function)
- Modify: `bin/px-post:736-797` (flush_queue — Pass 1 loop)
- Test: `tests/test_post.py`

The circuit breaker tracks consecutive QA failures. After 3 failures, it "opens" and skips QA calls for 5 minutes, returning `None` immediately. On success, the breaker resets.

- [ ] **Step 1: Write failing tests for circuit breaker**

Add to `tests/test_post.py`:

```python
@patch.dict(os.environ, {"PX_POST_QA": "1", "PX_CLAUDE_BIN": "/usr/bin/claude"})
def test_qa_circuit_breaker_opens_after_consecutive_failures():
    """After 3 consecutive QA failures, circuit breaker opens and skips calls."""
    fail_result = MagicMock(returncode=1, stderr="error", stdout="")
    with patch.object(_post_subprocess, "run", return_value=fail_result) as mock_run:
        # First 3 calls fail — these hit Claude
        for _ in range(3):
            assert run_qa_gate("test thought") is None
        assert mock_run.call_count == 3

        # 4th call should be skipped by circuit breaker (no subprocess call)
        assert run_qa_gate("another thought") is None
        assert mock_run.call_count == 3  # still 3 — breaker blocked the call


@patch.dict(os.environ, {"PX_POST_QA": "1", "PX_CLAUDE_BIN": "/usr/bin/claude"})
def test_qa_circuit_breaker_resets_on_success():
    """A successful QA call resets the circuit breaker."""
    fail_result = MagicMock(returncode=1, stderr="error", stdout="")
    pass_result = MagicMock(returncode=0, stderr="", stdout="YES")

    with patch.object(_post_subprocess, "run", return_value=fail_result):
        for _ in range(3):
            run_qa_gate("test")

    # Now simulate success — need to reset breaker
    with patch.object(_post_subprocess, "run", return_value=pass_result):
        # Manually reset the breaker for this test (simulating cooldown elapsed)
        _POST["_qa_breaker"]["failures"] = 0
        _POST["_qa_breaker"]["open_until"] = 0
        result = run_qa_gate("good thought")
        assert result == "pass"
        assert _POST["_qa_breaker"]["failures"] == 0


@patch.dict(os.environ, {"PX_POST_QA": "1", "PX_CLAUDE_BIN": "/usr/bin/claude"})
def test_qa_circuit_breaker_reopens_after_cooldown():
    """After cooldown expires, breaker closes and retries."""
    fail_result = MagicMock(returncode=1, stderr="error", stdout="")
    pass_result = MagicMock(returncode=0, stderr="", stdout="YES")

    with patch.object(_post_subprocess, "run", return_value=fail_result):
        for _ in range(3):
            run_qa_gate("test")

    # Simulate cooldown elapsed
    _POST["_qa_breaker"]["open_until"] = 0

    with patch.object(_post_subprocess, "run", return_value=pass_result) as mock_run:
        result = run_qa_gate("try again")
        assert result == "pass"
        assert mock_run.call_count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_post.py::test_qa_circuit_breaker_opens_after_consecutive_failures tests/test_post.py::test_qa_circuit_breaker_resets_on_success tests/test_post.py::test_qa_circuit_breaker_reopens_after_cooldown -v`
Expected: FAIL (KeyError on `_qa_breaker` — doesn't exist yet)

- [ ] **Step 3: Implement circuit breaker in run_qa_gate**

In `bin/px-post`, add the breaker state dict after the `QUEUE_LIMIT = 200` line (around line 658):

```python
# QA circuit breaker: skip Claude calls after repeated failures
_qa_breaker = {"failures": 0, "open_until": 0.0, "threshold": 3, "cooldown": 300}
```

Then modify `run_qa_gate` (line 318) to check and update the breaker:

```python
def run_qa_gate(thought: str) -> str | None:
    """Return 'pass', 'rejected', 'ambiguous', or None (error/timeout).

    Calls claude CLI with a binary YES/NO prompt. Response is stripped,
    lowercased, and prefix-matched.

    Circuit breaker: after `_qa_breaker['threshold']` consecutive failures,
    skip calls for `_qa_breaker['cooldown']` seconds.
    """
    if os.environ.get("PX_POST_QA", "1") == "0":
        return "pass"  # QA disabled for testing

    # Circuit breaker check
    now = time.monotonic()
    if _qa_breaker["failures"] >= _qa_breaker["threshold"]:
        if now < _qa_breaker["open_until"]:
            return None  # breaker open — skip
        # Cooldown elapsed — close breaker and retry
        log(f"qa breaker: cooldown elapsed, retrying (was {_qa_breaker['failures']} consecutive failures)")
        _qa_breaker["failures"] = 0

    claude_bin = (os.environ.get("PX_CLAUDE_BIN")
                  or shutil.which("claude")
                  or "/home/pi/.local/bin/claude")

    prompt = ('Is this thought from a small robot interesting enough to share '
              'publicly on social media?\n'
              'Answer only YES or NO. Nothing else.\n\n'
              f'The thought: "{thought}"')

    # Strip Claude Code env vars to allow nested invocation
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CLAUDE_CODE")
           and k not in ("CLAUDECODE", "DISABLE_CLAUDE_CODE_PROTECTIONS")}

    try:
        result = subprocess.run(
            [claude_bin, "-p", prompt,
             "--no-session-persistence",
             "--output-format", "text",
             "--allowedTools", ""],
            capture_output=True, text=True, timeout=15, env=env,
        )
        if result.returncode != 0:
            log(f"qa gate error: claude exit {result.returncode}: {result.stderr[:200]}")
            _qa_breaker["failures"] += 1
            if _qa_breaker["failures"] >= _qa_breaker["threshold"]:
                _qa_breaker["open_until"] = time.monotonic() + _qa_breaker["cooldown"]
                log(f"qa breaker: OPEN after {_qa_breaker['failures']} failures — skipping for {_qa_breaker['cooldown']}s")
            return None
        # Success — reset breaker
        _qa_breaker["failures"] = 0
        resp = result.stdout.strip().lower()
        if resp.startswith("yes"):
            return "pass"
        elif resp.startswith("no"):
            return "rejected"
        else:
            log(f"qa gate ambiguous response: {result.stdout.strip()[:100]}")
            return "ambiguous"
    except subprocess.TimeoutExpired:
        log("qa gate timeout (15s)")
        _qa_breaker["failures"] += 1
        if _qa_breaker["failures"] >= _qa_breaker["threshold"]:
            _qa_breaker["open_until"] = time.monotonic() + _qa_breaker["cooldown"]
            log(f"qa breaker: OPEN after {_qa_breaker['failures']} failures — skipping for {_qa_breaker['cooldown']}s")
        return None
    except Exception as exc:
        log(f"qa gate exception: {exc}")
        _qa_breaker["failures"] += 1
        if _qa_breaker["failures"] >= _qa_breaker["threshold"]:
            _qa_breaker["open_until"] = time.monotonic() + _qa_breaker["cooldown"]
            log(f"qa breaker: OPEN after {_qa_breaker['failures']} failures — skipping for {_qa_breaker['cooldown']}s")
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_post.py::test_qa_circuit_breaker_opens_after_consecutive_failures tests/test_post.py::test_qa_circuit_breaker_resets_on_success tests/test_post.py::test_qa_circuit_breaker_reopens_after_cooldown -v`
Expected: PASS

- [ ] **Step 5: Run full px-post test suite**

Run: `python -m pytest tests/test_post.py -v`
Expected: All existing tests still pass

- [ ] **Step 6: Commit**

```bash
git add bin/px-post tests/test_post.py
git commit -m "fix(px-post): add QA gate circuit breaker to avoid hammering Claude when down"
```

---

### Task 2: Loop Watchdog + Defensive Exception Wrapping in px-post

**Files:**
- Modify: `bin/px-post:990-1050` (main loop)
- Test: `tests/test_post.py`

Add a watchdog that tracks when the main loop last completed a full cycle. If it stalls for more than 10 minutes, self-exit (systemd `Restart=always` handles restart). Also wrap the non-flush parts of the loop body in a broad exception guard so unexpected errors don't kill the loop.

- [ ] **Step 1: Write failing test for watchdog**

Add to `tests/test_post.py`:

```python
def test_watchdog_stale_detection():
    """Watchdog helper detects stale loop."""
    _check_watchdog = _POST.get("_check_watchdog")
    assert _check_watchdog is not None, "_check_watchdog not defined"

    # Fresh timestamp — should not trigger
    assert _check_watchdog(time.monotonic(), 600) is False

    # Stale timestamp — should trigger
    assert _check_watchdog(time.monotonic() - 700, 600) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_post.py::test_watchdog_stale_detection -v`
Expected: FAIL (KeyError — `_check_watchdog` not defined)

- [ ] **Step 3: Implement watchdog and defensive wrapping**

In `bin/px-post`, add the watchdog helper near the circuit breaker definition (after `_qa_breaker`):

```python
WATCHDOG_STALE_S = 600  # 10 minutes — if main loop hasn't cycled, self-exit


def _check_watchdog(last_cycle: float, max_stale: float) -> bool:
    """Return True if the loop has been stale too long."""
    return (time.monotonic() - last_cycle) > max_stale
```

Then modify the main loop (starting at line 992):

```python
    poll_count = 0
    last_cycle = time.monotonic()
    try:
        while not _shutdown.is_set():
            poll_count += 1

            # Watchdog: if loop hasn't completed a cycle in WATCHDOG_STALE_S, exit
            if _check_watchdog(last_cycle, WATCHDOG_STALE_S):
                log(f"watchdog: loop stale for {time.monotonic() - last_cycle:.0f}s — exiting for systemd restart")
                break

            # Poll for new thoughts
            try:
                new_thoughts = poll_new_thoughts(thoughts_file)
            except Exception as exc:
                log(f"poll: CRASHED: {exc}")
                new_thoughts = []

            # Load feed for dedup
            feed_posts = []
            try:
                feed = json.loads(FEED_FILE.read_text()) if FEED_FILE.exists() else {}
                feed_posts = [p.get("thought", "") for p in feed.get("posts", [])]
            except (json.JSONDecodeError, OSError):
                pass

            try:
                queued_count = 0
                for entry in new_thoughts:
                    if qualifies(entry) and not is_duplicate(entry["thought"], feed_posts):
                        queue_thought(entry)
                        queued_count += 1
                if new_thoughts or queued_count:
                    log(f"poll: {len(new_thoughts)} new thoughts, {queued_count} queued")
            except Exception as exc:
                log(f"queue: CRASHED: {exc}")

            # Flush cycle
            now = time.monotonic()
            secs_until_flush = max(0, args.flush_interval - (now - last_flush))
            if poll_count % 5 == 0:  # log heartbeat every 5 polls (~5 min)
                log(f"heartbeat: poll={poll_count} flush_in={secs_until_flush:.0f}s queue={len(_load_queue())} bsky={bluesky.available()}")
            if now - last_flush >= args.flush_interval:
                log(f"flush: starting (interval={args.flush_interval}s, bsky_ok={bluesky.available()}, queue={len(_load_queue())})")
                try:
                    result = flush_queue(bluesky, dry)
                except Exception as exc:
                    log(f"flush: CRASHED: {exc}")
                    result = {"posted": False, "rejected": False, "processed": 0}
                log(f"flush: done processed={result.get('processed',0)} posted={result.get('posted')} rejected={result.get('rejected')}")
                if result.get("posted"):
                    total_posted += 1
                    last_post_ts = dt.datetime.now(dt.timezone.utc).isoformat()
                if result.get("rejected"):
                    total_rejected += 1

                try:
                    write_health_status(
                        queue_depth=len(_load_queue()),
                        total_posted=total_posted,
                        total_rejected=total_rejected,
                        bluesky_ok=bluesky.available(),
                        last_post_ts=last_post_ts,
                    )
                except Exception as exc:
                    log(f"health status: CRASHED: {exc}")
                last_flush = now

            last_cycle = time.monotonic()
            _shutdown.wait(timeout=args.poll_interval)
    finally:
        _safe_unlink_pid()
```

Key changes:
- `last_cycle` tracks when loop last completed a full iteration
- `_check_watchdog` breaks the loop if stale > 10 min
- `last_cycle = time.monotonic()` updated at the END of each cycle (before sleep)
- Queuing block wrapped in try/except
- `write_health_status` wrapped in try/except

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_post.py::test_watchdog_stale_detection -v`
Expected: PASS

- [ ] **Step 5: Run full px-post test suite**

Run: `python -m pytest tests/test_post.py -v`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
git add bin/px-post tests/test_post.py
git commit -m "fix(px-post): add loop watchdog and defensive exception wrapping"
```

---

### Task 3: Better Claude Failure Logging in px-mind

**Files:**
- Modify: `src/pxh/mind.py:2070-2072`
- Test: `tests/test_mind_coverage.py`

When Claude exits non-zero, the current error log only captures stderr — which was empty during the 12-hour outage. Include stdout too, since Claude CLI sometimes puts error info there.

- [ ] **Step 1: Write failing test**

Add to `tests/test_mind_coverage.py`:

```python
def test_call_claude_logs_stdout_on_failure():
    """When Claude exits non-zero, error includes both stdout and stderr."""
    import pxh.mind as mind

    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = "API rate limit exceeded"
    mock_result.stderr = ""

    with patch.object(mind.subprocess, "run", return_value=mock_result), \
         patch.object(mind.shutil, "which", return_value="/usr/bin/claude"):
        result = mind._call_claude_subprocess("system", "prompt")
        assert "rate limit" in result.get("error", "").lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mind_coverage.py::test_call_claude_logs_stdout_on_failure -v`
Expected: FAIL — error string contains only stderr (empty), not stdout

- [ ] **Step 3: Fix the error message to include stdout**

In `src/pxh/mind.py`, modify line 2070-2072:

Replace:
```python
    if result.returncode != 0:
        stderr = (result.stderr or "")[-200:]
        return {"error": f"claude exit {result.returncode}: {stderr}"}
```

With:
```python
    if result.returncode != 0:
        stderr = (result.stderr or "")[-200:]
        stdout = (result.stdout or "")[-200:]
        detail = stderr or stdout  # prefer stderr, fall back to stdout
        return {"error": f"claude exit {result.returncode}: {detail}"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mind_coverage.py::test_call_claude_logs_stdout_on_failure -v`
Expected: PASS

- [ ] **Step 5: Run full test suite**

Run: `python -m pytest tests/test_mind_coverage.py -v`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
git add src/pxh/mind.py tests/test_mind_coverage.py
git commit -m "fix(px-mind): log stdout on Claude failure for better diagnostics"
```

---

### Task 4: Restart px-post Service

- [ ] **Step 1: Run full test suite to verify nothing is broken**

Run: `python -m pytest tests/test_post.py tests/test_mind_coverage.py -v`
Expected: All tests pass

- [ ] **Step 2: Restart px-post to pick up changes**

```bash
sudo systemctl restart px-post
```

- [ ] **Step 3: Verify px-post is running and logging**

```bash
sleep 10 && tail -5 logs/tool-post.log
```

Expected: New log entries with `starting pid=...`

- [ ] **Step 4: Commit all changes if not already committed**

Final verification:
```bash
python -m pytest tests/ -v --timeout=30
```
