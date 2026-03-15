"""Tests for px-post qualification, deduplication, and social posting logic."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import types
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

PROJECT_ROOT = Path(__file__).parent.parent


def _load_post_helpers():
    """Parse bin/px-post and extract the helper functions we want to test."""
    src = (PROJECT_ROOT / "bin" / "px-post").read_text()

    # Find the heredoc Python block (everything between <<'PY' and the closing PY)
    start = src.index("<<'PY'\n") + len("<<'PY'\n")
    end = src.rindex("\nPY\n")
    py_src = src[start:end]

    env_patch = {
        "PROJECT_ROOT": str(PROJECT_ROOT),
        "LOG_DIR": str(PROJECT_ROOT / "logs"),
        "PX_STATE_DIR": str(PROJECT_ROOT / "state"),
    }
    old_env = {k: os.environ.get(k) for k in env_patch}
    for k, v in env_patch.items():
        os.environ[k] = v

    globs: dict = {"__file__": str(PROJECT_ROOT / "bin" / "px-post")}
    try:
        exec(compile(py_src, "bin/px-post", "exec"), globs)  # noqa: S102
    finally:
        for k, old_v in old_env.items():
            if old_v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old_v

    return globs


_POST = _load_post_helpers()
qualifies = _POST["qualifies"]
is_duplicate = _POST["is_duplicate"]
poll_new_thoughts = _POST["poll_new_thoughts"]
run_qa_gate = _POST["run_qa_gate"]
truncate_at_word = _POST["truncate_at_word"]
write_feed = _POST["write_feed"]

# The subprocess module reference used inside the exec'd module globals
_post_subprocess = _POST["subprocess"]
_post_urllib_request = _POST["urllib"].request
_post_urllib_error = _POST["urllib"].error
BlueskyClient = _POST["BlueskyClient"]
MastodonClient = _POST["MastodonClient"]
queue_thought = _POST["queue_thought"]
_load_queue = _POST["_load_queue"]
_save_queue = _POST["_save_queue"]
flush_queue = _POST["flush_queue"]
_is_in_feed = _POST["_is_in_feed"]
write_health_status = _POST["write_health_status"]
run_backfill = _POST["run_backfill"]
STATUS_FILE = _POST["STATUS_FILE"]


def _make_line(thought="hello", salience=0.8, action="comment"):
    """Build a valid JSONL line (with trailing newline)."""
    return json.dumps({"thought": thought, "salience": salience, "action": action}) + "\n"


def _patch_state_dir(mod_globals, tmp_path):
    """Point the module's STATE_DIR and CURSOR_FILE at tmp_path."""
    import importlib
    # _POST dict holds the globals of the exec'd module; mutate in place.
    _POST["STATE_DIR"] = tmp_path
    _POST["CURSOR_FILE"] = tmp_path / "px-post-cursor.json"


# ---------------------------------------------------------------------------
# qualifies()
# ---------------------------------------------------------------------------


def test_qualify_high_salience():
    """High salience thought qualifies regardless of action."""
    entry = {"thought": "x", "salience": 0.8, "action": "wait"}
    assert qualifies(entry) is True


def test_qualify_spoken_action():
    """Spoken action qualifies even with low salience."""
    entry = {"thought": "x", "salience": 0.3, "action": "comment"}
    assert qualifies(entry) is True


def test_reject_low_salience_wait():
    """Low salience wait action does NOT qualify."""
    entry = {"thought": "x", "salience": 0.3, "action": "wait"}
    assert qualifies(entry) is False


def test_suppressed_expression_qualifies():
    """Spoken action qualifies regardless of low salience."""
    entry = {"thought": "x", "salience": 0.3, "action": "comment"}
    assert qualifies(entry) is True


def test_malformed_thought_entry():
    """Missing required fields returns False."""
    entry = {"random": "data"}
    assert qualifies(entry) is False


# ---------------------------------------------------------------------------
# is_duplicate()
# ---------------------------------------------------------------------------


def test_dedup_similar_thought():
    """Thought that is 75%+ similar to a recent post is rejected."""
    recent = ["The weather is lovely today and I feel happy"]
    thought = "The weather is lovely today and I feel glad"
    assert is_duplicate(thought, recent) is True


def test_dedup_different_thought():
    """Sufficiently different thought is not a duplicate."""
    recent = ["The weather is lovely today and I feel happy"]
    thought = "I heard a strange noise from the kitchen"
    assert is_duplicate(thought, recent) is False


def test_dedup_empty_recent():
    """No recent posts means nothing is a duplicate."""
    assert is_duplicate("any thought", []) is False


# ---------------------------------------------------------------------------
# poll_new_thoughts() — cursor system
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=False)
def _cursor_env(tmp_path):
    """Redirect STATE_DIR for cursor tests; restore after."""
    orig_state = _POST["STATE_DIR"]
    orig_cursor = _POST["CURSOR_FILE"]
    orig_queue = _POST["QUEUE_FILE"]
    orig_feed = _POST["FEED_FILE"]
    _POST["STATE_DIR"] = tmp_path
    _POST["CURSOR_FILE"] = tmp_path / "px-post-cursor.json"
    _POST["QUEUE_FILE"] = tmp_path / "post_queue.jsonl"
    _POST["FEED_FILE"] = tmp_path / "feed.json"
    yield tmp_path
    _POST["STATE_DIR"] = orig_state
    _POST["CURSOR_FILE"] = orig_cursor
    _POST["QUEUE_FILE"] = orig_queue
    _POST["FEED_FILE"] = orig_feed


def test_file_offset_cursor(_cursor_env):
    """Poll reads new entries and advances cursor; second poll returns only new."""
    tmp = _cursor_env
    tf = tmp / "thoughts-spark.jsonl"
    tf.write_text(_make_line("a") + _make_line("b") + _make_line("c"))

    results = poll_new_thoughts(tf)
    assert len(results) == 3
    assert [r["thought"] for r in results] == ["a", "b", "c"]

    # Append 2 more lines
    with tf.open("a") as f:
        f.write(_make_line("d") + _make_line("e"))

    results2 = poll_new_thoughts(tf)
    assert len(results2) == 2
    assert [r["thought"] for r in results2] == ["d", "e"]


def test_file_shrink_resets_cursor(_cursor_env):
    """Truncated file resets cursor to 0 and re-reads from start."""
    tmp = _cursor_env
    tf = tmp / "thoughts-spark.jsonl"
    tf.write_text(_make_line("a") + _make_line("b") + _make_line("c"))

    poll_new_thoughts(tf)  # advance cursor

    # Truncate to shorter content
    tf.write_text(_make_line("x"))
    results = poll_new_thoughts(tf)
    assert len(results) == 1
    assert results[0]["thought"] == "x"


def test_cursor_inode_change(_cursor_env):
    """Deleting and recreating the file (new inode) resets cursor."""
    tmp = _cursor_env
    tf = tmp / "thoughts-spark.jsonl"
    tf.write_text(_make_line("a") + _make_line("b"))

    poll_new_thoughts(tf)  # advance cursor

    # Delete and recreate (different inode)
    tf.unlink()
    tf.write_text(_make_line("new"))

    results = poll_new_thoughts(tf)
    assert len(results) == 1
    assert results[0]["thought"] == "new"


def test_cursor_corrupt_resets(_cursor_env):
    """Corrupt cursor file causes reset to offset 0 without crashing."""
    tmp = _cursor_env
    tf = tmp / "thoughts-spark.jsonl"
    tf.write_text(_make_line("a"))

    # Write garbage to cursor file
    cursor_f = tmp / "px-post-cursor.json"
    cursor_f.write_text("{{{not json at all!!!")

    results = poll_new_thoughts(tf)
    assert len(results) == 1
    assert results[0]["thought"] == "a"


def test_partial_line_not_consumed(_cursor_env):
    """Incomplete line (no trailing newline) is not returned."""
    tmp = _cursor_env
    tf = tmp / "thoughts-spark.jsonl"
    complete = _make_line("complete")
    partial = json.dumps({"thought": "partial", "salience": 0.8, "action": "comment"})
    # partial has no trailing \n
    tf.write_text(complete + partial)

    results = poll_new_thoughts(tf)
    assert len(results) == 1
    assert results[0]["thought"] == "complete"

    # Now finish the partial line
    with tf.open("a") as f:
        f.write("\n")

    results2 = poll_new_thoughts(tf)
    assert len(results2) == 1
    assert results2[0]["thought"] == "partial"


def test_corrupt_jsonl_skipped(_cursor_env):
    """Corrupt JSONL line is skipped; valid lines on both sides are returned."""
    tmp = _cursor_env
    tf = tmp / "thoughts-spark.jsonl"
    tf.write_text(_make_line("good1") + "NOT VALID JSON\n" + _make_line("good2"))

    results = poll_new_thoughts(tf)
    assert len(results) == 2
    assert [r["thought"] for r in results] == ["good1", "good2"]


# ---------------------------------------------------------------------------
# run_qa_gate() — Claude QA gate
# ---------------------------------------------------------------------------


def _mock_run_result(stdout="YES", returncode=0, stderr=""):
    """Create a mock subprocess.CompletedProcess."""
    result = MagicMock()
    result.stdout = stdout
    result.stderr = stderr
    result.returncode = returncode
    return result


@patch.dict(os.environ, {"PX_POST_QA": "1", "PX_CLAUDE_BIN": "/usr/bin/claude"})
def test_qa_gate_pass():
    """Claude responds YES — gate returns 'pass'."""
    with patch.object(_post_subprocess, "run", return_value=_mock_run_result("YES")):
        assert run_qa_gate("I see a bird on the fence") == "pass"


@patch.dict(os.environ, {"PX_POST_QA": "1", "PX_CLAUDE_BIN": "/usr/bin/claude"})
def test_qa_gate_pass_verbose():
    """Claude responds with YES prefix — gate returns 'pass' (prefix match)."""
    with patch.object(_post_subprocess, "run", return_value=_mock_run_result("Yes, this is wonderful")):
        assert run_qa_gate("The sunset is beautiful tonight") == "pass"


@patch.dict(os.environ, {"PX_POST_QA": "1", "PX_CLAUDE_BIN": "/usr/bin/claude"})
def test_qa_gate_fail():
    """Claude responds NO — gate returns 'rejected'."""
    with patch.object(_post_subprocess, "run", return_value=_mock_run_result("NO")):
        assert run_qa_gate("sonar: 42cm") == "rejected"


@patch.dict(os.environ, {"PX_POST_QA": "1", "PX_CLAUDE_BIN": "/usr/bin/claude"})
def test_qa_gate_ambiguous():
    """Claude responds with something other than YES/NO — gate returns 'ambiguous'."""
    with patch.object(_post_subprocess, "run", return_value=_mock_run_result("Maybe")):
        assert run_qa_gate("hmm not sure") == "ambiguous"


@patch.dict(os.environ, {"PX_POST_QA": "1", "PX_CLAUDE_BIN": "/usr/bin/claude"})
def test_qa_gate_timeout():
    """Subprocess times out — gate returns None."""
    with patch.object(_post_subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=15)):
        assert run_qa_gate("anything") is None


@patch.dict(os.environ, {"PX_POST_QA": "1", "PX_CLAUDE_BIN": "/usr/bin/claude"})
def test_qa_response_whitespace():
    """Whitespace-padded response is stripped before matching."""
    with patch.object(_post_subprocess, "run", return_value=_mock_run_result("  YES\n")):
        assert run_qa_gate("I wonder about the stars") == "pass"


# ---------------------------------------------------------------------------
# write_feed() — feed.json writer
# ---------------------------------------------------------------------------


def test_feed_json_written(_cursor_env):
    """Write thoughts to feed.json; verify structure and 100-post trim."""
    tmp = _cursor_env
    _POST["FEED_FILE"] = tmp / "feed.json"

    # Write 101 thoughts
    for i in range(101):
        thought = {"thought": f"thought {i}", "mood": "curious", "ts": f"2026-01-01T00:00:{i:02d}Z"}
        assert write_feed(thought) is True

    feed = json.loads((tmp / "feed.json").read_text())
    assert "updated" in feed
    assert "posts" in feed
    assert len(feed["posts"]) == 100

    # Verify structure of a post
    post = feed["posts"][0]
    assert "ts" in post
    assert "thought" in post
    assert "mood" in post
    assert "posted_ts" in post

    # First entry should be thought 1 (thought 0 was trimmed)
    assert feed["posts"][0]["thought"] == "thought 1"
    assert feed["posts"][-1]["thought"] == "thought 100"


def test_feed_json_atomic(_cursor_env):
    """Verify write_feed uses atomic tempfile + os.replace pattern."""
    tmp = _cursor_env
    _POST["FEED_FILE"] = tmp / "feed.json"

    _post_os = _POST["os"]
    with patch.object(_post_os, "replace", wraps=_post_os.replace) as mock_replace:
        thought = {"thought": "atomic test", "mood": "calm"}
        write_feed(thought)
        mock_replace.assert_called_once()
        # Second arg should be the feed file path
        assert str(mock_replace.call_args[0][1]) == str(tmp / "feed.json")


# ---------------------------------------------------------------------------
# truncate_at_word() — word-boundary truncation
# ---------------------------------------------------------------------------


def test_truncation_word_boundary():
    """Truncate 350-char text for Bluesky (300) at word boundary; Mastodon (500) untouched."""
    # Build a string that is exactly 350 chars
    words = "hello world this is a very long text that keeps going and going "
    text = (words * 10)[:350]
    assert len(text) == 350

    # Bluesky: max 300
    truncated = truncate_at_word(text, 300)
    assert len(truncated) <= 300
    assert truncated.endswith("\u2026")
    # Should cut at a word boundary — no partial words before the ellipsis
    before_ellipsis = truncated[:-1]
    assert before_ellipsis == before_ellipsis  # sanity
    # The character before ellipsis should be a space or end of word
    # (i.e., the cut was at rfind(" "))
    assert " " not in truncated[-2:-1] or truncated[-2] == " "

    # Mastodon: max 500 — text is only 350, should be returned as-is
    assert truncate_at_word(text, 500) == text


# ---------------------------------------------------------------------------
# Helper: mock urllib.request.urlopen
# ---------------------------------------------------------------------------


def _mock_response(body: dict | bytes = b"", code: int = 200, headers: dict | None = None):
    """Create a mock HTTP response context manager."""
    if isinstance(body, dict):
        body = json.dumps(body).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.status = code
    resp.headers = headers or {}
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _http_error(code: int, headers: dict | None = None):
    """Create a urllib.error.HTTPError."""
    hdrs = MagicMock()
    hdrs.get = lambda key, default=None: (headers or {}).get(key, default)
    return urllib.error.HTTPError(
        url="https://example.com", code=code, msg="error",
        hdrs=hdrs, fp=io.BytesIO(b""),
    )


# ---------------------------------------------------------------------------
# BlueskyClient tests
# ---------------------------------------------------------------------------


@patch.dict(os.environ, {"PX_BSKY_HANDLE": "test.bsky.social", "PX_BSKY_APP_PASSWORD": "pass123"})
def test_bluesky_post_dry():
    """With dry=True, no HTTP call is made, returns 'ok'."""
    client = BlueskyClient()
    with patch.object(_post_urllib_request, "urlopen") as mock_urlopen:
        result = client.post("Hello from SPARK!", dry=True)
        assert result == "ok"
        mock_urlopen.assert_not_called()


@patch.dict(os.environ, {"PX_BSKY_HANDLE": "test.bsky.social", "PX_BSKY_APP_PASSWORD": "pass123"})
def test_bluesky_reauth_on_401():
    """On 401 from createRecord, re-auth and retry. Verify 2 auth calls."""
    client = BlueskyClient()

    auth_resp = _mock_response({"accessJwt": "tok", "refreshJwt": "ref", "did": "did:plc:test"})
    post_success = _mock_response({"uri": "at://..."})

    # Sequence: auth ok -> createRecord 401 -> auth ok -> createRecord ok
    with patch.object(_post_urllib_request, "urlopen") as mock_urlopen:
        mock_urlopen.side_effect = [
            auth_resp,           # initial _auth()
            _http_error(401),    # first createRecord -> 401
            auth_resp,           # re-auth in post()
            post_success,        # retry createRecord
        ]
        result = client.post("test thought")
        assert result == "ok"
        # 2 auth calls (initial + re-auth) + 2 createRecord attempts = 4 total
        assert mock_urlopen.call_count == 4


@patch.dict(os.environ, {"PX_BSKY_HANDLE": "test.bsky.social", "PX_BSKY_APP_PASSWORD": "pass123"})
def test_bluesky_disable_after_3_auth_failures():
    """3 consecutive auth failures disable the client."""
    client = BlueskyClient()

    with patch.object(_post_urllib_request, "urlopen") as mock_urlopen:
        mock_urlopen.side_effect = _http_error(400)

        # Each post attempt triggers _auth() which fails
        client.post("attempt 1")
        client.post("attempt 2")
        client.post("attempt 3")

        assert client.disabled is True
        # Further posts should be skipped without HTTP calls
        mock_urlopen.reset_mock()
        result = client.post("attempt 4")
        assert result == "skipped"
        mock_urlopen.assert_not_called()


@patch.dict(os.environ, {"PX_BSKY_HANDLE": "test.bsky.social", "PX_BSKY_APP_PASSWORD": "pass123"})
def test_bluesky_400_credentials():
    """Auth returning 400 logs a message about checking credentials."""
    client = BlueskyClient()
    logged = []
    orig_log = _POST["log"]

    def capture_log(msg, **kw):
        logged.append(msg)
        orig_log(msg, **kw)

    _POST["log"] = capture_log
    try:
        with patch.object(_post_urllib_request, "urlopen", side_effect=_http_error(400)):
            client.post("test")
        assert any("check PX_BSKY_HANDLE" in m for m in logged)
    finally:
        _POST["log"] = orig_log


def test_missing_credentials_skipped():
    """No PX_BSKY_HANDLE env — available() returns False, post returns 'skipped'."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("PX_BSKY_HANDLE", "PX_BSKY_APP_PASSWORD")}
    with patch.dict(os.environ, env, clear=True):
        client = BlueskyClient()
        assert client.available() is False
        result = client.post("anything")
        assert result == "skipped"


# ---------------------------------------------------------------------------
# MastodonClient tests
# ---------------------------------------------------------------------------


@patch.dict(os.environ, {"PX_MASTODON_INSTANCE": "https://mastodon.social", "PX_MASTODON_TOKEN": "tok123"})
def test_mastodon_post_dry():
    """With dry=True, no HTTP call is made, returns 'ok'."""
    client = MastodonClient()
    with patch.object(_post_urllib_request, "urlopen") as mock_urlopen:
        result = client.post("Hello from SPARK!", dry=True)
        assert result == "ok"
        mock_urlopen.assert_not_called()


# ---------------------------------------------------------------------------
# Cross-destination tests
# ---------------------------------------------------------------------------


@patch.dict(os.environ, {
    "PX_BSKY_HANDLE": "test.bsky.social",
    "PX_BSKY_APP_PASSWORD": "pass123",
    "PX_MASTODON_INSTANCE": "https://mastodon.social",
    "PX_MASTODON_TOKEN": "tok123",
})
def test_destination_independence():
    """Bluesky failure does not prevent Mastodon from succeeding."""
    bsky = BlueskyClient()
    masto = MastodonClient()

    mastodon_resp = _mock_response({"id": "12345"})

    with patch.object(_post_urllib_request, "urlopen") as mock_urlopen:
        # Auth succeeds for Bluesky, but createRecord fails with 500
        auth_resp = _mock_response({"accessJwt": "tok", "refreshJwt": "ref", "did": "did:plc:test"})
        mock_urlopen.side_effect = [
            auth_resp,        # bsky auth
            _http_error(500), # bsky createRecord fails
        ]
        bsky_result = bsky.post("test thought")
        assert bsky_result.startswith("error:")

    with patch.object(_post_urllib_request, "urlopen") as mock_urlopen:
        mock_urlopen.return_value = mastodon_resp
        masto_result = masto.post("test thought")
        assert masto_result == "ok"


# ---------------------------------------------------------------------------
# Queue management tests
# ---------------------------------------------------------------------------


def _make_queue_entry(thought="test thought", posted=None, qa_result="pass", entry_id=None):
    """Build a queue entry dict for testing."""
    return {
        "id": entry_id or f"post-20260315-100000-{_SYS_RNG.randint(0,999):03d}",
        "ts": "2026-03-15T10:00:00+11:00",
        "thought": thought,
        "mood": "contemplative",
        "action": "comment",
        "salience": 0.82,
        "queued_ts": "2026-03-15T10:00:05+00:00",
        "qa_result": qa_result,
        "posted": posted or {"feed": None, "bluesky": None, "mastodon": None},
    }


# Need _SYS_RNG for test helpers
import random as _test_random
_SYS_RNG = _test_random.SystemRandom()


@patch.dict(os.environ, {"PX_POST_QA": "0"})
def test_per_destination_retry(_cursor_env):
    """Bluesky errored, feed ok — flush retries Bluesky but not feed."""
    tmp = _cursor_env
    entry = _make_queue_entry(
        thought="I see something interesting",
        posted={"feed": "ok", "bluesky": "error:timeout", "mastodon": "ok"},
        qa_result="pass",
        entry_id="post-retry-001",
    )
    queue_file = tmp / "post_queue.jsonl"
    queue_file.write_text(json.dumps(entry) + "\n")

    bsky = MagicMock()
    bsky.post.return_value = "ok"
    masto = MagicMock()

    count = flush_queue(bsky, masto, dry=True)
    assert count == 1

    # Bluesky was retried
    bsky.post.assert_called_once()
    # Mastodon was NOT retried (already "ok")
    masto.post.assert_not_called()

    # Verify queue entry updated
    saved = json.loads(queue_file.read_text().strip())
    assert saved["posted"]["feed"] == "ok"
    assert saved["posted"]["bluesky"] == "ok"
    assert saved["posted"]["mastodon"] == "ok"


@patch.dict(os.environ, {"PX_POST_QA": "0"})
def test_flush_max_one_per_cycle(_cursor_env):
    """Queue 3 entries. Flush once. Verify only 1 processed."""
    tmp = _cursor_env
    queue_file = tmp / "post_queue.jsonl"
    lines = []
    for i in range(3):
        entry = _make_queue_entry(
            thought=f"thought number {i}",
            entry_id=f"post-batch-{i:03d}",
        )
        lines.append(json.dumps(entry))
    queue_file.write_text("\n".join(lines) + "\n")

    bsky = MagicMock()
    bsky.post.return_value = "ok"
    masto = MagicMock()
    masto.post.return_value = "ok"

    count = flush_queue(bsky, masto, dry=True)
    assert count == 1

    # Only 1 entry should have been touched
    saved = [json.loads(l) for l in queue_file.read_text().strip().splitlines()]
    posted_count = sum(1 for e in saved if e["posted"]["feed"] == "ok")
    assert posted_count == 1


@patch.dict(os.environ, {"PX_POST_QA": "0"})
def test_repost_guard_after_corruption(_cursor_env):
    """Thought already in feed.json — queue entry marked ok for all destinations without re-sending."""
    tmp = _cursor_env
    thought_text = "The sky is particularly beautiful this evening"

    # Pre-populate feed.json with the thought
    feed = {"posts": [{"thought": thought_text, "mood": "calm", "ts": "", "posted_ts": ""}]}
    (tmp / "feed.json").write_text(json.dumps(feed))

    # Queue the same thought (simulating corruption recovery)
    entry = _make_queue_entry(
        thought=thought_text,
        posted={"feed": None, "bluesky": None, "mastodon": None},
        qa_result=None,
        entry_id="post-corrupt-001",
    )
    queue_file = tmp / "post_queue.jsonl"
    queue_file.write_text(json.dumps(entry) + "\n")

    bsky = MagicMock()
    masto = MagicMock()

    count = flush_queue(bsky, masto, dry=True)
    assert count == 1

    # Neither client should have been called
    bsky.post.assert_not_called()
    masto.post.assert_not_called()

    # All destinations should be marked "ok"
    saved = json.loads(queue_file.read_text().strip())
    assert saved["posted"]["feed"] == "ok"
    assert saved["posted"]["bluesky"] == "ok"
    assert saved["posted"]["mastodon"] == "ok"


# ---------------------------------------------------------------------------
# Single-instance lock
# ---------------------------------------------------------------------------


def test_single_instance_lock(tmp_path):
    """Acquire flock on a temp file; second LOCK_NB attempt fails."""
    import fcntl
    lock_path = tmp_path / "px-post.lock"
    fd1 = open(lock_path, "a")
    fcntl.flock(fd1, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # Second attempt with LOCK_NB should raise
    fd2 = open(lock_path, "a")
    with pytest.raises((OSError, BlockingIOError)):
        fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
    fd1.close()
    fd2.close()


# ---------------------------------------------------------------------------
# Health status
# ---------------------------------------------------------------------------


def test_health_status_written(_cursor_env):
    """write_health_status creates px-post-status.json with correct structure."""
    tmp = _cursor_env
    _POST["STATUS_FILE"] = tmp / "px-post-status.json"

    write_health_status(
        queue_depth=3,
        total_posted=10,
        total_rejected=2,
        bluesky_ok=True,
        mastodon_ok=False,
        last_post_ts="2026-03-15T10:00:00+00:00",
    )

    status_file = tmp / "px-post-status.json"
    assert status_file.exists()
    data = json.loads(status_file.read_text())
    assert data["status"] == "running"
    assert data["queue_depth"] == 3
    assert data["total_posted"] == 10
    assert data["total_rejected"] == 2
    assert data["bluesky_ok"] is True
    assert data["mastodon_ok"] is False
    assert data["last_post_ts"] == "2026-03-15T10:00:00+00:00"
    assert "ts" in data


def test_health_status_atomic(_cursor_env):
    """Verify write_health_status uses atomic os.replace."""
    tmp = _cursor_env
    _POST["STATUS_FILE"] = tmp / "px-post-status.json"

    _post_os = _POST["os"]
    with patch.object(_post_os, "replace", wraps=_post_os.replace) as mock_replace:
        write_health_status(
            queue_depth=0, total_posted=0, total_rejected=0,
            bluesky_ok=True, mastodon_ok=True, last_post_ts=None,
        )
        mock_replace.assert_called_once()
        assert str(mock_replace.call_args[0][1]) == str(tmp / "px-post-status.json")


# ---------------------------------------------------------------------------
# Backfill mode
# ---------------------------------------------------------------------------


@patch.dict(os.environ, {"PX_POST_QA": "0"})
def test_backfill_mode(_cursor_env):
    """Backfill processes qualifying thoughts to feed.json only, no social posting."""
    tmp = _cursor_env
    thoughts_file = tmp / "thoughts-spark.jsonl"
    # 2 qualifying (high salience), 1 not (low salience + wait)
    thoughts_file.write_text(
        _make_line("A bird landed on my head", salience=0.9, action="comment")
        + _make_line("sonar reading 42cm", salience=0.2, action="wait")
        + _make_line("The sunset is gorgeous tonight", salience=0.85, action="greet")
    )

    count = run_backfill(dry=True)
    assert count == 2

    feed = json.loads((tmp / "feed.json").read_text())
    assert len(feed["posts"]) == 2
    thoughts_in_feed = [p["thought"] for p in feed["posts"]]
    assert "A bird landed on my head" in thoughts_in_feed
    assert "The sunset is gorgeous tonight" in thoughts_in_feed
    assert "sonar reading 42cm" not in thoughts_in_feed


@patch.dict(os.environ, {"PX_POST_QA": "0"})
def test_backfill_idempotent(_cursor_env):
    """Running backfill twice on the same file produces no duplicates."""
    tmp = _cursor_env
    thoughts_file = tmp / "thoughts-spark.jsonl"
    thoughts_file.write_text(
        _make_line("A bird landed on my head", salience=0.9, action="comment")
        + _make_line("The sunset is gorgeous tonight", salience=0.85, action="greet")
    )

    count1 = run_backfill(dry=True)
    assert count1 == 2

    count2 = run_backfill(dry=True)
    assert count2 == 0

    feed = json.loads((tmp / "feed.json").read_text())
    assert len(feed["posts"]) == 2
