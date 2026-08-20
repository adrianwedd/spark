"""Contention-aware access to a configured or already-resident M5 model."""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from filelock import FileLock, Timeout as FileLockTimeout

from .state import PROJECT_ROOT, atomic_write
from .time import utc_timestamp

M5_SESSION = "m5"
M5_HOST = os.environ.get("PX_M5_SPARK_HOST", "http://M5.local:11434")
M5_TIMEOUT_S = float(os.environ.get("PX_M5_SPARK_TIMEOUT_S", "30"))
CIRCUIT_OPEN_S = 300.0

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


def _configured_model() -> str | None:
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
            "open_until_monotonic": time.monotonic() + CIRCUIT_OPEN_S}))
    except OSError:
        pass


def circuit_summary() -> dict:
    try:
        data = json.loads(_circuit_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": None, "open_until_monotonic": 0.0}
    return data if isinstance(data, dict) else {"status": None, "open_until_monotonic": 0.0}


def ask_m5(kind: str, prompt: str, system: str, *, timeout_s: float | None = None) -> M5Result:
    """Run one no-tools turn on the pinned M5 model, without ever waiting in line."""
    started = time.monotonic()
    mode = _configured_model()
    if mode is None:
        return _result("bad_response", kind=kind, started=started,
                       error="PX_M5_SPARK_MODEL must name a model explicitly (not auto)")

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
        if time.monotonic() < float(circuit.get("open_until_monotonic", 0)):
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
            "think": False,
        }).encode()
        request = urllib.request.Request(
            f"{M5_HOST}/api/generate", data=payload,
            headers={"Content-Type": "application/json"},
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


def reset_for_tests() -> None:
    """Reset process-local circuit state; test-only helper."""
    try:
        _circuit_path().unlink()
    except OSError:
        pass
