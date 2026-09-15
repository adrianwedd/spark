"""Contracts for the cognition-tier client (`pxh.m5`).

Since #308 the tier is Ollama Cloud by default (`https://ollama.com`,
`deepseek-v4.1-flash:cloud`, `OLLAMA_API_KEY`). The name `m5` survives because
it names a role, not a host — see the module docstring.
"""
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
    # A hosted host with no key fails closed before any network call, so every
    # test that is not *about* the missing key needs one present.
    monkeypatch.setenv("PX_M5_SPARK_API_KEY", "test-key")
    from pxh import m5

    m5.reset_for_tests()
    yield m5
    m5.reset_for_tests()


@pytest.fixture
def _local_host(monkeypatch, _isolated_m5):
    """Point the tier at a LAN daemon — the only place `resident` is legal."""
    monkeypatch.setattr(_isolated_m5, "M5_HOST", "http://M5.local:11434")
    return _isolated_m5


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


def test_resident_mode_uses_only_the_model_proven_loaded_by_api_ps(monkeypatch, _local_host):
    """Borrowing is allowed only from Ollama's resident set, never /api/tags."""
    monkeypatch.setenv("PX_M5_SPARK_MODEL", "resident")
    ps = MagicMock()
    ps.read.return_value = json.dumps({"models": [{"name": "qwen3.8:27b"}]}).encode()
    generate = _response("thought")
    with patch("urllib.request.urlopen", side_effect=[ps, generate]) as request:
        assert _local_host.ask_m5("reflection", "prompt", "system").status == "available"
    payload = json.loads(request.call_args_list[1].args[0].data)
    assert payload["model"] == "qwen3.8:27b"
    assert all("/api/tags" not in str(call) for call in request.call_args_list)


def test_resident_only_defers_when_nothing_is_loaded(monkeypatch, _local_host):
    monkeypatch.setenv("PX_M5_SPARK_MODEL", "resident-only")
    ps = MagicMock()
    ps.read.return_value = b'{"models": []}'
    with patch("urllib.request.urlopen", return_value=ps) as request:
        result = _local_host.ask_m5("reflection", "prompt", "system")
    assert result.status == "busy"
    assert request.call_count == 1


def test_resident_uses_explicit_default_only_when_nothing_is_loaded(monkeypatch, _local_host):
    monkeypatch.setenv("PX_M5_SPARK_MODEL", "resident")
    monkeypatch.setenv("PX_M5_SPARK_DEFAULT", "llama3.2:1b")
    ps = MagicMock()
    ps.read.return_value = b'{"models": []}'
    generate = _response("thought")
    with patch("urllib.request.urlopen", side_effect=[ps, generate]) as request:
        assert _local_host.ask_m5("reflection", "prompt", "system").status == "available"
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


# ── The hosted tier (#308) ─────────────────────────────────────────
#
# These replaced a local daemon borrowed from Adrian's workstation. The
# failure modes that mattered then (contention, eviction) do not exist here;
# the ones that matter now are a missing credential, a residency mode that
# cannot mean anything against a hosted host, and a probe that reports a model
# the request never asks for (#302).

def test_default_host_is_the_cloud_endpoint_not_a_local_daemon():
    """No default may reach a Pi-local or LAN Ollama host."""
    from pxh import m5

    assert m5.M5_HOST == "https://ollama.com"
    assert not m5.is_local_host(m5.M5_HOST)


def test_hosted_request_carries_the_model_and_the_bearer_token(_isolated_m5):
    with patch("urllib.request.urlopen", return_value=_response("thought")) as probe:
        assert _isolated_m5.ask_m5("reflection", "prompt", "system").status == "available"
    request = probe.call_args_list[0].args[0]
    assert request.full_url == "https://ollama.com/api/generate"
    assert request.get_header("Authorization") == "Bearer test-key"
    assert json.loads(request.data)["model"] == "spark:fixed"


def test_resident_mode_on_a_hosted_host_fails_loudly_without_a_probe(monkeypatch, _isolated_m5):
    """`resident` borrows from a local daemon's /api/ps set.

    Against a hosted host that endpoint is a 401, which would open the
    five-minute circuit and report itself as "offline" — the shape that left
    reflection dead and silent for 22h on 2026-08-25 (#302).
    """
    monkeypatch.setenv("PX_M5_SPARK_MODEL", "resident")
    with patch("urllib.request.urlopen", side_effect=AssertionError("network probe")):
        result = _isolated_m5.ask_m5("reflection", "prompt", "system")
    assert result.status == "bad_response"
    assert "local Ollama daemon" in result.error
    assert "deepseek-v4.1-flash:cloud" in result.error


def test_missing_key_on_a_hosted_host_fails_loudly_without_a_probe(monkeypatch, _isolated_m5):
    """A configuration fault must be re-reported, never hidden behind a circuit."""
    monkeypatch.delenv("PX_M5_SPARK_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    with patch("urllib.request.urlopen", side_effect=AssertionError("network probe")):
        result = _isolated_m5.ask_m5("reflection", "prompt", "system")
    assert result.status == "bad_response"
    assert "OLLAMA_API_KEY" in result.error


def test_a_local_host_needs_no_key(monkeypatch, _local_host):
    monkeypatch.delenv("PX_M5_SPARK_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    with patch("urllib.request.urlopen", return_value=_response("thought")) as probe:
        assert _local_host.ask_m5("reflection", "prompt", "system").status == "available"
    assert probe.call_args_list[0].args[0].get_header("Authorization") is None


def test_local_host_detection_covers_loopback_and_mdns():
    from pxh import m5

    for host in ("http://localhost:11434", "http://127.0.0.1:11434",
                 "http://M5.local:11434", "https://M5.local:11434"):
        assert m5.is_local_host(host), host
    for host in ("https://ollama.com", "api.ollama.com", "https://ollama.example.org"):
        assert not m5.is_local_host(host), host


def test_model_normalisation_treats_cloud_and_local_spellings_as_one():
    from pxh import m5

    assert m5.normalize_model("deepseek-v4.1-flash:cloud") == "deepseek-v4.1-flash"
    assert m5.normalize_model("gpt-oss:120b-cloud") == "gpt-oss:120b"
    assert m5.normalize_model("gemma4:e4b") == "gemma4:e4b"


def test_probe_reports_a_cloud_spelling_of_the_model_as_available(monkeypatch, _isolated_m5):
    """#302 was a probe disagreeing with the request. /api/tags lists the model
    without its `:cloud` suffix, so a raw string compare would false-alarm."""
    monkeypatch.setenv("PX_M5_SPARK_MODEL", "deepseek-v4.1-flash:cloud")
    inner = MagicMock()
    inner.read.return_value = json.dumps(
        {"models": [{"name": "deepseek-v4.1-flash"}, {"name": "gpt-oss:20b"}]}).encode()
    tags = MagicMock()
    tags.__enter__.return_value = inner
    tags.__exit__.return_value = False
    with patch("urllib.request.urlopen", return_value=tags):
        line = _isolated_m5.probe()
    assert line.startswith("✓")
    assert "deepseek-v4.1-flash:cloud" in line


def test_probe_never_raises(monkeypatch, _isolated_m5):
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
        assert "unreachable" in _isolated_m5.probe()
    monkeypatch.delenv("PX_M5_SPARK_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    assert "OLLAMA_API_KEY" in _isolated_m5.probe()
    monkeypatch.setenv("PX_M5_SPARK_MODEL", "resident")
    assert "local daemon" in _isolated_m5.probe()
