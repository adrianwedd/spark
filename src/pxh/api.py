"""PiCar-X REST API — thin HTTP facade over the voice_loop tool pipeline.

Usage:
    # Via launcher (preferred — sets up env correctly):
    bin/px-api-server --dry-run

    # Direct (only if px-env already sourced):
    uvicorn pxh.api:app --host 0.0.0.0 --port 8420
"""
from __future__ import annotations

import asyncio
import os
import secrets
import threading
import uuid
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .state import load_session, update_session
from .voice_loop import (
    ALLOWED_TOOLS,
    VoiceLoopError,
    execute_tool,
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
    mode: Optional[str] = None
    persona: Optional[str] = None  # "vixen", "gremlin", or "claude" (clears persona)


PATCHABLE_FIELDS = {"listening", "confirm_motion_allowed", "wheels_on_blocks", "mode", "persona"}
VALID_PERSONAS = {"vixen", "gremlin", "claude", ""}  # "claude" or "" clears persona

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FORCE_DRY = os.environ.get("PX_DRY", "0") == "1"
SYNC_TIMEOUT_DEFAULT = float(os.environ.get("PX_API_TIMEOUT", "30"))

# Tools that involve Ollama generation need longer timeouts (generation + espeak)
SLOW_TOOLS = {"tool_chat", "tool_chat_vixen", "tool_describe_scene", "tool_wander"}
SYNC_TIMEOUT_SLOW = float(os.environ.get("PX_API_TIMEOUT_SLOW", "120"))


def _resolve_dry(requested: Optional[bool]) -> bool:
    """FORCE_DRY override: server dry-run cannot be overridden remotely."""
    if FORCE_DRY:
        return True
    if requested is None:
        return FORCE_DRY
    return requested


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/v1/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


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
            raise HTTPException(status_code=400, detail=f"invalid persona: {p!r} (valid: vixen, gremlin, claude)")
        else:
            fields["persona"] = p
    return update_session(fields=fields)


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
