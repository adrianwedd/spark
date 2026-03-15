"""Tests for px-post qualification and deduplication logic."""
from __future__ import annotations

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
