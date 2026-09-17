"""Capability health: a daemon that is alive but cannot do one named thing.

#332's three stale units each kept their *process* axis green — the blog poll
loop, the evolve queue, the API request loop all kept proving their daemons were
running — while a capability inside each was gone. The historical instance was
`pxh.claude_session` becoming `pxh.model_session`: the compiled statement in each
already-running process still named the old module, so a blog post, an evolution
request and a budget read each failed as `ImportError`, and `read_health()` said
`ok` throughout.

These tests pin the axis, not the incident: a capability block must be visible,
must not be cleared by the daemon merely reporting success, must survive a
restart, and must not be confused with a dying process.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pxh import health


def test_blocked_capability_reads_degraded_and_names_itself():
    health.record_success("px-fake-daemon")
    health.record_capability_failure(
        "px-fake-daemon", "generation", "cannot import pxh.model_session: ModuleNotFoundError(...)"
    )

    entry = health.read_health(components=("px-fake-daemon",))["components"]["px-fake-daemon"]
    assert entry["status"] == "degraded"
    assert "generation" in entry["capabilities"]
    assert "pxh.model_session" in entry["capabilities"]["generation"]["error"]


def test_daemon_success_does_not_clear_a_capability_block():
    """The poll loop proving the process is alive is what hid this class."""
    health.record_capability_failure("px-fake-daemon", "generation", "dependency gone")
    health.record_success("px-fake-daemon", detail={"generated": 0})

    entry = health.read_health(components=("px-fake-daemon",))["components"]["px-fake-daemon"]
    assert entry["status"] == "degraded"
    assert "generation" in entry["capabilities"]


def test_capability_success_clears_the_block():
    health.record_capability_failure("px-fake-daemon", "generation", "dependency gone")
    health.record_capability_success("px-fake-daemon", "generation")

    entry = health.read_health(components=("px-fake-daemon",))["components"]["px-fake-daemon"]
    assert entry["status"] == "ok"
    assert "capabilities" not in entry


def test_a_restart_does_not_clear_a_block(tmp_path, monkeypatch):
    """The record is on disk, so the block outlives the process that wrote it."""
    health.record_capability_failure("px-fake-daemon", "generation", "dependency gone")
    on_disk = json.loads((health.health_dir() / "px-fake-daemon.json").read_text())

    # A new process starts with empty in-process state and reads what is left.
    health._last_success_write.clear()
    assert on_disk["capabilities"]["generation"]["error"] == "dependency gone"
    assert health.read_health(components=("px-fake-daemon",))["components"]["px-fake-daemon"]["status"] == "degraded"


def test_summarize_names_the_capability_rather_than_a_failure_streak():
    health.record_success("px-fake-daemon")
    health.record_capability_failure("px-fake-daemon", "model-budget", "cannot read budget")

    line = health.summarize(health.read_health(components=("px-fake-daemon",)))
    assert "capability model-budget unavailable" in line
    assert "0 failures" not in line


def test_a_block_does_not_outrank_a_failing_process():
    """'This daemon is dying' is bigger news than 'one boundary is blocked'."""
    for _ in range(health.FAIL_THRESHOLD):
        health.record_failure("px-fake-daemon", "tick blew up")
    health.record_capability_failure("px-fake-daemon", "generation", "dependency gone")

    entry = health.read_health(components=("px-fake-daemon",))["components"]["px-fake-daemon"]
    assert entry["status"] == "failing"


def test_capability_success_is_a_no_op_when_nothing_was_blocked():
    health.record_success("px-fake-daemon")
    before = (health.health_dir() / "px-fake-daemon.json").read_text()

    health.record_capability_success("px-fake-daemon", "generation")

    assert (health.health_dir() / "px-fake-daemon.json").read_text() == before


# ---------------------------------------------------------------------------
# px-evolve: the conditional boundary
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
_EVOLVE_SCRIPT = ROOT / "bin" / "px-evolve"


def _load_evolve_module(tmp_path, monkeypatch):
    """Load bin/px-evolve's heredoc body as a namespace (same trick as test_blog)."""
    script = _EVOLVE_SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"<<'PY'\n(.*?)^PY$", script, re.DOTALL | re.MULTILINE)
    assert match, "could not find the PY heredoc in bin/px-evolve"

    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)

    monkeypatch.setenv("PX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LOG_DIR", str(log_dir))
    monkeypatch.setenv("PROJECT_ROOT", str(ROOT))

    ns = {"__file__": str(_EVOLVE_SCRIPT), "__name__": "px_evolve_mod"}
    exec(compile(match.group(1), str(_EVOLVE_SCRIPT), "exec"), ns)  # noqa: S102
    return ns, state_dir, log_dir


def test_a_dry_run_never_records_a_capability_failure(tmp_path, monkeypatch):
    """'This feature is deliberately switched off' is not a health problem.

    --dry-run returns before the import is ever attempted, so an operator
    checking the pipeline cannot turn the board red by exercising it.
    """
    ns, _state_dir, _log_dir = _load_evolve_module(tmp_path, monkeypatch)

    result = ns["_run_in_worktree"](
        "evolve-test-dry", "Add a thing", {}, "spark/evolve-test-dry", str(tmp_path / "wt"), dry=True
    )

    assert result == "failed:no_changes"
    assert not (health.health_dir() / "px-evolve.json").exists()


def test_a_requested_evolution_without_its_dependency_fails_closed_and_degrades_health(
    tmp_path, monkeypatch, block_module_import
):
    """The conditional case: a *request* lost its implementation dependency.

    Unlike px-blog, px-evolve has a deliberately-neutered direct path — its
    refusal stub. That is why the signal is scoped to "an evolution was asked
    for", not to "the module cannot be imported": an idle daemon with an empty
    queue must stay invisible, and a requested one that cannot run must not.
    """
    ns, _state_dir, _log_dir = _load_evolve_module(tmp_path, monkeypatch)

    block_module_import.block("pxh.model_session")
    result = ns["_run_in_worktree"](
        "evolve-test-1", "Add acoustic exploration angles", {}, "spark/evolve-test-1",
        str(tmp_path / "wt"), dry=False,
    )

    assert result == "failed:no_session_manager", "must fail closed, not improvise"
    entry = health.read_health(components=("px-evolve",))["components"]["px-evolve"]
    assert entry["status"] == "degraded"
    assert "evolution" in entry["capabilities"]
