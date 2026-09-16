"""Runtime paths the code declares must be ignored by git.

`state/` is the robot's own record of what happened, and `.gitignore` lists its
runtime files one by one. That enumeration is a load-bearing list nobody
re-reads: #317 Phase 3 renamed the model-call log from
`state/claude_sessions.jsonl` to `state/model_sessions.jsonl` and updated the
writer, the reader, the docs and the tests — and not `.gitignore`. The live log
then sat as an untracked file in the robot's checkout, one `git add -A` away
from being committed, which is exactly how `state/nonexistent.jsonl` reached
`master` earlier the same evening.

So this asserts the property directly, for the paths the modules themselves
declare: if the code says "I write here", git must not want it.

`tools/` and the test suite read the repo's real `.gitignore`, so this is a
check on the repository rather than on a copy of it.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _declared_runtime_paths() -> dict[str, str]:
    """path (relative to the repo) -> what declares it."""
    out: dict[str, str] = {}

    from pxh import m5, model_session

    out[str(model_session.SESSION_LOG.relative_to(model_session.PROJECT_ROOT))] = \
        "model_session.SESSION_LOG — the model-call log"
    out[str(m5.m5_lock_path().relative_to(REPO_ROOT))] = \
        "m5.m5_lock_path() — the tier's single-flight lock"
    out[str(m5._circuit_path().relative_to(REPO_ROOT))] = \
        "m5._circuit_path() — the tier's shared circuit"
    out[str(m5._meter_path().relative_to(REPO_ROOT))] = \
        "m5._meter_path() — the tier's request meter"
    return out


def _is_ignored(rel: str) -> bool:
    result = subprocess.run(["git", "check-ignore", "-q", rel],
                            cwd=REPO_ROOT, capture_output=True)
    return result.returncode == 0


@pytest.mark.parametrize("rel,declared_by", sorted(_declared_runtime_paths().items()))
def test_a_declared_runtime_path_is_gitignored(rel, declared_by):
    assert _is_ignored(rel), (
        f"{rel} is written by {declared_by}, and .gitignore does not cover it.\n"
        f"An untracked runtime record in the robot's checkout is one `git add -A` "
        f"from being committed as evidence. Add it to .gitignore."
    )


def test_the_historical_log_stays_ignored():
    """The old name holds the same records. Keeping it ignored is provenance,
    not nostalgia — the file is still on the robot and still not ours to
    commit."""
    assert _is_ignored("state/claude_sessions.jsonl")
