"""PiCar-X REST API — thin HTTP facade over the voice_loop tool pipeline.

Also serves the SPARK web UI at / (text chat + quick-action buttons).

Usage:
    # Via launcher (preferred — sets up env correctly):
    bin/px-api-server --dry-run

    # Direct (only if px-env already sourced):
    uvicorn pxh.api:app --host 0.0.0.0 --port 8420
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .state import load_session, update_session
from .time import utc_timestamp
from .voice_loop import (
    ALLOWED_TOOLS,
    PERSONA_PROMPTS,
    PROJECT_ROOT,
    VoiceLoopError,
    build_model_prompt,
    execute_tool,
    extract_action,
    read_prompt,
    run_codex,
    validate_action,
)

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_API_TOKEN: Optional[str] = None


def _load_token() -> str:
    """Load PX_API_TOKEN from environment. Hard-fail if missing."""
    global _API_TOKEN
    token = os.environ.get("PX_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "PX_API_TOKEN is not set. Refusing to start without authentication. "
            "Add PX_API_TOKEN=<hex-token> to .env"
        )
    _API_TOKEN = token
    return token


def _verify_token(request: Request) -> None:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    provided = auth[7:]
    if _API_TOKEN is None or not secrets.compare_digest(provided, _API_TOKEN):
        raise HTTPException(status_code=401, detail="invalid token")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(application: FastAPI):
    _load_token()
    yield


app = FastAPI(title="PiCar-X API", version="0.1.0", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://spark.wedd.au", "http://spark.wedd.au", "https://spark-api.wedd.au", "http://localhost:8000"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

_THERMAL_ZONE = Path("/sys/class/thermal/thermal_zone0/temp")

# ---------------------------------------------------------------------------
# Job registry (async wander)
# ---------------------------------------------------------------------------

_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_JOBS_MAX = 200  # evict oldest when exceeded


def _set_job(job_id: str, data: Dict[str, Any]) -> None:
    with _jobs_lock:
        _jobs[job_id] = data
        # Evict oldest entries when registry grows too large
        if len(_jobs) > _JOBS_MAX:
            oldest = list(_jobs.keys())[: len(_jobs) - _JOBS_MAX]
            for k in oldest:
                del _jobs[k]


def _get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with _jobs_lock:
        return _jobs.get(job_id)


# ---------------------------------------------------------------------------
# History ring buffer (background thread, 30s interval)
# ---------------------------------------------------------------------------

import collections as _collections

_history_buf: "_collections.deque[Dict[str, Any]]" = _collections.deque(maxlen=60)
_history_lock = threading.Lock()


def _collect_history_sample(state_dir: "Path", persona: str = "") -> "Dict[str, Any]":
    """Collect one vitals + sonar + ambient reading. Extracted for testability."""
    import time as _time

    sample: Dict[str, Any] = {"ts": utc_timestamp()}

    # CPU / RAM / disk
    try:
        import psutil as _psutil
        sample["cpu_pct"] = round(_psutil.cpu_percent(interval=None), 1)
        sample["ram_pct"] = round(_psutil.virtual_memory().percent, 1)
        sample["disk_pct"] = round(_psutil.disk_usage("/").percent, 1)
    except Exception:
        sample["cpu_pct"] = None
        sample["ram_pct"] = None
        sample["disk_pct"] = None

    # CPU temperature
    try:
        raw = _THERMAL_ZONE.read_text().strip()
        sample["cpu_temp_c"] = round(int(raw) / 1000.0, 1)
    except Exception:
        sample["cpu_temp_c"] = None

    # Battery
    try:
        bdata = json.loads((state_dir / "battery.json").read_text())
        sample["battery_pct"] = bdata.get("pct")
    except Exception:
        sample["battery_pct"] = None

    # Sonar — age gate: null if > 60s
    try:
        sdata = json.loads((state_dir / "sonar_live.json").read_text())
        age = _time.time() - float(sdata["ts"])
        sample["sonar_cm"] = sdata["distance_cm"] if age <= 60 else None
    except Exception:
        sample["sonar_cm"] = None

    # Token usage (cumulative since last restart)
    try:
        tdata = json.loads((state_dir / "token_usage.json").read_text())
        sample["tokens_in"] = tdata.get("input_tokens", 0)
        sample["tokens_out"] = tdata.get("output_tokens", 0)
    except Exception:
        sample["tokens_in"] = None
        sample["tokens_out"] = None

    # Ambient RMS + weather fields from awareness.json (single read)
    try:
        aw = json.loads((state_dir / "awareness.json").read_text())
        if not isinstance(aw, dict):
            aw = {}
        ambient = aw.get("ambient_sound") or {}
        sample["ambient_rms"] = ambient.get("rms") if isinstance(ambient, dict) else None
        weather = aw.get("weather")
        if isinstance(weather, dict):
            tc = weather.get("temp_C")
            sample["weather_temp_c"] = tc if tc is not None else weather.get("temp_c")
            sample["wind_kmh"] = weather.get("wind_kmh")
            sample["humidity_pct"] = weather.get("humidity_pct")
        else:
            sample["weather_temp_c"] = None
            sample["wind_kmh"] = None
            sample["humidity_pct"] = None
    except Exception:
        sample["ambient_rms"] = None
        sample["weather_temp_c"] = None
        sample["wind_kmh"] = None
        sample["humidity_pct"] = None

    # Salience + mood_val from latest thought (persona-scoped)
    _MOOD_VAL = {"peaceful": 1, "content": 2, "contemplative": 2, "curious": 3, "active": 4, "excited": 5}
    _thoughts_path = state_dir / (f"thoughts-{persona}.jsonl" if persona else "thoughts.jsonl")
    try:
        lines = _thoughts_path.read_text().strip().splitlines()
        last = json.loads(lines[-1]) if lines else {}
        sample["salience"] = last.get("salience")
        sample["mood_val"] = _MOOD_VAL.get((last.get("mood") or "").lower())
    except Exception:
        sample["salience"] = None
        sample["mood_val"] = None

    return sample


_FORWARD_FILL_FIELDS = ("weather_temp_c", "wind_kmh", "humidity_pct", "battery_pct")


def _history_worker() -> None:
    """Background daemon thread: appends a reading every 30s to _history_buf."""
    import time as _time

    while True:
        _time.sleep(30)
        try:
            _persona = (load_session().get("persona") or "").strip().lower()
            sample = _collect_history_sample(_public_state_dir(), _persona)
            # Forward-fill fields that change slowly (weather, battery) so sparklines
            # don't go blank between BOM/battery poll cycles.
            with _history_lock:
                if _history_buf:
                    prev = _history_buf[-1]
                    for f in _FORWARD_FILL_FIELDS:
                        if sample.get(f) is None and prev.get(f) is not None:
                            sample[f] = prev[f]
                _history_buf.append(sample)
        except Exception:
            pass


_history_thread = threading.Thread(
    target=_history_worker, daemon=True, name="history-worker"
)
_history_thread.start()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ToolRequest(BaseModel):
    tool: str
    params: Dict[str, Any] = Field(default_factory=dict)
    dry: Optional[bool] = None


class SessionPatch(BaseModel):
    listening: Optional[bool] = None
    confirm_motion_allowed: Optional[bool] = None
    wheels_on_blocks: Optional[bool] = None
    spark_quiet_mode: Optional[bool] = None
    mode: Optional[str] = None
    persona: Optional[str] = None  # "vixen", "gremlin", "spark", or "claude" (clears persona)


PATCHABLE_FIELDS = {"listening", "confirm_motion_allowed", "wheels_on_blocks", "mode", "persona", "spark_quiet_mode"}
VALID_PERSONAS = {"vixen", "gremlin", "spark", "claude", ""}  # "claude" or "" clears persona



class PinRequest(BaseModel):
    pin: str = Field(min_length=1, max_length=16)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FORCE_DRY = os.environ.get("PX_DRY", "0") == "1"
SYNC_TIMEOUT_DEFAULT = float(os.environ.get("PX_API_TIMEOUT", "30"))

# Tools that involve Ollama, network I/O, or multiple sequential subprocesses
SLOW_TOOLS = {
    "tool_chat", "tool_chat_vixen", "tool_describe_scene", "tool_wander",
    # SPARK tools: multiple subprocess calls (emote + voice + timer)
    "tool_routine", "tool_checkin", "tool_celebrate", "tool_transition",
    "tool_quiet", "tool_breathe", "tool_sensory_check", "tool_repair",
    # GWS tools: network I/O to Google APIs
    "tool_gws_calendar", "tool_gws_sheets_log",
}
SYNC_TIMEOUT_SLOW = float(os.environ.get("PX_API_TIMEOUT_SLOW", "120"))


def _resolve_dry(requested: Optional[bool]) -> bool:
    """FORCE_DRY override: server dry-run cannot be overridden remotely."""
    if FORCE_DRY:
        return True
    if requested is None:
        return FORCE_DRY
    return requested


def _public_state_dir() -> Path:
    """Resolve STATE_DIR respecting PX_STATE_DIR override (same as px-mind)."""
    return Path(os.environ.get("PX_STATE_DIR", str(PROJECT_ROOT / "state")))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/v1/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Public read-only endpoints (no auth required)
# ---------------------------------------------------------------------------

@app.get("/api/v1/public/status")
async def public_status() -> Dict[str, Any]:
    """Live SPARK status: persona, mood, last thought. No auth required."""
    session = load_session()
    persona = (session.get("persona") or "").strip().lower()

    state_dir = _public_state_dir()
    if persona:
        thoughts_path = state_dir / f"thoughts-{persona}.jsonl"
    else:
        thoughts_path = state_dir / "thoughts.jsonl"

    last = {}
    last_spoken = None
    last_spoken_ts = None
    try:
        lines = thoughts_path.read_text().strip().splitlines()
        if lines:
            last = json.loads(lines[-1])
        # Scan backwards for most recent thought that was actually spoken
        _silent = {None, "wait", "remember"}
        for line in reversed(lines):
            try:
                t = json.loads(line)
                if t.get("action") not in _silent:
                    last_spoken = t.get("thought")
                    last_spoken_ts = t.get("ts")
                    break
            except Exception:
                continue
    except Exception:
        pass

    return {
        "persona": persona or None,
        "mood": last.get("mood"),
        "last_thought": last.get("thought"),
        "last_spoken": last_spoken,
        "last_spoken_ts": last_spoken_ts,
        "last_action": last.get("action"),
        "salience": last.get("salience"),
        "ts": last.get("ts"),
        "listening": session.get("listening", False),
    }


@app.get("/api/v1/public/vitals")
async def public_vitals() -> Dict[str, Any]:
    """System vitals: CPU, RAM, temp, battery, disk. No auth required."""
    import time as _time

    cpu_pct = None
    ram_pct = None
    disk_pct = None
    try:
        import psutil
        cpu_pct = round(psutil.cpu_percent(interval=None), 1)
        vm = psutil.virtual_memory()
        ram_pct = round(vm.percent, 1)
        dk = psutil.disk_usage("/")
        disk_pct = round(dk.percent, 1)
    except Exception:
        pass

    cpu_temp_c = None
    try:
        raw = _THERMAL_ZONE.read_text().strip()
        cpu_temp_c = round(int(raw) / 1000.0, 1)
    except Exception:
        pass

    battery_pct = None
    try:
        data = json.loads((_public_state_dir() / "battery.json").read_text())
        battery_pct = data.get("pct")
    except Exception:
        pass

    tokens_in = tokens_out = None
    try:
        tdata = json.loads((_public_state_dir() / "token_usage.json").read_text())
        tokens_in = tdata.get("input_tokens", 0)
        tokens_out = tdata.get("output_tokens", 0)
    except Exception:
        pass

    return {
        "cpu_pct": cpu_pct,
        "ram_pct": ram_pct,
        "cpu_temp_c": cpu_temp_c,
        "battery_pct": battery_pct,
        "disk_pct": disk_pct,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "ts": utc_timestamp(),
    }


@app.get("/api/v1/public/sonar")
async def public_sonar() -> Dict[str, Any]:
    """Latest sonar reading from sonar_live.json. No auth required."""
    import time as _time

    sonar_path = _public_state_dir() / "sonar_live.json"
    try:
        data = json.loads(sonar_path.read_text())
        ts_float = float(data["ts"])
        age = round(_time.time() - ts_float)
        if age > 60:
            return {"sonar_cm": None, "age_seconds": None, "source": "unavailable"}
        return {
            "sonar_cm": data["distance_cm"],
            "age_seconds": age,
            "source": "sonar_live",
        }
    except Exception:
        return {"sonar_cm": None, "age_seconds": None, "source": "unavailable"}


@app.get("/api/v1/public/awareness")
async def public_awareness() -> Dict[str, Any]:
    """SPARK awareness snapshot: mode, Frigate, ambient, weather, time context. No auth."""
    try:
        parsed = json.loads((_public_state_dir() / "awareness.json").read_text())
        awareness = parsed if isinstance(parsed, dict) else {}
    except Exception:
        awareness = {}

    # frigate: absent/None → Frigate offline → person_present=None (hidden in UI)
    # frigate: dict → Frigate online → use its person_present value
    raw_frigate = awareness.get("frigate")
    if isinstance(raw_frigate, dict):
        person_present: Any = raw_frigate.get("person_present", False)
        frigate_score: Any = raw_frigate.get("score")
    else:
        person_present = None
        frigate_score = None

    ambient = awareness.get("ambient_sound") or {}

    raw_weather = awareness.get("weather")
    if isinstance(raw_weather, dict):
        weather_out: Any = {
            "temp_c": raw_weather.get("temp_C"),      # normalise uppercase → lowercase
            "wind_kmh": raw_weather.get("wind_kmh"),
            "humidity_pct": raw_weather.get("humidity_pct"),
            "summary": raw_weather.get("summary"),
        }
    else:
        weather_out = None

    return {
        "obi_mode": awareness.get("obi_mode"),
        "person_present": person_present,
        "frigate_score": frigate_score,
        "ambient_level": ambient.get("level"),
        "ambient_rms": ambient.get("rms"),
        "weather": weather_out,
        "minutes_since_speech": awareness.get("minutes_since_speech"),
        "time_period": awareness.get("time_period"),
        "ts": awareness.get("ts"),
    }


@app.get("/api/v1/public/history")
async def public_history() -> list:
    """Ring buffer of up to 60 vitals readings (~30 min history). No auth."""
    with _history_lock:
        return list(_history_buf)


@app.get("/api/v1/public/thoughts")
async def public_thoughts(limit: int = Query(default=12, ge=1, le=50)) -> list:
    """Recent SPARK thoughts (newest first). No auth required."""
    session = load_session()
    persona = (session.get("persona") or "").strip().lower()
    state_dir = _public_state_dir()
    thoughts_path = state_dir / (f"thoughts-{persona}.jsonl" if persona else "thoughts.jsonl")
    results = []
    try:
        lines = thoughts_path.read_text().strip().splitlines()
        for line in reversed(lines[-50:]):
            try:
                t = json.loads(line)
                results.append({
                    "thought": t.get("thought"),
                    "mood": t.get("mood"),
                    "ts": t.get("ts"),
                    "salience": t.get("salience"),
                    "action": t.get("action"),
                })
                if len(results) >= limit:
                    break
            except Exception:
                continue
    except Exception:
        pass
    return results


@app.post("/api/v1/pin/verify")
async def verify_pin(body: PinRequest) -> JSONResponse:
    """Verify the admin PIN. Public endpoint — no Bearer token required."""
    global _pin_attempts, _pin_lockout_until
    import time as _time
    now = _time.monotonic()
    with _pin_lock:
        if now < _pin_lockout_until:
            return JSONResponse(status_code=429, content={"verified": False, "error": "too many attempts"})

    submitted = body.pin.strip()
    if not submitted:
        return JSONResponse(status_code=200, content={"verified": False})

    expected = os.environ.get("PX_ADMIN_PIN", "").strip()
    if not expected:
        return JSONResponse(status_code=200, content={"verified": False})

    match = secrets.compare_digest(submitted, expected)
    if match:
        with _pin_lock:
            _pin_attempts = 0
            _pin_lockout_until = 0.0
        return JSONResponse(status_code=200, content={"verified": True})
    else:
        with _pin_lock:
            _pin_attempts += 1
            if _pin_attempts >= _PIN_MAX_ATTEMPTS:
                _pin_lockout_until = _time.monotonic() + _PIN_LOCKOUT_SECONDS
                _pin_attempts = 0
        return JSONResponse(status_code=200, content={"verified": False})


@app.get("/api/v1/tools", dependencies=[Depends(_verify_token)])
async def list_tools() -> Dict[str, List[str]]:
    return {"tools": sorted(ALLOWED_TOOLS)}


@app.get("/api/v1/session", dependencies=[Depends(_verify_token)])
async def get_session() -> Dict[str, Any]:
    return load_session()


@app.patch("/api/v1/session", dependencies=[Depends(_verify_token)])
async def patch_session(body: SessionPatch) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    for key in PATCHABLE_FIELDS:
        value = getattr(body, key, None)
        if value is not None:
            fields[key] = value
    if not fields:
        raise HTTPException(status_code=400, detail="no patchable fields provided")
    # Validate and normalize persona
    if "persona" in fields:
        p = (fields["persona"] or "").lower().strip()
        if p in ("claude", ""):
            fields["persona"] = None  # clear persona → default Claude
        elif p not in VALID_PERSONAS:
            raise HTTPException(status_code=400, detail=f"invalid persona: {p!r} (valid: vixen, gremlin, spark, claude)")
        else:
            fields["persona"] = p
    return update_session(fields=fields)


@app.post("/api/v1/session/history/clear", dependencies=[Depends(_verify_token)])
async def clear_session_history() -> Dict[str, Any]:
    """Wipe session conversation history. Keeps all other session fields intact."""
    session = load_session()
    count = len(session.get("history", []))
    update_session(fields={"history": []})
    return {"status": "ok", "cleared": count}


@app.post("/api/v1/tool", dependencies=[Depends(_verify_token)])
async def run_tool(body: ToolRequest) -> JSONResponse:
    dry = _resolve_dry(body.dry)
    action = {"tool": body.tool, "params": body.params}

    try:
        tool, env_overrides = validate_action(action)
    except VoiceLoopError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Async path for long-running tools
    if tool == "tool_wander":
        job_id = str(uuid.uuid4())
        _set_job(job_id, {"status": "running", "tool": tool, "dry": dry})

        async def _run_async() -> None:
            loop = asyncio.get_running_loop()
            try:
                rc, stdout, stderr = await loop.run_in_executor(
                    None, execute_tool, tool, env_overrides, dry
                )
                _set_job(job_id, {
                    "status": "complete",
                    "tool": tool,
                    "returncode": rc,
                    "dry": dry,
                    "stdout": stdout[-2048:],
                    "stderr": stderr[-1024:],
                })
            except VoiceLoopError as exc:
                _set_job(job_id, {"status": "error", "tool": tool, "error": str(exc)})
            except Exception as exc:
                _set_job(job_id, {"status": "error", "tool": tool, "error": f"{type(exc).__name__}: {exc}"})

        asyncio.create_task(_run_async())
        return JSONResponse(
            status_code=202,
            content={"status": "accepted", "job_id": job_id, "poll": f"/api/v1/jobs/{job_id}"},
        )

    # Synchronous path — slow tools (Ollama, vision, wander) get longer timeout
    timeout = SYNC_TIMEOUT_SLOW if tool in SLOW_TOOLS else SYNC_TIMEOUT_DEFAULT
    loop = asyncio.get_running_loop()
    try:
        rc, stdout, stderr = await asyncio.wait_for(
            loop.run_in_executor(None, execute_tool, tool, env_overrides, dry),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail=f"tool {tool} timed out after {timeout}s")
    except VoiceLoopError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Motion blocked returns 403
    if rc == 2:
        return JSONResponse(
            status_code=403,
            content={"status": "blocked", "detail": "motion not confirmed safe"},
        )

    return JSONResponse(
        status_code=200,
        content={
            "status": "ok" if rc == 0 else "error",
            "returncode": rc,
            "tool": tool,
            "dry": dry,
            "stdout": stdout[-2048:],
            "stderr": stderr[-1024:],
        },
    )


@app.get("/api/v1/jobs/{job_id}", dependencies=[Depends(_verify_token)])
async def get_job(job_id: str) -> Dict[str, Any]:
    job = _get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


# ---------------------------------------------------------------------------
# Chat endpoint — one voice-loop turn via LLM
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    text: str
    dry: Optional[bool] = None


_DEFAULT_PROMPT_PATH = PROJECT_ROOT / "docs" / "prompts" / "spark-voice-system.md"
_CODEX_CMD = os.environ.get(
    "CODEX_CHAT_CMD",
    "codex exec --model gpt-5-codex --full-auto -",
)


def _do_chat_turn(text: str, dry: bool) -> Dict[str, Any]:
    """Run one LLM turn: build prompt → LLM → parse action → execute tool."""
    session = load_session()
    # Pick system prompt based on active persona
    active_persona = (session.get("persona") or "").lower().strip()
    prompt_path = PERSONA_PROMPTS.get(active_persona, _DEFAULT_PROMPT_PATH)
    if not prompt_path.exists():
        prompt_path = _DEFAULT_PROMPT_PATH
    if not prompt_path.exists():
        return {"status": "error", "error": "system prompt not found"}

    system_prompt = read_prompt(prompt_path)
    prompt = build_model_prompt(system_prompt, session, text)

    codex_cmd = os.environ.get("CODEX_CHAT_CMD", _CODEX_CMD)
    rc, stdout, stderr = run_codex(codex_cmd, prompt)
    if rc != 0:
        return {"status": "error", "error": f"LLM error (rc={rc}): {stderr.strip()[-500:]}"}

    action = extract_action(stdout)
    if not action:
        return {"status": "error", "error": "LLM returned no valid JSON action", "raw": stdout[-500:]}

    try:
        tool, env_overrides = validate_action(action)
    except VoiceLoopError as exc:
        return {"status": "error", "error": str(exc), "action": action}

    t_rc, t_stdout, t_stderr = execute_tool(tool, env_overrides, dry)
    return {
        "status": "ok" if t_rc == 0 else "error",
        "tool": tool,
        "action": action,
        "tool_output": t_stdout[-2048:],
        "dry": dry,
    }


@app.post("/api/v1/chat", dependencies=[Depends(_verify_token)])
async def chat(body: ChatRequest) -> JSONResponse:
    """Send a text message; SPARK picks a tool and executes it."""
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="text must not be empty")
    dry = _resolve_dry(body.dry)
    loop = asyncio.get_running_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _do_chat_turn, body.text.strip(), dry),
            timeout=SYNC_TIMEOUT_SLOW,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="chat turn timed out")
    return JSONResponse(status_code=200 if result.get("status") == "ok" else 500, content=result)


# ---------------------------------------------------------------------------
# Service management — restart/stop/start allowlisted systemd services
# ---------------------------------------------------------------------------

# Public services endpoint queries these five explicitly. px-battery-poll is not
# in _MANAGED_SERVICES (the auth'd endpoint doesn't control it) but the public
# dashboard needs to show its status.
_PUBLIC_SERVICES = frozenset({
    "px-mind", "px-alive", "px-wake-listen", "px-battery-poll", "px-api-server"
})
_PUBLIC_SERVICE_STATES = frozenset({"active", "activating", "failed", "inactive", "unknown"})


def _get_public_service_status(service: str) -> tuple:
    """Returns (service_name, normalised_status_string)."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service],
            capture_output=True, text=True, timeout=5,
        )
        state = result.stdout.strip()
        if state not in _PUBLIC_SERVICE_STATES:
            state = "unknown"
        return (service, state)
    except Exception:
        return (service, "unknown")


@app.get("/api/v1/public/services")
async def public_services_status() -> Dict[str, str]:
    """Public service status dict (no auth). Shape: {name: status_string}.
    IMPORTANT: does not modify /api/v1/services — different shape, used by web UI.
    """
    loop = asyncio.get_running_loop()
    pairs = await asyncio.gather(*[
        loop.run_in_executor(None, _get_public_service_status, svc)
        for svc in sorted(_PUBLIC_SERVICES)
    ])
    return dict(pairs)


# Only these services can be controlled via the API (prevents privilege abuse)
_MANAGED_SERVICES = {"px-alive", "px-wake-listen", "px-mind", "px-api-server"}


def _run_systemctl(action: str, service: str) -> Dict[str, Any]:
    """Run systemctl {action} {service}. Returns status dict."""
    try:
        result = subprocess.run(
            ["sudo", "systemctl", action, service],
            capture_output=True, text=True, timeout=15,
        )
        return {
            "status": "ok" if result.returncode == 0 else "error",
            "service": service,
            "action": action,
            "returncode": result.returncode,
            "stdout": result.stdout.strip()[-500:],
            "stderr": result.stderr.strip()[-500:],
        }
    except subprocess.TimeoutExpired:
        return {"status": "error", "service": service, "action": action, "error": "timeout"}
    except Exception as exc:
        return {"status": "error", "service": service, "action": action, "error": str(exc)}


def _get_service_status(service: str) -> Dict[str, Any]:
    """Get systemd service status. Returns simplified state dict."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service],
            capture_output=True, text=True, timeout=5,
        )
        active = result.stdout.strip()
        result2 = subprocess.run(
            ["systemctl", "is-enabled", service],
            capture_output=True, text=True, timeout=5,
        )
        enabled = result2.stdout.strip()
        return {"service": service, "active": active, "enabled": enabled}
    except Exception as exc:
        return {"service": service, "active": "unknown", "enabled": "unknown", "error": str(exc)}


@app.get("/api/v1/services", dependencies=[Depends(_verify_token)])
async def list_services() -> JSONResponse:
    """Get status of all managed services."""
    loop = asyncio.get_running_loop()
    statuses = await asyncio.gather(*[
        loop.run_in_executor(None, _get_service_status, svc)
        for svc in sorted(_MANAGED_SERVICES)
    ])
    return JSONResponse(content={"services": list(statuses)})


@app.post("/api/v1/services/{service}/{action}", dependencies=[Depends(_verify_token)])
async def control_service(service: str, action: str) -> JSONResponse:
    """Restart/stop/start a managed service. Action: restart | stop | start."""
    if service not in _MANAGED_SERVICES:
        raise HTTPException(status_code=400, detail=f"Service '{service}' not managed. Allowed: {sorted(_MANAGED_SERVICES)}")
    if action not in ("restart", "stop", "start", "status"):
        raise HTTPException(status_code=400, detail="action must be: restart, stop, start, status")
    loop = asyncio.get_running_loop()
    if action == "status":
        result = await loop.run_in_executor(None, _get_service_status, service)
    else:
        result = await loop.run_in_executor(None, _run_systemctl, action, service)
    return JSONResponse(
        status_code=200 if result.get("status") == "ok" else 500,
        content=result,
    )


# ---------------------------------------------------------------------------
# Device control — reboot / shutdown
# ---------------------------------------------------------------------------

_DEVICE_ACTIONS: dict[str, list[str]] = {
    "reboot": ["sudo", "/usr/bin/systemctl", "reboot"],
    "shutdown": ["sudo", "/sbin/shutdown", "-h", "now"],
}

_pin_lock = threading.Lock()
_pin_attempts = 0
_pin_lockout_until = 0.0
_PIN_MAX_ATTEMPTS = 5
_PIN_LOCKOUT_SECONDS = 30


@app.post("/api/v1/device/{action}", dependencies=[Depends(_verify_token)])
async def device_control(action: str) -> JSONResponse:
    """Reboot or shut down the host device. Action: reboot | shutdown."""
    if action not in _DEVICE_ACTIONS:
        raise HTTPException(status_code=400, detail=f"unknown action: {action}")
    try:
        subprocess.Popen(_DEVICE_ACTIONS[action])
    except Exception as exc:
        return JSONResponse(status_code=500, content={"status": "error", "error": str(exc)})
    return JSONResponse(status_code=200, content={"status": "ok", "action": action})


# ---------------------------------------------------------------------------
# Log tailing endpoint
# ---------------------------------------------------------------------------

_LOG_ALLOWLIST = {
    "px-mind", "px-wake-listen", "px-alive",
    "tool-voice", "tool-describe_scene",
}


@app.get("/api/v1/logs/{service}", dependencies=[Depends(_verify_token)])
async def tail_log(service: str, lines: int = Query(default=100, ge=1, le=2000)) -> JSONResponse:
    """Return last N lines from a named log file."""
    if service not in _LOG_ALLOWLIST:
        raise HTTPException(status_code=400, detail=f"unknown log: {service}")
    log_dir = Path(os.environ.get("LOG_DIR", PROJECT_ROOT / "logs"))
    log_path = log_dir / f"{service}.log"
    if not log_path.exists():
        return JSONResponse(content={"lines": [], "service": service})
    text = log_path.read_text(errors="replace")
    tail = text.splitlines()[-lines:]
    return JSONResponse(content={"lines": tail, "service": service})


# ---------------------------------------------------------------------------
# Web UI — single-page SPARK dashboard served at /
# ---------------------------------------------------------------------------

_HTML_UI = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>SPARK</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
:root {
  --bg:#12111a; --surface:#1e1c2e; --surface2:#2a2840;
  --spark:#00d4aa; --spark-dim:#00937a; --text:#f0eeff; --muted:#8884aa;
  --danger:#e05c5c; --orange:#f5a623; --yellow:#f7d547;
  --purple:#9b7be8; --blue:#5b9cf6;
  --tab-h:64px; --radius:16px;
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{height:100%;background:var(--bg);color:var(--text);font-family:'Nunito',sans-serif;overflow:hidden}
#tab-bar{position:fixed;bottom:0;left:0;right:0;height:var(--tab-h);background:var(--surface);border-top:1px solid var(--surface2);display:flex;z-index:100}
.tab-btn{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;border:none;background:none;color:var(--muted);font-family:inherit;font-size:11px;font-weight:600;cursor:pointer;transition:color .15s;padding:4px 0}
.tab-btn .ti{font-size:22px;line-height:1}
.tab-btn.active{color:var(--spark)}
#app{position:fixed;top:0;left:0;right:0;bottom:var(--tab-h);overflow:hidden}
.tab-panel{display:none;height:100%;overflow-y:auto;flex-direction:column}
.tab-panel.active{display:flex}
.btn{display:flex;align-items:center;justify-content:center;gap:8px;padding:14px 20px;border-radius:var(--radius);border:none;font-family:inherit;font-size:15px;font-weight:700;cursor:pointer;transition:opacity .1s,transform .1s;min-height:56px;width:100%}
.btn:active{opacity:.8;transform:scale(.97)}
.btn-spark{background:var(--spark);color:#0a1a15}
.btn-orange{background:var(--orange);color:#1a0f00}
.btn-yellow{background:var(--yellow);color:#1a1500}
.btn-purple{background:var(--purple);color:#0e0820}
.btn-blue{background:var(--blue);color:#040e20}
.btn-muted{background:var(--surface2);color:var(--text)}
.btn-danger{background:var(--danger);color:#fff}
.sec-hdr{padding:10px 16px 6px;font-size:12px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
@keyframes pulse-ring{0%,100%{box-shadow:0 0 0 0 rgba(0,212,170,.4)}50%{box-shadow:0 0 0 8px rgba(0,212,170,0)}}
@keyframes ring-listen{from{box-shadow:0 0 10px rgba(0,212,170,.6)}to{box-shadow:0 0 40px rgba(0,212,170,.9)}}
.spark-stat{text-align:center;background:var(--surface2);padding:10px 16px;border-radius:12px;font-size:14px;font-weight:800}
.stat-lbl{font-size:10px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.atab-btn{flex:1;padding:12px 4px;border:none;background:none;color:var(--muted);font-family:inherit;font-size:12px;font-weight:700;cursor:pointer;border-bottom:2px solid transparent}
.atab-btn.active{color:var(--spark);border-bottom-color:var(--spark)}
.apanel{display:none}.apanel.active{display:block}
</style>
</head>
<body>
<div id="app">
  <div id="panel-chat"    class="tab-panel active">
    <div id="av-bar" style="padding:12px 16px 8px;display:flex;align-items:center;gap:12px;background:var(--surface);border-bottom:1px solid var(--surface2);flex-shrink:0">
      <div id="av-ring" style="width:52px;height:52px;border-radius:50%;border:3px solid var(--spark);display:flex;align-items:center;justify-content:center;font-size:28px;flex-shrink:0;animation:pulse-ring 2s ease-in-out infinite">&#x1F914;</div>
      <div><div style="font-size:13px;font-weight:800;color:var(--spark)">SPARK</div><div id="av-mood" style="font-size:11px;color:var(--muted)">curious &middot; ready</div></div>
    </div>
    <div id="msgs" style="flex:1;overflow-y:auto;padding:12px 16px;display:flex;flex-direction:column;gap:10px"></div>
    <div style="padding:10px 12px;background:var(--surface);border-top:1px solid var(--surface2);flex-shrink:0;display:flex;gap:8px">
      <input id="ci" type="text" placeholder="Talk to SPARK&#x2026;" style="flex:1;background:var(--surface2);border:2px solid transparent;border-radius:24px;padding:12px 18px;font-family:inherit;font-size:15px;color:var(--text);outline:none" onfocus="this.style.borderColor='var(--spark)'" onblur="this.style.borderColor='transparent'" onkeydown="if(event.key==='Enter')sendChat()">
      <button onclick="sendChat()" id="sbtn" class="btn btn-spark" style="width:auto;padding:12px 20px;border-radius:24px;flex-shrink:0">Send</button>
    </div>
  </div>
  <div id="panel-actions" class="tab-panel">
    <div style="padding:12px 16px 80px;display:flex;flex-direction:column;gap:6px">
      <div class="sec-hdr" style="color:var(--spark)">&#x1F9D8; I need help</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
        <button class="btn btn-spark" onclick="doTool('tool_breathe',{rounds:2})">&#x1F4A8; Breathe with me</button>
        <button class="btn btn-spark" onclick="doTool('tool_quiet',{mode:'on'})">&#x1F92B; Go quiet</button>
        <button class="btn btn-muted"  onclick="doTool('tool_quiet',{mode:'off'})">&#x2705; End quiet</button>
        <button class="btn btn-spark" onclick="doTool('tool_sensory_check',{})">&#x1F9E0; Body check</button>
        <button class="btn btn-spark" onclick="doTool('tool_dopamine_menu',{energy:'medium'})">&#x1F3B2; What can I do?</button>
        <button class="btn btn-spark" onclick="doTool('tool_repair',{})">&#x1F91D; Make things better</button>
      </div>
      <div class="sec-hdr" style="color:var(--yellow)">&#x1F49B; How are we doing?</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
        <button class="btn btn-yellow" onclick="doTool('tool_checkin',{})">&#x1F60A; How are you?</button>
        <button class="btn btn-yellow" onclick="doTool('tool_celebrate',{})">&#x1F389; Celebrate!</button>
        <button class="btn btn-yellow" onclick="doTool('tool_gws_calendar',{action:'today'})">&#x1F4C5; What&apos;s today?</button>
        <button class="btn btn-yellow" onclick="doTool('tool_gws_calendar',{action:'next'})">&#x27A1;&#xFE0F; Next thing</button>
        <button class="btn btn-yellow" onclick="doTool('tool_time',{})">&#x1F550; What time is it?</button>
        <button class="btn btn-yellow" onclick="doTool('tool_weather',{})">&#x26C5; Weather</button>
      </div>
      <div class="sec-hdr" style="color:var(--blue)">&#x1F50A; Sounds &amp; memory</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
        <button class="btn btn-blue" onclick="doTool('tool_play_sound',{sound:'happy'})">&#x1F3B5; Play a sound</button>
        <button class="btn btn-blue" onclick="promptTimer()">&#x23F1;&#xFE0F; Set a timer</button>
        <button class="btn btn-blue" onclick="promptRemember()">&#x1F4AD; Remember this</button>
        <button class="btn btn-blue" onclick="doTool('tool_recall',{})">&#x1F50D; What do you remember?</button>
        <button class="btn btn-blue" style="grid-column:span 2" onclick="doPhoto()">&#x1F4F8; Take a photo!</button>
      </div>
      <div class="sec-hdr" style="color:var(--orange)">&#x1F4CB; Our routines</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
        <button class="btn btn-orange" onclick="doTool('tool_routine',{action:'start',routine:'morning'})">&#x1F305; Morning</button>
        <button class="btn btn-orange" onclick="doTool('tool_routine',{action:'start',routine:'homework'})">&#x1F4DA; Homework</button>
        <button class="btn btn-orange" onclick="doTool('tool_routine',{action:'start',routine:'bedtime'})">&#x1F319; Bedtime</button>
        <button class="btn btn-orange" onclick="doTool('tool_routine',{action:'next'})">&#x27A1;&#xFE0F; Next step</button>
        <button class="btn btn-orange" onclick="doTool('tool_routine',{action:'status'})">&#x2753; What&apos;s the plan?</button>
        <button class="btn btn-muted"  onclick="doTool('tool_routine',{action:'stop'})">&#x23F9;&#xFE0F; Stop routine</button>
      </div>
      <div class="sec-hdr" style="color:var(--orange)">&#x23F0; Transitions</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
        <button class="btn btn-orange" onclick="doTool('tool_transition',{minutes:5})">&#x23F0; 5 min warning</button>
        <button class="btn btn-orange" onclick="doTool('tool_transition',{minutes:2})">&#x23F0; 2 min warning</button>
        <button class="btn btn-orange" onclick="doTool('tool_transition',{action:'arrived'})">&#x2705; I&apos;m here now</button>
      </div>
      <div class="sec-hdr" style="color:var(--purple)">&#x1F916; Move SPARK!</div>
      <div style="background:var(--surface2);border-radius:var(--radius);padding:16px;margin-bottom:8px">
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;grid-template-rows:auto auto auto;gap:8px;max-width:240px;margin:0 auto 12px">
          <div></div>
          <button class="btn btn-purple" style="min-height:64px;font-size:24px" onpointerdown="rcStart('forward',0)" onpointerup="rcStop()" onpointerleave="rcStop()">&#x25B2;</button>
          <div></div>
          <button class="btn btn-purple" style="min-height:64px;font-size:24px" onpointerdown="rcStart('forward',-28)" onpointerup="rcStop()" onpointerleave="rcStop()">&#x25C4;</button>
          <button class="btn btn-danger" style="min-height:64px;font-size:20px;font-weight:900" onclick="doTool('tool_stop',{})">&#x26D4;</button>
          <button class="btn btn-purple" style="min-height:64px;font-size:24px" onpointerdown="rcStart('forward',28)" onpointerup="rcStop()" onpointerleave="rcStop()">&#x25BA;</button>
          <div></div>
          <button class="btn btn-purple" style="min-height:64px;font-size:24px" onpointerdown="rcStart('backward',0)" onpointerup="rcStop()" onpointerleave="rcStop()">&#x25BC;</button>
          <div></div>
        </div>
        <div style="display:flex;align-items:center;gap:10px;max-width:240px;margin:0 auto">
          <span style="font-size:12px;color:var(--muted);white-space:nowrap">Speed</span>
          <input type="range" id="rc-speed" min="10" max="50" value="30" style="flex:1;accent-color:var(--purple)" oninput="document.getElementById('rc-spd-val').textContent=this.value">
          <span id="rc-spd-val" style="font-size:13px;font-weight:800;color:var(--purple);min-width:28px">30</span>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
        <button class="btn btn-purple" onclick="doTool('tool_circle',{speed:30,duration:4})">&#x2B55; Spin in a circle</button>
        <button class="btn btn-purple" onclick="doTool('tool_figure8',{speed:25,duration:6})">&#x221E; Figure-8</button>
        <button class="btn btn-purple" onclick="doTool('tool_wander',{})">&#x1F3B2; Explore the room</button>
        <button class="btn btn-purple" onclick="doTool('tool_perform',{performance:'dance'})">&#x1F57A; Do a trick</button>
        <button class="btn btn-purple" onclick="doTool('tool_look',{direction:'left'})">&#x1F448; Look left</button>
        <button class="btn btn-purple" onclick="doTool('tool_look',{direction:'right'})">&#x1F449; Look right</button>
        <button class="btn btn-purple" onclick="doTool('tool_look',{direction:'up'})">&#x261D;&#xFE0F; Look up</button>
        <button class="btn btn-purple" onclick="doTool('tool_emote',{emotion:'happy'})">&#x1F604; Happy face</button>
        <button class="btn btn-purple" onclick="doTool('tool_describe_scene',{})">&#x1F4F8; What do you see?</button>
        <button class="btn btn-purple" onclick="doTool('tool_sonar',{})">&#x1F4E1; How far away?</button>
      </div>
    </div>
  </div>
  <div id="panel-spark"   class="tab-panel">
    <div style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:24px;gap:20px">
      <div id="f-status" style="font-size:13px;font-weight:800;color:var(--muted);letter-spacing:.1em;text-transform:uppercase">idle</div>
      <div id="f-ring" style="width:140px;height:140px;border-radius:50%;border:5px solid var(--spark);display:flex;align-items:center;justify-content:center;font-size:72px;box-shadow:0 0 30px rgba(0,212,170,.3);transition:border-color .5s,box-shadow .5s;animation:pulse-ring 2s ease-in-out infinite">&#x1F914;</div>
      <div style="background:var(--surface2);border-radius:var(--radius);padding:18px 20px;max-width:340px;width:100%;border-left:4px solid var(--spark)">
        <div style="font-size:11px;font-weight:800;color:var(--spark);margin-bottom:8px;letter-spacing:.05em">SPARK IS THINKING</div>
        <div id="f-thought" style="font-size:15px;line-height:1.6;font-style:italic">Loading&#x2026;</div>
      </div>
      <div style="display:flex;gap:12px;flex-wrap:wrap;justify-content:center">
        <div class="spark-stat"><span id="st-mood">&#x2013;</span><br><span class="stat-lbl">mood</span></div>
        <div class="spark-stat"><span id="st-sonar">&#x2013;</span><br><span class="stat-lbl">sonar</span></div>
        <div class="spark-stat"><span id="st-period">&#x2013;</span><br><span class="stat-lbl">time</span></div>
        <div class="spark-stat"><span id="st-persona">&#x2013;</span><br><span class="stat-lbl">persona</span></div>
      </div>
    </div>
  </div>
  <div id="panel-admin"   class="tab-panel">
    <div id="pin-gate" style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:40px 24px;gap:16px">
      <div style="font-size:48px">&#x1F527;</div>
      <div style="font-size:20px;font-weight:800">Adrian&apos;s Panel</div>
      <div style="font-size:14px;color:var(--muted);text-align:center">Enter your PIN to continue.</div>
      <form onsubmit="subPin();return false" style="display:contents">
      <input id="pin-inp" type="password" inputmode="numeric" maxlength="8" placeholder="PIN"
        style="font-size:28px;letter-spacing:.3em;text-align:center;background:var(--surface2);border:2px solid var(--surface2);border-radius:var(--radius);padding:14px 20px;width:180px;color:var(--text);font-family:inherit;outline:none"
        onfocus="this.style.borderColor='var(--spark)'" onblur="this.style.borderColor='var(--surface2)'"
        onkeydown="if(event.key==='Enter')subPin()">
      <button class="btn btn-spark" style="width:180px" type="submit">Unlock</button>
      </form>
      <div id="pin-err" style="color:var(--danger);font-size:13px;display:none">Wrong PIN &mdash; try again</div>
    </div>
    <div id="admin-body" style="display:none;flex-direction:column;height:100%">
      <div style="display:flex;background:var(--surface);border-bottom:1px solid var(--surface2);flex-shrink:0">
        <button class="atab-btn active" id="at-svc"      onclick="swA('svc')">&#x2699;&#xFE0F; Services</button>
        <button class="atab-btn"        id="at-tools"    onclick="swA('tools')">&#x1F6E0; Tools</button>
        <button class="atab-btn"        id="at-logs"     onclick="swA('logs')">&#x1F4CB; Logs</button>
        <button class="atab-btn"        id="at-parental" onclick="swA('parental')">&#x1F46A; Parental</button>
      </div>
      <div id="ap-svc" class="apanel active" style="padding:16px;overflow-y:auto">
        <div id="svc-list" style="display:flex;flex-direction:column;gap:10px;margin-bottom:16px"></div>
        <div class="sec-hdr" style="color:var(--danger)">&#x26A0;&#xFE0F; Device</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px">
          <button class="btn btn-danger" onclick="confirmDev('reboot')">&#x1F504; Reboot Pi</button>
          <button class="btn btn-danger" onclick="confirmDev('shutdown')">&#x26D4; Shutdown Pi</button>
        </div>
      </div>
      <div id="ap-tools" class="apanel" style="padding:16px;overflow-y:auto">
        <div class="sec-hdr" style="margin-bottom:8px">Raw Tool Runner</div>
        <select id="tool-sel" style="width:100%;background:var(--surface2);border:none;border-radius:8px;padding:10px 14px;color:var(--text);font-family:inherit;font-size:14px;margin-bottom:8px" onchange="this.value?document.getElementById('tool-params').style.display='block':document.getElementById('tool-params').style.display='none'"></select>
        <div id="tool-params" style="display:none">
          <textarea id="tool-prms" placeholder='{&quot;key&quot;:&quot;value&quot;}' style="width:100%;background:var(--surface2);border:none;border-radius:8px;padding:10px 14px;color:var(--text);font-family:monospace;font-size:13px;min-height:80px;margin-bottom:8px;resize:vertical"></textarea>
          <button class="btn btn-spark" style="margin-bottom:12px" onclick="runAdminTool()">&#x25B6; Run</button>
        </div>
        <pre id="tool-out" style="background:var(--surface2);border-radius:8px;padding:12px;font-family:monospace;font-size:12px;color:var(--spark);white-space:pre-wrap;min-height:60px;overflow-y:auto;max-height:300px"></pre>
      </div>
      <div id="ap-logs" class="apanel" style="padding:16px;overflow-y:auto">
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">
          <button class="btn btn-muted" style="min-height:36px;padding:6px 12px;font-size:12px;width:auto" onclick="loadLog('px-mind')">&#x1F9E0; Mind</button>
          <button class="btn btn-muted" style="min-height:36px;padding:6px 12px;font-size:12px;width:auto" onclick="loadLog('px-wake-listen')">&#x1F442; Wake</button>
          <button class="btn btn-muted" style="min-height:36px;padding:6px 12px;font-size:12px;width:auto" onclick="loadLog('px-alive')">&#x1F916; Alive</button>
          <button class="btn btn-muted" style="min-height:36px;padding:6px 12px;font-size:12px;width:auto" onclick="loadLog('tool-voice')">&#x1F50A; Voice</button>
        </div>
        <pre id="log-out" style="background:var(--surface2);border-radius:8px;padding:12px;font-family:monospace;font-size:11px;white-space:pre-wrap;overflow-y:auto;max-height:calc(100vh - 220px);line-height:1.5"></pre>
      </div>
      <div id="ap-parental" class="apanel" style="padding:16px;overflow-y:auto;display:flex;flex-direction:column;gap:12px">
        <div class="sec-hdr">Motion</div>
        <button class="btn btn-muted" id="btn-motion" onclick="toggleMotion()">Loading&#x2026;</button>
        <div class="sec-hdr">Quiet mode</div>
        <button class="btn btn-muted" id="btn-quiet" onclick="toggleQuiet()">Loading&#x2026;</button>
        <div class="sec-hdr">Persona</div>
        <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px">
          <button class="btn btn-muted" onclick="setPersona('spark')">&#x1F31F; spark</button>
          <button class="btn btn-muted" onclick="setPersona('gremlin')">&#x1F479; gremlin</button>
          <button class="btn btn-muted" onclick="setPersona('vixen')">&#x1F98A; vixen</button>
          <button class="btn btn-muted" onclick="setPersona('')">&#x25CB; none</button>
        </div>
        <div class="sec-hdr">Session</div>
        <button class="btn btn-danger" onclick="clearHistory()">&#x1F5D1; Clear Session History</button>
        <div class="sec-hdr">Log an event</div>
        <input id="sh-mood" placeholder="Mood" style="background:var(--surface2);border:none;border-radius:8px;padding:10px 14px;color:var(--text);font-family:inherit;font-size:14px">
        <input id="sh-detail" placeholder="What happened?" style="background:var(--surface2);border:none;border-radius:8px;padding:10px 14px;color:var(--text);font-family:inherit;font-size:14px">
        <button class="btn btn-blue" onclick="logEvt()">&#x1F4DD; Log to sheets</button>
      </div>
    </div>
  </div>
</div>
<nav id="tab-bar">
  <button class="tab-btn active" id="tab-chat"    onclick="sw('chat')"><span class="ti">&#x1F4AC;</span>Chat</button>
  <button class="tab-btn"        id="tab-actions" onclick="sw('actions')"><span class="ti">&#x26A1;</span>Actions</button>
  <button class="tab-btn"        id="tab-spark"   onclick="sw('spark')"><span class="ti">&#x1F916;</span>SPARK</button>
  <button class="tab-btn"        id="tab-admin"   onclick="sw('admin')"><span class="ti">&#x1F527;&#x1F512;</span>Adrian</button>
</nav>
<input type="hidden" id="tok" value="__SPARK_TOKEN__">
<script>
const tok=()=>document.getElementById('tok').value;
const api=(path,opts={})=>fetch(path,{headers:{'Authorization':'Bearer '+tok(),'Content-Type':'application/json',...(opts.headers||{})}, ...opts}).then(r=>r.json());
let _pinOk=false;
function showPin(){
  if(_pinOk)return;
  document.getElementById('pin-gate').style.display='flex';
  document.getElementById('admin-body').style.display='none';
}
async function subPin(){
  const pin=document.getElementById('pin-inp').value;
  try{
    const r=await fetch('/api/v1/pin/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pin})}).then(x=>x.json());
    if(r.verified){
      _pinOk=true;
      document.getElementById('pin-gate').style.display='none';
      document.getElementById('admin-body').style.display='flex';
      loadSvcs();loadParental();initTools();
    } else {
      document.getElementById('pin-err').style.display='block';
      document.getElementById('pin-inp').value='';
    }
  } catch(e){document.getElementById('pin-err').style.display='block';}
}
function swA(name){
  document.querySelectorAll('.atab-btn').forEach(b=>b.classList.remove('active'));
  document.querySelectorAll('.apanel').forEach(p=>p.classList.remove('active'));
  document.getElementById('at-'+name).classList.add('active');
  document.getElementById('ap-'+name).classList.add('active');
  if(name==='logs')loadLog('px-mind');
}
async function loadSvcs(){
  try{
    const r=await api('/api/v1/services');
    const list=document.getElementById('svc-list');list.textContent='';
    (r.services||[]).forEach(s=>{
      const on=s.active==='active';
      const n=s.service.replace('px-','');
      const ico={'alive':'\U0001F916','mind':'\U0001F9E0','wake-listen':'\U0001F442','api-server':'\U0001F310'}[n]||'\u2699\uFE0F';
      const row=document.createElement('div');
      row.style.cssText='display:flex;align-items:center;gap:10px;background:var(--surface2);padding:12px 14px;border-radius:12px';
      const dot=document.createElement('span');dot.style.cssText='font-size:10px;color:'+(on?'var(--spark)':'var(--danger)');dot.textContent='\u25CF';
      const ic=document.createElement('span');ic.style.cssText='font-size:18px';ic.textContent=ico;
      const nm=document.createElement('span');nm.style.cssText='flex:1;font-weight:700;font-size:14px';nm.textContent=n;
      const rb=document.createElement('button');rb.className='btn btn-muted';rb.style.cssText='min-height:36px;padding:6px 12px;font-size:12px;width:auto';rb.textContent='\u21BA Restart';rb.onclick=()=>svcAct(s.service,'restart');
      const tb=document.createElement('button');tb.className='btn '+(on?'btn-danger':'btn-spark');tb.style.cssText='min-height:36px;padding:6px 12px;font-size:12px;width:auto';tb.textContent=on?'\u25A0 Stop':'\u25B6 Start';tb.onclick=()=>svcAct(s.service,on?'stop':'start');
      row.appendChild(dot);row.appendChild(ic);row.appendChild(nm);row.appendChild(rb);row.appendChild(tb);
      list.appendChild(row);
    });
  } catch(e){}
}
async function svcAct(svc,act){try{await api('/api/v1/services/'+svc+'/'+act,{method:'POST'});}catch(e){}setTimeout(loadSvcs,1500);}
async function confirmDev(act){if(confirm('Really '+act+' the Pi?'))try{await api('/api/v1/device/'+act,{method:'POST'});}catch(e){}}
async function loadParental(){
  try{
    const s=await api('/api/v1/session');
    const bm=document.getElementById('btn-motion');
    bm.textContent=s.confirm_motion_allowed?'\u2705 Motion: ON':'\U0001F6AB Motion: OFF';
    bm.className='btn '+(s.confirm_motion_allowed?'btn-spark':'btn-danger');
    const bq=document.getElementById('btn-quiet');
    bq.textContent=s.spark_quiet_mode?'\U0001F92B Quiet: ON':'\U0001F4AC Quiet: OFF';
    bq.className='btn '+(s.spark_quiet_mode?'btn-danger':'btn-muted');
  } catch(e){}
}
async function toggleMotion(){try{const s=await api('/api/v1/session');await api('/api/v1/session',{method:'PATCH',body:JSON.stringify({confirm_motion_allowed:!s.confirm_motion_allowed})});}catch(e){}loadParental();}
async function toggleQuiet(){try{const s=await api('/api/v1/session');await api('/api/v1/session',{method:'PATCH',body:JSON.stringify({spark_quiet_mode:!s.spark_quiet_mode})});}catch(e){}loadParental();}
async function setPersona(p){try{await api('/api/v1/session',{method:'PATCH',body:JSON.stringify({persona:p})});}catch(e){}}
async function clearHistory(){if(!confirm('Wipe all session history? SPARK will stop ruminating on old phrases.'))return;const r=await api('/api/v1/session/history/clear',{method:'POST'});chat('History cleared ('+r.cleared+' entries removed).');}
async function logEvt(){
  const mood=document.getElementById('sh-mood').value;
  const detail=document.getElementById('sh-detail').value;
  if(!detail)return;
  doTool('tool_gws_sheets_log',{event_type:'note',detail,mood});
  document.getElementById('sh-mood').value='';
  document.getElementById('sh-detail').value='';
}
async function initTools(){
  try{
    const r=await api('/api/v1/tools');
    const sel=document.getElementById('tool-sel');sel.textContent='';
    const def=document.createElement('option');def.value='';def.textContent='\u2014 select a tool \u2014';sel.appendChild(def);
    (r.tools||[]).forEach(t=>{const o=document.createElement('option');o.value=t;o.textContent=t.replace('tool_','').replace(/_/g,' ');sel.appendChild(o);});
  } catch(e){}
}
async function runAdminTool(){
  const tool=document.getElementById('tool-sel').value;if(!tool)return;
  let params={};try{params=JSON.parse(document.getElementById('tool-prms').value||'{}');}catch(e){}
  try{const r=await api('/api/v1/tool',{method:'POST',body:JSON.stringify({tool,params,dry:false})});document.getElementById('tool-out').textContent=JSON.stringify(r,null,2);}catch(e){document.getElementById('tool-out').textContent='Error: '+e.message;}
}
async function loadLog(svc){
  try{const r=await api('/api/v1/logs/'+svc+'?lines=100');document.getElementById('log-out').textContent=(r.lines||[]).join('\\n');}catch(e){}
  const pre=document.getElementById('log-out');if(pre)pre.scrollTop=pre.scrollHeight;
}
setInterval(()=>{if(_pinOk&&document.getElementById('panel-admin').classList.contains('active'))loadSvcs();},15000);
const MOOD_EMOJI={curious:'\U0001F914',content:'\U0001F60C',alert:'\U0001F440',playful:'\U0001F604',contemplative:'\U0001F319',bored:'\U0001F611',mischievous:'\U0001F60F',lonely:'\U0001F97A',excited:'\U0001F929',grumpy:'\U0001F624',peaceful:'\u2601\uFE0F',anxious:'\U0001F630'};
const MOOD_COL={curious:'#00d4aa',content:'#5b9cf6',alert:'#f5a623',playful:'#f7d547',contemplative:'#9b7be8',bored:'#8884aa',mischievous:'#f5a623',lonely:'#5b9cf6',excited:'#f7d547',grumpy:'#e05c5c',peaceful:'#5b9cf6',anxious:'#e05c5c'};
async function pollFace(){
  try{
    const s=await api('/api/v1/session');
    const mood=(s.obi_mood||'curious').toLowerCase();
    document.getElementById('f-ring').textContent=MOOD_EMOJI[mood]||'\U0001F914';
    const col=MOOD_COL[mood]||'var(--spark)';
    const ring=document.getElementById('f-ring');
    ring.style.borderColor=col;ring.style.boxShadow='0 0 30px '+col+'55';
    if(s.listening){ring.style.animation='ring-listen .5s ease-in-out infinite alternate';document.getElementById('f-status').textContent='listening\u2026';}
    else{ring.style.animation='pulse-ring 2s ease-in-out infinite';document.getElementById('f-status').textContent='idle';}
    document.getElementById('st-mood').textContent=mood;
    document.getElementById('st-persona').textContent=s.persona||'spark';
    if(document.getElementById('av-mood'))document.getElementById('av-mood').textContent=mood+' \u00b7 ready';
    if(document.getElementById('av-ring'))document.getElementById('av-ring').textContent=MOOD_EMOJI[mood]||'\U0001F914';
  }catch(e){}
  try{
    const logs=await api('/api/v1/logs/px-mind?lines=50');
    const tl=[...(logs.lines||[])].reverse().find(l=>l.includes('[mind] thought:'));
    if(tl){const m=tl.match(/thought: (.+?)  mood=/);if(m)document.getElementById('f-thought').textContent=m[1];}
    const sl=[...(logs.lines||[])].reverse().find(l=>l.includes('sonar='));
    if(sl){
      const ms=sl.match(/sonar=(\\d+)cm/);if(ms)document.getElementById('st-sonar').textContent=ms[1]+'cm';
      const mp=sl.match(/period=(\\w+)/);if(mp)document.getElementById('st-period').textContent=mp[1];
    }
  }catch(e){}
}
setInterval(pollFace,5000);
function addMsg(role,content,tool){
  const feed=document.getElementById('msgs');
  const d=document.createElement('div');
  const isU=role==='user';
  d.style.cssText='max-width:85%;padding:12px 16px;border-radius:'+(isU?'18px 18px 4px 18px':'18px 18px 18px 4px')+';background:'+(isU?'var(--spark)':'var(--surface2)')+';color:'+(isU?'#0a1a15':'var(--text)')+';align-self:'+(isU?'flex-end':'flex-start')+';font-size:15px;line-height:1.5;font-weight:'+(isU?'700':'600');
  if(tool){const t=document.createElement('div');t.style.cssText='font-size:10px;font-weight:800;color:var(--spark);margin-bottom:4px';t.textContent='\u25b8 '+tool.replace('tool_','').replace(/_/g,' ').toUpperCase();d.appendChild(t)}
  let txt=content;try{txt=JSON.stringify(JSON.parse(content),null,2)}catch(e){}
  const p=document.createElement('pre');p.style.cssText='white-space:pre-wrap;font-family:inherit;font-size:inherit';p.textContent=txt;d.appendChild(p);
  feed.appendChild(d);feed.scrollTop=feed.scrollHeight;
}
async function doTool(tool,params){
  try{const r=await api('/api/v1/tool',{method:'POST',body:JSON.stringify({tool,params,dry:false})});
  const out=r.stdout||r.error||JSON.stringify(r);
  sw('chat');addMsg('spark',out,tool);}
  catch(e){sw('chat');addMsg('spark','Error: '+e.message,tool);}
}
async function doPhoto(){
  sw('chat');
  const feed=document.getElementById('msgs');
  const th=document.createElement('div');th.style.cssText='color:var(--muted);font-size:13px;align-self:flex-start;padding:8px 4px';th.textContent='Taking a photo\u2026';feed.appendChild(th);feed.scrollTop=feed.scrollHeight;
  try{
    const r=await api('/api/v1/tool',{method:'POST',body:JSON.stringify({tool:'tool_describe_scene',params:{},dry:false})});
    th.remove();
    const desc=r.description||r.error||'Could not describe scene.';
    const d=document.createElement('div');
    d.style.cssText='max-width:85%;border-radius:18px 18px 18px 4px;background:var(--surface2);align-self:flex-start;overflow:hidden';
    const lab=document.createElement('div');lab.style.cssText='font-size:10px;font-weight:800;color:var(--spark);padding:8px 12px 0';lab.textContent='\u25b8 DESCRIBE SCENE';d.appendChild(lab);
    if(r.path){const fn=r.path.split('/').pop();const img=document.createElement('img');img.src='/photos/'+fn;img.style.cssText='width:100%;max-width:320px;display:block';d.appendChild(img);}
    const p=document.createElement('div');p.style.cssText='padding:8px 12px 12px;font-size:15px;line-height:1.5;font-weight:600;color:var(--text)';p.textContent=desc;d.appendChild(p);
    feed.appendChild(d);feed.scrollTop=feed.scrollHeight;
  }catch(e){th.remove();addMsg('spark','Error: '+e.message,'tool_describe_scene');}
}
function promptTimer(){
  const label=prompt('Timer name?');if(!label)return;
  const mins=prompt('How many minutes?');if(!mins||isNaN(mins))return;
  doTool('tool_timer',{duration_s:parseFloat(mins)*60,label});
}
function promptRemember(){const t=prompt('What should SPARK remember?');if(t)doTool('tool_remember',{text:t});}
let _rcT=null;
function rcStart(dir,steer){
  if(_rcT)return;
  const spd=parseInt(document.getElementById('rc-speed').value);
  const fire=()=>api('/api/v1/tool',{method:'POST',body:JSON.stringify({tool:'tool_drive',params:{direction:dir,speed:spd,duration:0.6,steer},dry:false})});
  fire();_rcT=setInterval(fire,500);
}
function rcStop(){if(_rcT){clearInterval(_rcT);_rcT=null;}api('/api/v1/tool',{method:'POST',body:JSON.stringify({tool:'tool_stop',params:{},dry:false})});}
async function sendChat(){
  const inp=document.getElementById('ci');const text=inp.value.trim();if(!text)return;
  inp.value='';inp.disabled=true;document.getElementById('sbtn').disabled=true;
  addMsg('user',text);
  const th=document.createElement('div');th.id='thinking';th.style.cssText='color:var(--muted);font-size:13px;align-self:flex-start;padding:8px 4px';th.textContent='SPARK is thinking\u2026';document.getElementById('msgs').appendChild(th);
  try{const r=await api('/api/v1/chat',{method:'POST',body:JSON.stringify({text})});document.getElementById('thinking')?.remove();addMsg('spark',r.tool_output||r.error||JSON.stringify(r),r.tool)}
  catch(e){document.getElementById('thinking')?.remove();addMsg('spark','Something went wrong.')}
  inp.disabled=false;document.getElementById('sbtn').disabled=false;inp.focus();
}
function sw(name){
  document.querySelectorAll('.tab-panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('panel-'+name).classList.add('active');
  document.getElementById('tab-'+name).classList.add('active');
  if(name==='admin')showPin();
  if(name==='spark')pollFace();
}
</script>
</body></html>"""


@app.get("/photos/{filename}")
async def serve_photo(filename: str):
    from fastapi.responses import FileResponse
    import re
    if not re.fullmatch(r"[\w\-]+\.jpe?g", filename, re.IGNORECASE):
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="not found")
    photo_path = PROJECT_ROOT / "photos" / filename
    if not photo_path.exists():
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="photo not found")
    return FileResponse(str(photo_path), media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=3600"})


@app.get("/favicon.ico")
async def favicon():
    from fastapi.responses import Response
    # Minimal 1x1 green ICO (prevents 404 log noise)
    ico = (b"\x00\x00\x01\x00\x01\x00\x01\x01\x00\x00\x01\x00\x18\x00"
           b"\x28\x00\x00\x00\x16\x00\x00\x00\x28\x00\x00\x00\x01\x00"
           b"\x00\x00\x02\x00\x00\x00\x01\x00\x18\x00\x00\x00\x00\x00"
           b"\x04\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
           b"\x00\x00\x00\x00\x00\x00\x00\xd4\x00\x00\x00\x00\x00\x00"
           b"\x00\x00\x00")
    return Response(content=ico, media_type="image/x-icon",
                    headers={"Cache-Control": "max-age=86400"})


@app.get("/", response_class=HTMLResponse)
async def web_ui():
    """Serve the SPARK web dashboard. Token injected server-side — no paste required."""
    token = os.environ.get("PX_API_TOKEN", "")
    html = _HTML_UI.replace("__SPARK_TOKEN__", token)
    return HTMLResponse(content=html)
