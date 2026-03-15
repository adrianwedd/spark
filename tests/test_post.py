"""Tests for px-post qualification and deduplication logic."""
from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

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
    _POST["STATE_DIR"] = tmp_path
    _POST["CURSOR_FILE"] = tmp_path / "px-post-cursor.json"
    yield tmp_path
    _POST["STATE_DIR"] = orig_state
    _POST["CURSOR_FILE"] = orig_cursor


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
