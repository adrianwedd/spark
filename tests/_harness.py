"""Shared helpers for tests that exec a `bin/` daemon into a namespace dict.

Several test modules (`test_post.py`, `test_alive_frigate.py`) pull the Python
heredoc out of a bash daemon and `exec` it at *import* time, so the daemon's
module-level path constants are computed once and frozen for the whole pytest
session. Those constants derive from `PX_STATE_DIR` / `LOG_DIR`, so whatever
the environment says at exec time is where every unmocked write in that module
lands — for the rest of the run.

Binding them to the real project directories, as these loaders originally did,
points `QUEUE_FILE`, `FEED_FILE`, `CURSOR_FILE` and `STATUS_FILE` at a running
robot's live state. `daemon_load_env` exists so that binding happens in exactly
one place and can't be copy-pasted wrong again.

Underscore-prefixed so pytest does not collect it as a test module.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_SANDBOX: Path | None = None


def sandbox_root() -> Path:
    """A session-lifetime temp tree standing in for the live project dirs.

    Deliberately not `tmp_path`. The daemon namespaces are built at module
    import, before any fixture exists, and they persist across every test in
    the file — a function-scoped directory would be torn out from under the
    frozen constants after the first test.
    """
    global _SANDBOX
    if _SANDBOX is None:
        sandbox = Path(tempfile.mkdtemp(prefix="px-test-sandbox-"))
        (sandbox / "state").mkdir()
        (sandbox / "logs").mkdir()
        atexit.register(shutil.rmtree, sandbox, True)
        _SANDBOX = sandbox
    return _SANDBOX


@contextmanager
def daemon_load_env(**extra: str):
    """Bind the env a `bin/` daemon reads at exec time, then restore it.

    `PROJECT_ROOT` stays pointed at the real repository: daemons resolve
    sibling tools through it (`px-alive` builds `PROJECT_ROOT/bin/tool-voice`),
    so a fake root would break those lookups. Only the two variables that
    decide where the daemon *writes* are redirected.

    Yields the sandbox root so callers can assert against it.
    """
    sandbox = sandbox_root()
    patch = {
        "PROJECT_ROOT": str(ROOT),
        "LOG_DIR": str(sandbox / "logs"),
        "PX_STATE_DIR": str(sandbox / "state"),
        **extra,
    }
    old = {k: os.environ.get(k) for k in patch}
    os.environ.update(patch)
    try:
        yield sandbox
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
