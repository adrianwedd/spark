"""Regression tests for the retired Git-writing feed cron."""

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "bin" / "px-feed-snapshot"


def test_feed_snapshot_is_a_non_writing_compatibility_shim():
    before = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    result = subprocess.run(
        [str(SCRIPT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    after = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert result.stdout.startswith('{"status":"disabled"')
    assert before == after


def test_feed_snapshot_contains_no_git_mutation_commands():
    content = SCRIPT.read_text(encoding="utf-8")
    for command in ("git add", "git commit", "git pull", "git push"):
        assert command not in content
