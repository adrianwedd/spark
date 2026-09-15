"""Access to the cognition model: Ollama Cloud by default (see #308).

This module used to borrow a model from the Ollama daemon running on
``M5.local`` — Adrian's workstation — with ``/api/ps`` proving residency so a
reflection never evicted his own workload. Since #308 the tier runs on Ollama
Cloud (``https://ollama.com``, model ``deepseek-v4.1-flash:cloud``,
authenticated with ``OLLAMA_API_KEY``) and borrows nothing from anyone.

The tier keeps its old name — ``m5``, ``ask_m5``, ``M5_SESSION``,
``state/m5/``, ``by_route.m5`` — because it names a *role*, not a host: the
no-tools cognitive tier that carries text SPARK did not write. Renaming it is a
separate, mechanical change; leaving the name while changing the host is
deliberate, and this docstring is the record of it.

What survives the move, and is more load-bearing than the host:

- **No tools, no filesystem.** ``brain.py``'s ``_M5_KINDS`` boundary is "the
  privileged session never sees untrusted text" (public chat, Obi chat, post
  and blog QA). A cloud model has no tools and cannot read this repository
  either — but that text now leaves the LAN. That is a change in *exposure*
  even though it is not a change in privilege, and it is the one real cost of
  this arrangement.
- **Defer, never escalate.** A failure here is terminal for the caller
  (``mind.py::call_llm``). It never falls through to the resident Claude
  session and never reaches a Pi-local model.
- **No waiting in line.** The process-shared gate has a zero timeout: a second
  concurrent request defers rather than queueing behind the first.
"""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from filelock import FileLock, Timeout as FileLockTimeout

from .state import PROJECT_ROOT, atomic_write
from .time import utc_timestamp

M5_SESSION = "m5"
M5_HOST = os.environ.get("PX_M5_SPARK_HOST", "https://ollama.com")
# Hosted tiers need more headroom than a warm LAN daemon: a cold 486B model
# behind an internet round trip can comfortably outlast the 30s that was sized
# for `M5.local`. A timeout opens the five-minute circuit, so undersizing this
# is a five-minute outage per miss.
M5_TIMEOUT_S = float(os.environ.get("PX_M5_SPARK_TIMEOUT_S", "60"))
CIRCUIT_OPEN_S = 300.0

# Bearer token for a hosted host, in precedence order.
#   1. PX_M5_SPARK_API_KEY — lets the cognition tier hold a different key from
#      the persona tools.
#   2. OLLAMA_API_KEY — the name the `ollama` CLI itself reads, so a host
#      already configured for cloud models needs no SPARK-specific duplicate.
#   3. OLLAMA_CLOUD_API_KEY — the repo's original name for this credential
#      (`mind.py`'s long-dead cloud tier read it). The live robot still has the
#      working key under this name, so it is kept as a real fallback rather
#      than a migration note: honouring it means deploying #308 needs no
#      credential to be copied or renamed anywhere.
_API_KEY_VARS = ("PX_M5_SPARK_API_KEY", "OLLAMA_API_KEY", "OLLAMA_CLOUD_API_KEY")


def _read_boot_id() -> str:
    """The kernel's boot id — see brain_daemon._read_boot_id for why this host
    needs it: no RTC, so a monotonic deadline written before a reboot reads as
    still-open for a full CIRCUIT_OPEN_S (or worse, whatever uptime the prior
    boot had reached) against the new boot's near-zero clock."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="utf-8"
        ).strip() or "unknown"
    except OSError:
        return "unknown"


_BOOT_ID = _read_boot_id()

M5Status = Literal["available", "busy", "timeout", "offline", "bad_response"]


@dataclass(frozen=True)
class M5Result:
    status: M5Status
    response: str = ""
    error: str = ""
    duration_ms: int = 0


def _state_dir() -> Path:
    root = Path(os.environ.get("PROJECT_ROOT", PROJECT_ROOT))
    return Path(os.environ.get("PX_STATE_DIR", root / "state"))


def _m5_dir() -> Path:
    return _state_dir() / "m5"


def m5_lock_path() -> Path:
    return _m5_dir() / "spark.lock"


def _circuit_path() -> Path:
    return _m5_dir() / "circuit.json"


def _meter_path() -> Path:
    return _m5_dir() / "meter.json"


def _ensure_dir() -> bool:
    try:
        _m5_dir().mkdir(parents=True, exist_ok=True)
        os.chmod(_m5_dir(), 0o1777)
        return True
    except OSError:
        return False


def _api_key() -> str:
    """Bearer token for a hosted host; empty when none is configured."""
    for var in _API_KEY_VARS:
        value = os.environ.get(var, "").strip()
        if value:
            return value
    return ""


def is_local_host(host: str) -> bool:
    """Is this a daemon on this machine or the LAN?

    Only a local daemon has a resident model set worth borrowing from, and only
    a local daemon has a workload worth being polite to. Everything else is a
    hosted endpoint reached over the internet, which needs a key and cannot
    answer `/api/ps` at all.
    """
    try:
        netloc = urllib.parse.urlsplit(host).netloc or host
    except ValueError:
        netloc = host
    hostname = netloc.rsplit("@", 1)[-1].split(":")[0].strip("[]").lower()
    return hostname in {"localhost", "127.0.0.1", "::1", "0.0.0.0"} \
        or hostname.endswith(".local")


def normalize_model(name: str) -> str:
    """Compare cloud and local spellings of the same model.

    Ollama Cloud lists `deepseek-v4.1-flash` in `/api/tags` but accepts — and
    is conventionally invoked as — `deepseek-v4.1-flash:cloud`. A startup probe
    that compared the raw strings would report a perfectly healthy tier as
    missing its model.
    """
    return name.strip().removesuffix(":cloud").removesuffix("-cloud")


def configured_model() -> str | None:
    """The pinned cognition model, or None when it is unset or `auto`."""
    model = os.environ.get("PX_M5_SPARK_MODEL", "").strip()
    if not model or model.lower() == "auto":
        return None
    return model


def _resident_model(mode: str) -> str | None:
    """Return only a model `/api/ps` proves loaded; never consult `/api/tags`."""
    response = urllib.request.urlopen(f"{M5_HOST}/api/ps", timeout=3)
    body = json.loads(response.read())
    models = body.get("models", []) if isinstance(body, dict) else []
    if models and isinstance(models[0], dict) and isinstance(models[0].get("name"), str):
        return models[0]["name"]
    if mode == "resident":
        default = os.environ.get("PX_M5_SPARK_DEFAULT", "").strip()
        if default and default.lower() not in {"auto", "resident", "resident-only"}:
            return default
    return None


def _record_request(kind: str, status: M5Status, duration_ms: int) -> None:
    """Best-effort request evidence, keyed by workload kind and session."""
    if not _ensure_dir():
        return
    try:
        data = json.loads(_meter_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    by_kind = data.setdefault("by_kind", {})
    by_route = data.setdefault("by_route", {})
    by_status = data.setdefault("by_status", {})
    by_kind[kind] = int(by_kind.get(kind, 0)) + 1
    by_route[M5_SESSION] = int(by_route.get(M5_SESSION, 0)) + 1
    by_status[status] = int(by_status.get(status, 0)) + 1
    outcomes = data.setdefault("outcomes", {})
    outcome = outcomes.setdefault(status, {"count": 0, "total_duration_ms": 0})
    outcome["count"] += 1
    outcome["total_duration_ms"] += duration_ms
    data["total"] = sum(by_kind.values())
    data["updated_ts"] = utc_timestamp()
    try:
        atomic_write(_meter_path(), json.dumps(data, indent=2))
    except OSError:
        pass


def meter_summary() -> dict:
    empty = {"by_kind": {}, "by_route": {}, "by_status": {}, "outcomes": {}, "total": 0}
    try:
        data = json.loads(_meter_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    return data if isinstance(data, dict) else empty


def _result(status: M5Status, *, response: str = "", error: str = "",
            kind: str, started: float) -> M5Result:
    duration_ms = round((time.monotonic() - started) * 1000)
    _record_request(kind, status, duration_ms)
    return M5Result(status=status, response=response, error=error, duration_ms=duration_ms)


def _open_circuit(status: M5Status) -> None:
    try:
        atomic_write(_circuit_path(), json.dumps({"status": status,
            "open_until_monotonic": time.monotonic() + CIRCUIT_OPEN_S,
            "boot_id": _BOOT_ID}))
    except OSError:
        pass


def circuit_summary() -> dict:
    try:
        data = json.loads(_circuit_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": None, "open_until_monotonic": 0.0}
    return data if isinstance(data, dict) else {"status": None, "open_until_monotonic": 0.0}


def ask_m5(kind: str, prompt: str, system: str, *, timeout_s: float | None = None) -> M5Result:
    """Run one no-tools turn on the pinned cognition model, without queueing."""
    started = time.monotonic()
    mode = configured_model()
    if mode is None:
        return _result("bad_response", kind=kind, started=started,
                       error="PX_M5_SPARK_MODEL must name a model explicitly (not auto)")

    # Residency borrowing is a property of a local daemon. Asking a hosted host
    # for it is not a degraded answer, it is a wrong question — and it fails as
    # an opaque 401 that opens the five-minute circuit, which is exactly how
    # reflection died silently on 2026-08-25 (#302). Refuse it by name instead.
    if mode in {"resident", "resident-only"} and not is_local_host(M5_HOST):
        return _result("bad_response", kind=kind, started=started,
                       error=(f"PX_M5_SPARK_MODEL={mode} borrows a resident model from a "
                              f"local Ollama daemon, but {M5_HOST} is hosted. Set "
                              f"PX_M5_SPARK_MODEL to an explicit model "
                              f"(e.g. deepseek-v4.1-flash:cloud)."))

    if not _ensure_dir():
        _open_circuit("offline")
        return _result("offline", kind=kind, started=started, error="M5 state directory unavailable")

    lock = FileLock(str(m5_lock_path()))
    try:
        # Exactly zero wait: the lock spans processes and a caller must defer
        # rather than enqueue behind another SPARK workload.
        lock.acquire(timeout=0)
    except (FileLockTimeout, OSError):
        return _result("busy", kind=kind, started=started, error="M5 SPARK model busy")
    try:
        circuit = circuit_summary()
        # A circuit opened before this boot is meaningless: time.monotonic()
        # resets to ~0 on reboot, so a deadline computed against the prior
        # boot's uptime can read as "still open" for days regardless of
        # whether M5 is actually reachable now.
        if circuit.get("boot_id") == _BOOT_ID and \
                time.monotonic() < float(circuit.get("open_until_monotonic", 0)):
            status = circuit.get("status")
            if status in ("timeout", "offline", "bad_response"):
                return _result(status, kind=kind, started=started, error="M5 circuit open")
        try:
            model = _resident_model(mode) if mode in {"resident", "resident-only"} else mode
        except (urllib.error.URLError, OSError, ValueError) as exc:
            _open_circuit("offline")
            return _result("offline", kind=kind, started=started, error=str(exc))
        if model is None:
            return _result("busy", kind=kind, started=started, error="no resident M5 model")
        payload = json.dumps({
            "model": model,
            "prompt": prompt,
            "system": system,
            "stream": False,
            # Reasoning chains re-enable refusal in small models and burn the
            # whole budget on a  thinking block that never emits an answer.
            "think": False,
        }).encode()
        headers = {"Content-Type": "application/json"}
        key = _api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        elif not is_local_host(M5_HOST):
            # A missing credential is a configuration fault, not a transport
            # one: report it every time rather than opening a circuit that
            # would hide the message behind "M5 circuit open" for five
            # minutes. Same fail-closed-without-a-network-probe shape as the
            # missing-model check above.
            return _result("bad_response", kind=kind, started=started,
                           error=(f"no API key for hosted Ollama host {M5_HOST} — set "
                                  f"{_API_KEY_VARS[1]} (or {_API_KEY_VARS[0]})"))
        request = urllib.request.Request(
            f"{M5_HOST}/api/generate", data=payload, headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s or M5_TIMEOUT_S) as response:
                body = json.loads(response.read())
        except (TimeoutError, socket.timeout) as exc:
            _open_circuit("timeout")
            return _result("timeout", kind=kind, started=started, error=str(exc))
        except urllib.error.URLError as exc:
            if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)):
                _open_circuit("timeout")
                return _result("timeout", kind=kind, started=started, error=str(exc))
            _open_circuit("offline")
            return _result("offline", kind=kind, started=started, error=str(exc))
        except (urllib.error.HTTPError, OSError, ValueError) as exc:
            _open_circuit("bad_response")
            return _result("bad_response", kind=kind, started=started, error=str(exc))

        text = body.get("response") if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip():
            _open_circuit("bad_response")
            return _result("bad_response", kind=kind, started=started, error="M5 returned no usable response")
        return _result("available", kind=kind, started=started, response=text.strip())
    finally:
        try:
            lock.release()
        except OSError:
            pass


def probe(timeout_s: float = 5.0) -> str:
    """One-line, never-raising startup truth about the cognition tier.

    Lives here rather than in `px-mind` so the probe cannot drift from the host,
    key and model that `ask_m5` actually uses — the drift the old probe had, and
    the reason #302 stayed invisible: it reported a model `ask_m5` never asked
    for.
    """
    model = configured_model()
    if model is None:
        return "⚠ cognition tier: PX_M5_SPARK_MODEL must name a model explicitly (not auto)"

    # Ask the most specific question first: a mode that cannot work where it
    # is pointed is a stronger diagnosis than a credential that is missing.
    if model in {"resident", "resident-only"} and not is_local_host(M5_HOST):
        return (f"⚠ cognition tier: PX_M5_SPARK_MODEL={model} needs a local daemon, "
                f"but {M5_HOST} is hosted")

    headers = {}
    key = _api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    elif not is_local_host(M5_HOST):
        return f"⚠ cognition tier: no API key for hosted {M5_HOST} (set {_API_KEY_VARS[1]})"

    try:
        request = urllib.request.Request(f"{M5_HOST}/api/tags", headers=headers)
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = json.loads(response.read())
    except Exception as exc:  # noqa: BLE001 - a probe reports, it never raises
        return f"⚠ cognition tier unreachable at startup: {exc}"

    available = [m.get("name", "") for m in (body.get("models") or [])
                 if isinstance(m, dict)]
    wanted = normalize_model(model)
    if any(normalize_model(name) == wanted for name in available):
        return f"✓ cognition tier: model '{model}' available ({M5_HOST})"
    return (f"⚠ cognition tier: model '{model}' NOT found on {M5_HOST} — "
            f"available: {', '.join(available) or 'none'}")


def reset_for_tests() -> None:
    """Reset process-local circuit state; test-only helper."""
    try:
        _circuit_path().unlink()
    except OSError:
        pass
