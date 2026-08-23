"""#219 Track B: allocator-retention fix and bounded startup contract for
px-wake-listen. Text-assertion style, mirroring tests/test_px_alive.py's
Type=notify tests — bin/px-wake-listen's Python body is embedded in a bash
heredoc (see px-wake-memprofile's header comment), so there is no module to
import; these tests read the source text directly instead.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = REPO_ROOT / "bin" / "px-wake-listen"
SERVICE = REPO_ROOT / "systemd" / "px-wake-listen.service"
DROP_IN = REPO_ROOT / "systemd" / "px-wake-listen.service.d" / "10-containment.conf"


def test_systemd_service_is_type_notify_with_notify_access():
    """Type=simple never gates model-load time on TimeoutStartSec — the unit
    is "started" at exec(). A live incident on 2026-08-23 sat wedged in
    SenseVoice loading for 47+ minutes under this gap with systemd unaware
    anything was wrong. NotifyAccess must be explicit: systemd's default
    (NotifyAccess=none) silently discards sd_notify() traffic, which would
    make every future start time out regardless of real readiness.
    """
    service = SERVICE.read_text()

    assert "\nType=notify\n" in service
    assert "NotifyAccess=main" in service
    assert "TimeoutStartSec=" in service


def test_launcher_signals_ready_only_after_backend_selection_or_duplicate_exit():
    """READY=1 must not fire before the daemon can actually do its job — that
    would re-arm systemd's readiness deadline over work it hasn't finished.
    The only two legitimate ready points are: STT backend selection completed
    (the daemon can transcribe), or the duplicate-instance guard's clean exit
    (systemd must not treat that exit as a failed start under Type=notify).
    """
    body = LAUNCHER.read_text()
    ready_calls = [
        line.strip() for line in body.splitlines()
        if line.strip().startswith("notify_ready(")
    ]

    assert len(ready_calls) == 2, ready_calls
    assert any("backend=" in call for call in ready_calls), ready_calls
    assert any("duplicate of pid" in call for call in ready_calls), ready_calls


def test_do_transcribe_releases_memory_on_every_path_before_its_single_return():
    """_release_transient_memory() must run after every completed cycle,
    including a full fall-through to Vosk — not just the sensevoice happy
    path. Pinning a single `return text` guards against a future edit adding
    an early return that skips the release call.
    """
    body = LAUNCHER.read_text()
    start = body.index("def _do_transcribe(")
    end = body.index("\ndef ", start + 1)
    fn_body = body[start:end]

    assert fn_body.count("\n    return text") == 1, (
        "_do_transcribe must have exactly one return point so memory release "
        "cannot be skipped by an early return"
    )
    release_idx = fn_body.index("_release_transient_memory(")
    return_idx = fn_body.index("\n    return text")
    assert release_idx < return_idx, (
        "_release_transient_memory() must run before _do_transcribe returns"
    )


def test_release_transient_memory_uses_gc_then_malloc_trim_without_unloading_model():
    """#219 Track B: gc.collect() alone only reaches Python-level references
    (measured: ~82M of ~582M retained); malloc_trim(0) is what returns
    freed-but-retained glibc arena memory to the OS. The resident STT models
    must not be unloaded — this is a per-cycle release, not a teardown.
    """
    body = LAUNCHER.read_text()
    start = body.index("def _release_transient_memory(")
    end = body.index("\ndef ", start + 1)
    fn_body = body[start:end]

    assert "gc.collect()" in fn_body
    assert "malloc_trim(0)" in fn_body
    assert "del model" not in fn_body
    assert "del sensevoice" not in fn_body


def test_containment_drop_in_headroom_covers_measured_load_peak():
    """MemoryHigh must clear the one-time model-load peak (707-730M PSS/RSS,
    measured via bin/px-wake-memprofile 2026-08-23), not just the lower
    steady-state floor malloc_trim recovers after the first completed cycle
    (~460M) — that peak happens once per process lifetime, before any trim
    call has run, and cannot be trimmed away.
    """
    drop_in = DROP_IN.read_text()
    lines = drop_in.splitlines()
    service_idx = lines.index("[Service]")

    def _value(key):
        for line in lines[service_idx:]:
            if line.startswith(f"{key}="):
                return int(line.split("=", 1)[1].rstrip("M")) * 1024 * 1024
        raise AssertionError(f"{key} not found")

    high = _value("MemoryHigh")
    maximum = _value("MemoryMax")
    measured_load_peak = 730 * 1024 * 1024

    assert high > measured_load_peak, (
        "MemoryHigh must clear the measured load-time peak or the throttle "
        "fires on ordinary startup, every time"
    )
    assert high < maximum
