"""Contracts for the cognition-tier client (`pxh.m5`).

Since #308 the tier is Ollama Cloud by default (`https://ollama.com`,
`deepseek-v4.1-flash:cloud`, `OLLAMA_API_KEY`). The name `m5` survives because
it names a role, not a host — see the module docstring.
"""
from __future__ import annotations

import time

import io
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


def test_the_legacy_cloud_key_name_still_authenticates(monkeypatch, _isolated_m5):
    """The live robot's `.env` holds its working cloud key as
    `OLLAMA_CLOUD_API_KEY` — the name `mind.py`'s long-dead cloud tier read.
    Honouring it is what makes deploying #308 a two-line `.env` change instead
    of a credential copy."""
    monkeypatch.delenv("PX_M5_SPARK_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("OLLAMA_CLOUD_API_KEY", "legacy-key")
    with patch("urllib.request.urlopen", return_value=_response("thought")) as probe:
        assert _isolated_m5.ask_m5("reflection", "p", "s").status == "available"
    assert probe.call_args_list[0].args[0].get_header("Authorization") == "Bearer legacy-key"


def test_missing_key_on_a_hosted_host_fails_loudly_without_a_probe(monkeypatch, _isolated_m5):
    """A configuration fault must be re-reported, never hidden behind a circuit."""
    monkeypatch.delenv("PX_M5_SPARK_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_CLOUD_API_KEY", raising=False)
    with patch("urllib.request.urlopen", side_effect=AssertionError("network probe")):
        result = _isolated_m5.ask_m5("reflection", "prompt", "system")
    assert result.status == "bad_response"
    assert "OLLAMA_API_KEY" in result.error


def test_a_local_host_needs_no_key(monkeypatch, _local_host):
    monkeypatch.delenv("PX_M5_SPARK_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_CLOUD_API_KEY", raising=False)
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
    monkeypatch.delenv("OLLAMA_CLOUD_API_KEY", raising=False)
    assert "OLLAMA_API_KEY" in _isolated_m5.probe()
    monkeypatch.setenv("PX_M5_SPARK_MODEL", "resident")
    assert "local daemon" in _isolated_m5.probe()


# ---------------------------------------------------------------------------
# HTTP status vs transport fault (#324)
#
# Every HTTP response used to arrive at the URLError branch, because
# HTTPError is a subclass of URLError and that branch was written first. The
# status was then reported as `offline` and opened the five-minute circuit,
# so the actual reason survived exactly one log line and every later call in
# the window read "M5 circuit open".
# ---------------------------------------------------------------------------

def _http_error(code: int, body: bytes = b"", url: str = "https://ollama.com/api/generate"):
    return urllib.error.HTTPError(
        url, code, f"HTTP {code}", MagicMock(), io.BytesIO(body))


def test_http_status_is_caught_before_the_transport_branch(_isolated_m5):
    """The regression itself: an HTTPError must not be read as URLError."""
    with patch("urllib.request.urlopen", side_effect=_http_error(401, b'{"error":"invalid api key"}')):
        result = _isolated_m5.ask_m5("reflection", "prompt", "system")
    assert result.status == "bad_response"
    assert "offline" not in result.status
    assert "401" in result.error
    # The provider's own words, not just the number.
    assert "invalid api key" in result.error


def test_a_rejected_credential_reports_every_time_and_opens_no_circuit(_isolated_m5):
    """A missing key is deliberately reported every time; a rejected one is
    the same configuration fault and must not take the opposite path."""
    # A fresh exception per call, deliberately: an HTTPError wraps one
    # response body, and re-raising the same instance would read EOF the
    # second time and make "the reason survives" untestable.
    def _unauthorized(*_a, **_k):
        raise _http_error(401, b'{"error":"invalid api key"}')

    with patch("urllib.request.urlopen", side_effect=_unauthorized) as probe:
        first = _isolated_m5.ask_m5("reflection", "prompt", "system")
        second = _isolated_m5.ask_m5("reflection", "prompt", "system")
    assert first.status == second.status == "bad_response"
    assert "invalid api key" in second.error, "the reason must survive the next call"
    assert probe.call_count == 2, "no circuit means the probe is not suppressed"
    assert _isolated_m5.circuit_summary()["status"] is None


def test_a_refused_request_does_not_open_the_circuit(_isolated_m5):
    """A 4xx is the request being refused; retrying it identically fails
    identically, so a circuit buys nothing and costs the reason."""
    for code in (400, 404, 413, 422):
        _isolated_m5.reset_for_tests()
        with patch("urllib.request.urlopen", side_effect=_http_error(code)):
            assert _isolated_m5.ask_m5("describe_scene", "prompt", "system").status == "bad_response"
        assert _isolated_m5.circuit_summary()["status"] is None, f"{code} opened a circuit"


def test_rate_limiting_is_occupancy_not_an_outage(_isolated_m5):
    """429 means slow down, which callers already model as `busy`."""
    with patch("urllib.request.urlopen", side_effect=_http_error(429)):
        result = _isolated_m5.ask_m5("reflection", "prompt", "system")
    assert result.status == "busy"
    assert _isolated_m5.circuit_summary()["status"] is None


def test_a_provider_fault_still_opens_the_circuit(_isolated_m5):
    """5xx is the provider accepting the request and failing to serve it —
    the case the circuit was built for. It must not be weakened away."""
    def _unavailable(*_a, **_k):
        raise _http_error(503)

    with patch("urllib.request.urlopen", side_effect=_unavailable) as probe:
        first = _isolated_m5.ask_m5("reflection", "prompt", "system")
        second = _isolated_m5.ask_m5("reflection", "prompt", "system")
    assert first.status == "offline"
    assert "503" in first.error
    assert second.error == "M5 circuit open"
    assert probe.call_count == 1
    assert _isolated_m5.circuit_summary()["status"] == "offline"


def test_an_unreadable_error_body_does_not_replace_the_status(_isolated_m5):
    """Reading the provider's explanation must never be able to raise."""
    broken = urllib.error.HTTPError(
        "https://ollama.com/api/generate", 400, "HTTP 400", MagicMock(), None)

    def _boom():
        raise OSError("body gone")

    broken.read = _boom
    with patch("urllib.request.urlopen", side_effect=broken):
        result = _isolated_m5.ask_m5("reflection", "prompt", "system")
    assert result.status == "bad_response"
    assert "400" in result.error


# ---------------------------------------------------------------------------
# Secret hygiene (#317 lists "never log OLLAMA_API_KEY" as an acceptance item)
# ---------------------------------------------------------------------------
#
# Nothing pinned this. The key is only ever interpolated into an Authorization
# header, which reads as obviously safe — but #324 made the provider's *error
# body* part of the reported error, and an error string travels into logs, into
# `state/claude_sessions.jsonl`'s neighbours and into whatever a caller prints.
# "It only builds a header" is exactly the kind of reasoning that stops being
# true one edit later, so the property is asserted rather than argued.

def _walk_text(root):
    """Every text blob under a directory, concatenated. Binary files skipped."""
    import pathlib
    out = []
    for path in sorted(pathlib.Path(root).rglob("*")):
        if path.is_file():
            try:
                out.append(path.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, OSError):
                continue
    return "\n".join(out)


def test_the_api_key_appears_nowhere_after_every_failure_mode(
        _isolated_m5, monkeypatch, tmp_path):
    """Not in the meter, not in the circuit, not in an error, not on disk.

    Every outcome that produces a message is exercised, because the leak — if
    one ever exists — will be in whichever one nobody checked.
    """
    secret = "super-secret-token-8f3a1c"
    monkeypatch.setenv("OLLAMA_API_KEY", secret)
    monkeypatch.delenv("PX_M5_SPARK_API_KEY", raising=False)

    def _http(code, body=b""):
        raise urllib.error.HTTPError(
            "https://ollama.com/api/generate", code, f"HTTP {code}",
            MagicMock(), io.BytesIO(body))

    errors = []

    # available
    with patch("urllib.request.urlopen", return_value=_response("hello")):
        errors.append(_isolated_m5.ask_m5("reflection", "p", "s").error)
    # offline (transport)
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
        errors.append(_isolated_m5.ask_m5("reflection", "p", "s").error)
    # circuit open, which is the path that *replaces* the real error
    errors.append(_isolated_m5.ask_m5("reflection", "p", "s").error)
    # timeout
    _isolated_m5.reset_for_tests()
    with patch("urllib.request.urlopen", side_effect=TimeoutError("slow")):
        errors.append(_isolated_m5.ask_m5("reflection", "p", "s").error)
    # HTTP status with a body — #324's path, and the one that could echo it
    _isolated_m5.reset_for_tests()
    with patch("urllib.request.urlopen",
               side_effect=lambda *a, **k: _http(401, b'{"error":"bad token"}')):
        errors.append(_isolated_m5.ask_m5("reflection", "p", "s").error)
    # unusable payload
    _isolated_m5.reset_for_tests()
    with patch("urllib.request.urlopen", return_value=_response("   ")):
        errors.append(_isolated_m5.ask_m5("reflection", "p", "s").error)

    assert any(e for e in errors), "no failure produced a message to check"
    for err in errors:
        assert secret not in (err or ""), f"the key surfaced in an error: {err!r}"

    on_disk = _walk_text(tmp_path)
    assert secret not in on_disk, "the API key was written under the state dir"
    assert secret not in str(_isolated_m5.meter_summary())
    assert secret not in str(_isolated_m5.circuit_summary())


def test_the_missing_key_message_names_the_variable_not_the_value(_isolated_m5, monkeypatch):
    """The failure an operator actually hits must be actionable without being a
    disclosure: it names which variable to set, never what it held."""
    monkeypatch.delenv("PX_M5_SPARK_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_CLOUD_API_KEY", raising=False)

    result = _isolated_m5.ask_m5("reflection", "p", "s")

    assert result.status == "bad_response"
    assert "OLLAMA_API_KEY" in result.error


# ── the token-log bucket for the host that answered (#306) ───────────────


def test_backend_label_names_the_cloud_bucket_for_a_hosted_tier(monkeypatch):
    import pxh.m5 as m5

    monkeypatch.setattr(m5, "M5_HOST", "https://ollama.com")
    assert m5.backend_label() == "ollama-m5"
    assert m5.backend_label("https://ollama.com") == "ollama-m5"


def test_backend_label_names_the_local_bucket_for_a_lan_daemon(monkeypatch):
    """A LAN daemon is not metered consumption, and #308 made that distinction
    load-bearing: `ollama-m5` now means "Ollama Cloud, on a plan"."""
    import pxh.m5 as m5

    monkeypatch.setattr(m5, "M5_HOST", "http://localhost:11434")
    assert m5.backend_label() == "ollama-local"
    assert m5.backend_label("http://m5.local:11434") == "ollama-local"
    assert m5.backend_label("http://127.0.0.1:11434") == "ollama-local"


def test_results_carry_the_serving_backend(monkeypatch):
    """A caller recording spend reads it off the result, so it has to be there
    even on a failed call — that is the call the old code logged as unknown."""
    import pxh.m5 as m5

    monkeypatch.setattr(m5, "M5_HOST", "https://ollama.com")
    result = m5._result("bad_response", kind="voice_turn", started=time.monotonic(),
                        error="no")
    assert result.backend == "ollama-m5"

def test_the_request_meter_is_a_gauge_not_a_ledger(tmp_path, monkeypatch):
    """Written on every cognition request (~12k on picar), so the fsync forced a
    journal commit per request for a number nobody needs across a power cut
    (#247)."""
    import pxh.m5 as m5
    import pxh.state as state

    monkeypatch.setattr(m5, "_m5_dir", lambda: tmp_path)
    monkeypatch.setattr(m5, "_ensure_dir", lambda: tmp_path)
    calls = []
    real = state.atomic_write

    def spy(path, content, **kwargs):
        calls.append(kwargs.get("durable", True))
        return real(path, content, **kwargs)

    monkeypatch.setattr(m5, "atomic_write", spy)
    m5._record_request("voice_turn", "available", 42)

    assert calls == [False], "the gauge must not force a journal commit"
    body = (tmp_path / "meter.json").read_text()
    assert "voice_turn" in body
