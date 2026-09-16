"""Constitutional suite: runtime state and logs are not committed.

`state/` and `logs/` are the robot's own record of what happened. They are
live: daemons append to them while a checkout sits on top of them, and a test
that writes into them writes *production-shaped records into production state*,
which CLAUDE.md already names for logs — "a test that writes production-shaped
logs can falsify later forensics even if it never touches production state".

This test exists because it happened. `state/nonexistent.jsonl` was committed
to `master` in #326:

    a test pointed the model dispatcher's log at
    `PROJECT_ROOT / "state" / "nonexistent.jsonl"` — a name that reads as
    deliberately harmless, and is not, because the dispatcher *creates* the log
    it is pointed at — and a `git add -A` swept six real session records into
    the repository.

Three files are legitimately tracked here, and each is a template or a
placeholder rather than a record: the session template the setup instructions
copy from, the reflect-agent prompt that belongs to `state/spark-reflect/`, and
the `.gitkeep` that holds the log directory open. Anything else under either
tree is evidence, and evidence does not belong in the history of the code that
produced it.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Path -> why this one is a repository file rather than a record.
TRACKED_RUNTIME_ALLOWLIST = {
    "logs/.gitkeep": "holds the log directory open in a fresh clone",
    "state/session.template.json": "the template `cp state/session.template.json state/session.json` reads",
    "state/spark-reflect/CLAUDE.md": "the reflect agent's own instructions, which live under state/ by design",
}


def _tracked_runtime_files() -> set[str]:
    result = subprocess.run(
        ["git", "ls-files", "state", "logs"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    )
    return {line for line in result.stdout.splitlines() if line.strip()}


def test_only_allowlisted_runtime_files_are_tracked():
    """A committed record is indistinguishable from real evidence, later.

    Failing here means something that the robot or a test wrote has been added
    to the repository. Either it is genuinely a repository file — in which case
    add it to the allowlist with the reason — or it is a record, in which case
    it should be deleted and the writer taught not to write there.
    """
    tracked = _tracked_runtime_files()
    unexpected = sorted(tracked - set(TRACKED_RUNTIME_ALLOWLIST))
    assert unexpected == [], (
        "runtime state or logs were committed:\n  "
        + "\n  ".join(unexpected)
        + "\n\nThese are records, not source. If a test created one, fix the test "
          "to use tmp_path; if it is a real repository file, add it to "
          "TRACKED_RUNTIME_ALLOWLIST with the reason."
    )


@pytest.mark.parametrize("rel,why", sorted(TRACKED_RUNTIME_ALLOWLIST.items()))
def test_each_allowlisted_entry_still_exists(rel, why):
    """An allowlist entry for a file that was deleted is an exemption waiting
    for something else to move in."""
    assert (REPO_ROOT / rel).exists(), f"{rel} ({why}) is allowlisted but missing"
