"""Contracts for the pinned, contention-aware M5 SPARK model client."""
from __future__ import annotations

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest
from filelock import FileLock


def _response(text: str):
    inner = MagicMock()
    inner.read.return_value = json.dumps({"response": text}).encode()
    cm = MagicMock()
    cm.__enter__.return_value = inner
    cm.__exit__.return_value = False
    return cm


@pytest.fixture(autouse=True)
def _isolated_m5(monkeypatch, tmp_path):
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("PX_M5_SPARK_MODEL", "spark:fixed")
    from pxh import m5

    m5.reset_for_tests()
    yield m5
    m5.reset_for_tests()


def test_missing_pinned_model_fails_closed_without_a_network_probe(monkeypatch, _isolated_m5):
    """Removing the explicit model must not silently discover M5's workload."""
    monkeypatch.delenv("PX_M5_SPARK_MODEL")
    with patch("urllib.request.urlopen", side_effect=AssertionError("network probe")):
        result = _isolated_m5.ask_m5("reflection", "prompt", "system")
    assert result.status == "bad_response"
    assert "PX_M5_SPARK_MODEL" in result.error


def test_auto_model_is_rejected_without_a_network_probe(monkeypatch, _isolated_m5):
    """`auto` could select or disturb Adrian's currently loaded Ollama model."""
    monkeypatch.setenv("PX_M5_SPARK_MODEL", "auto")
    with patch("urllib.request.urlopen", side_effect=AssertionError("network probe")):
        result = _isolated_m5.ask_m5("reflection", "prompt", "system")
    assert result.status == "bad_response"
    assert "must name a model" in result.error


def test_resident_mode_uses_only_the_model_proven_loaded_by_api_ps(monkeypatch, _isolated_m5):
    """Borrowing is allowed only from Ollama's resident set, never /api/tags."""
    monkeypatch.setenv("PX_M5_SPARK_MODEL", "resident")
    ps = MagicMock()
    ps.read.return_value = json.dumps({"models": [{"name": "qwen3.8:27b"}]}).encode()
    generate = _response("thought")
    with patch("urllib.request.urlopen", side_effect=[ps, generate]) as request:
        assert _isolated_m5.ask_m5("reflection", "prompt", "system").status == "available"
    payload = json.loads(request.call_args_list[1].args[0].data)
    assert payload["model"] == "qwen3.8:27b"
    assert all("/api/tags" not in str(call) for call in request.call_args_list)


def test_resident_only_defers_when_nothing_is_loaded(monkeypatch, _isolated_m5):
    monkeypatch.setenv("PX_M5_SPARK_MODEL", "resident-only")
    ps = MagicMock()
    ps.read.return_value = b'{"models": []}'
    with patch("urllib.request.urlopen", return_value=ps) as request:
        result = _isolated_m5.ask_m5("reflection", "prompt", "system")
    assert result.status == "busy"
    assert request.call_count == 1


def test_resident_uses_explicit_default_only_when_nothing_is_loaded(monkeypatch, _isolated_m5):
    monkeypatch.setenv("PX_M5_SPARK_MODEL", "resident")
    monkeypatch.setenv("PX_M5_SPARK_DEFAULT", "llama3.2:1b")
    ps = MagicMock()
    ps.read.return_value = b'{"models": []}'
    generate = _response("thought")
    with patch("urllib.request.urlopen", side_effect=[ps, generate]) as request:
        assert _isolated_m5.ask_m5("reflection", "prompt", "system").status == "available"
    assert json.loads(request.call_args_list[1].args[0].data)["model"] == "llama3.2:1b"


def test_busy_process_shared_gate_returns_immediately_without_a_network_probe(_isolated_m5):
    """A held lock means occupied, not queued; the peer must not touch M5."""
    lock_path = _isolated_m5.m5_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(lock_path))
    lock.acquire(timeout=1)
    try:
        with patch("urllib.request.urlopen", side_effect=AssertionError("network probe")):
            result = _isolated_m5.ask_m5("reflection", "prompt", "system")
    finally:
        lock.release()
    assert result.status == "busy"


def test_busy_does_not_open_the_shared_circuit(_isolated_m5):
    """Contention is healthy occupancy; the next admitted request may proceed."""
    lock_path = _isolated_m5.m5_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(lock_path))
    lock.acquire(timeout=1)
    try:
        assert _isolated_m5.ask_m5("reflection", "prompt", "system").status == "busy"
    finally:
        lock.release()
    with patch("urllib.request.urlopen", return_value=_response("thought")):
        assert _isolated_m5.ask_m5("reflection", "prompt", "system").status == "available"


def test_offline_opens_monotonic_circuit_and_suppresses_repeated_probes(monkeypatch, _isolated_m5):
    """A failed reflection must not DNS/connect-stall every later reflection."""
    now = [100.0]
    monkeypatch.setattr(_isolated_m5.time, "monotonic", lambda: now[0])
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")) as probe:
        first = _isolated_m5.ask_m5("reflection", "prompt", "system")
        now[0] = 250.0
        second = _isolated_m5.ask_m5("reflection", "prompt", "system")
    assert first.status == "offline"
    assert second.status == "offline"
    assert probe.call_count == 1
    circuit = _isolated_m5.circuit_summary()
    assert circuit["status"] == "offline"
    assert circuit["open_until_monotonic"] == 400.0


def test_circuit_opened_before_a_reboot_does_not_survive_it(monkeypatch, _isolated_m5):
    """A circuit deadline is a monotonic value; time.monotonic() resets to
    ~0 on reboot, so without a boot_id check a deadline written late in a
    long prior uptime reads as still-open for a very long time afterward —
    this is the exact defect that left reflection and post_qa dead for
    hours after a real reboot (2026-08-23)."""
    now = [58 * 3600.0]  # late in a long prior uptime
    monkeypatch.setattr(_isolated_m5.time, "monotonic", lambda: now[0])
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
        assert _isolated_m5.ask_m5("reflection", "prompt", "system").status == "offline"

    # Reboot: monotonic resets near zero, boot_id changes.
    now[0] = 5.0
    monkeypatch.setattr(_isolated_m5, "_BOOT_ID", "post-reboot-id")
    with patch("urllib.request.urlopen", return_value=_response("thought")) as probe:
        result = _isolated_m5.ask_m5("reflection", "prompt", "system")
    assert result.status == "available"
    assert probe.call_count == 1


def test_circuit_retries_after_five_monotonic_minutes(monkeypatch, _isolated_m5):
    """The circuit must reopen only after its full five-minute monotonic interval."""
    now = [100.0]
    monkeypatch.setattr(_isolated_m5.time, "monotonic", lambda: now[0])
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
        assert _isolated_m5.ask_m5("reflection", "prompt", "system").status == "offline"
    now[0] = 400.0
    with patch("urllib.request.urlopen", return_value=_response("thought")) as probe:
        result = _isolated_m5.ask_m5("reflection", "prompt", "system")
    assert result.status == "available"
    assert result.response == "thought"
    assert probe.call_count == 1


def test_timeout_and_bad_response_open_the_circuit(_isolated_m5):
    """Timeouts and unusable M5 payloads are unhealthy, unlike lock contention."""
    with patch("urllib.request.urlopen", side_effect=TimeoutError("slow")):
        assert _isolated_m5.ask_m5("reflection", "prompt", "system").status == "timeout"
    assert _isolated_m5.ask_m5("reflection", "prompt", "system").status == "timeout"

    _isolated_m5.reset_for_tests()
    with patch("urllib.request.urlopen", return_value=_response("   ")):
        assert _isolated_m5.ask_m5("reflection", "prompt", "system").status == "bad_response"


def test_success_records_kind_and_session_telemetry(_isolated_m5):
    """Stage 2 (#242) needed an auditable proof that untrusted text never
    reaches the privileged brain session — this is the M5 half of it: routed
    kinds are metered by route, and the meter never carries the prompt text."""
    with patch("urllib.request.urlopen", return_value=_response("hello")):
        assert _isolated_m5.ask_m5("public_chat", "prompt", "system").status == "available"
    meter = _isolated_m5.meter_summary()
    assert meter["by_kind"]["public_chat"] == 1
    assert meter["by_route"] == {"m5": 1}
    assert meter["outcomes"]["available"]["count"] == 1
    assert "prompt" not in str(meter)
