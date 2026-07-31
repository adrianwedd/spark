"""Guard: exec-loaded daemon namespaces must never resolve to live state.

`tests/test_post.py` and `tests/test_alive_frigate.py` exec the Python heredoc
out of `bin/px-post` / `bin/px-alive` into a namespace dict at *import* time.
That freezes the daemon's module-level path constants — `QUEUE_FILE`,
`FEED_FILE`, `CURSOR_FILE`, `STATUS_FILE` — for the whole pytest session,
because they are computed once from `PX_STATE_DIR` at exec time and never
re-read the environment afterwards.

Both loaders used to bind `PX_STATE_DIR` and `LOG_DIR` to the real
`PROJECT_ROOT/state` and `PROJECT_ROOT/logs`. On this repo that is a running
robot's live state. Nothing was corrupted only because each individual test
happened to reassign the specific constant it wrote through
(`_POST["FEED_FILE"] = tmp / "feed.json"`, and so on). A new test that wrote
through an unpatched constant would have overwritten the live `feed.json` or
`post_queue.jsonl` with fixture data, and would have done it silently.

These tests assert the property rather than the code shape: whatever the
loader does, the constants it produces must land outside the repository. That
survives refactors of the loaders themselves.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LIVE_STATE = ROOT / "state"
LIVE_LOGS = ROOT / "logs"

# (module, namespace attribute, constants that must not point at live state)
#
# Bare module names, matching how pytest imports these files (tests/ has no
# __init__.py, so pytest puts the directory itself on sys.path). Importing them
# as `tests.test_post` would create a *second* module object and exec the
# daemon heredoc a second time.
LOADERS = [
    ("test_post", "_POST",
     ["STATE_DIR", "LOG_DIR", "QUEUE_FILE", "FEED_FILE", "CURSOR_FILE", "STATUS_FILE"]),
    ("test_alive_frigate", "_ALIVE", ["STATE_DIR"]),
]


def _load(module_name: str, attr: str) -> dict:
    import importlib
    mod = importlib.import_module(module_name)
    return getattr(mod, attr)


@pytest.mark.parametrize("module_name,attr,constants", LOADERS)
def test_loaded_daemon_paths_are_outside_the_repo(module_name, attr, constants):
    """No frozen path constant may resolve into the live state/ or logs/ tree."""
    ns = _load(module_name, attr)
    for name in constants:
        assert name in ns, f"{module_name}:{attr} has no {name} — loader drifted"
        path = Path(ns[name]).resolve()
        for live in (LIVE_STATE, LIVE_LOGS):
            assert not path.is_relative_to(live), (
                f"{module_name}:{attr}[{name}] resolves to {path}, inside the live "
                f"{live}. An unmocked write in any test would corrupt the running "
                f"robot's state. Bind PX_STATE_DIR/LOG_DIR to a temp dir in the loader."
            )


@pytest.mark.parametrize("module_name,attr,constants", LOADERS)
def test_loaded_daemon_paths_are_writable_sandboxes(module_name, attr, constants):
    """The redirect must point somewhere real, not merely somewhere else.

    A loader that pointed the constants at a nonexistent path would satisfy the
    test above while making every unmocked write raise instead of pass — which
    is safe but useless. The sandbox has to be a directory that exists.
    """
    ns = _load(module_name, attr)
    state_dir = Path(ns["STATE_DIR"]).resolve()
    assert state_dir.is_dir(), f"{module_name}:{attr}[STATE_DIR] = {state_dir} does not exist"
