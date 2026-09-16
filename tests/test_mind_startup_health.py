"""A daemon that dies on startup must not read `ok` in the health board (#315).

The health store's own docstring names the gap: absence is the one state it
cannot report. A component that never writes a record and a component that is
working both look like nothing, and the store resolves that in favour of
"nothing wrong" — which is right for a store whose writers are daemons, and
wrong for the one case where a daemon dies *before* it can write.

On 2026-09-16 that cost an hour of confusion: px-mind crash-looped 59 times on
a lock file it could not open, while `state/health/px-mind.json` read `ok` —
because the last successful write was from the *previous* run. The fatal line
went to the log and nowhere else.
"""
from __future__ import annotations

from pxh import health, mind


def test_a_fatal_startup_records_a_health_failure(monkeypatch):
    def _boom(_args):
        raise PermissionError(13, "Permission denied",
                              "/home/pi/picar-x-hacking/state/session.json.lock")

    recorded: list[tuple] = []
    monkeypatch.setattr(mind, "mind_loop", _boom)
    monkeypatch.setattr(mind.health_mod, "record_failure",
                        lambda component, error, **kw: recorded.append((component, error)))

    rc = mind.main([])

    assert rc == 1, "a fatal start must still exit non-zero"
    assert recorded, "the fatal was logged but never recorded as a health failure"
    component, error = recorded[0]
    assert component == "px-mind"
    assert "Permission denied" in error


def test_the_recorded_failure_is_what_the_board_reads(monkeypatch, tmp_path):
    """End to end through the store: the component reads `failing`, not `ok`."""
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("PX_HEALTH_DIR", str(tmp_path / "health"))

    monkeypatch.setattr(mind, "mind_loop",
                        lambda _args: (_ for _ in ()).throw(OSError("boom")))
    assert mind.main([]) == 1

    entry = health.read_health()["components"].get("px-mind")
    assert entry is not None, "no health record was written for a daemon that died"
    # One failure reads `degraded` on the store's ladder (ok -> degraded ->
    # failing); what matters is that it is no longer indistinguishable from a
    # daemon that is working.
    assert entry["status"] != "ok"
    assert entry["consecutive_failures"] >= 1
    assert "boom" in entry["last_error"]
