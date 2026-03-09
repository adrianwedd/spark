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
import os
import secrets
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .state import load_session, update_session
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
    persona: Optional[str] = None  # "vixen", "gremlin", "spark", or "claude" (clears persona)


PATCHABLE_FIELDS = {"listening", "confirm_motion_allowed", "wheels_on_blocks", "mode", "persona"}
VALID_PERSONAS = {"vixen", "gremlin", "spark", "claude", ""}  # "claude" or "" clears persona

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
            raise HTTPException(status_code=400, detail=f"invalid persona: {p!r} (valid: vixen, gremlin, spark, claude)")
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

_DEVICE_ACTIONS = {
    "reboot": ["sudo", "/bin/systemctl", "reboot"],
    "shutdown": ["sudo", "/sbin/shutdown", "-h", "now"],
}


@app.post("/api/v1/device/{action}", dependencies=[Depends(_verify_token)])
async def device_control(action: str) -> JSONResponse:
    """Reboot or shut down the host device. Action: reboot | shutdown."""
    if action not in _DEVICE_ACTIONS:
        raise HTTPException(status_code=400, detail=f"unknown action: {action}")
    subprocess.Popen(_DEVICE_ACTIONS[action])
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
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SPARK</title>
<style>
  :root {
    --bg: #1a1a2e; --surface: #16213e; --accent: #0f3460;
    --spark: #e94560; --text: #eee; --muted: #888; --radius: 8px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: system-ui, sans-serif;
         display: flex; flex-direction: column; height: 100vh; }
  header { background: var(--surface); padding: 12px 20px; display: flex;
           align-items: center; gap: 12px; border-bottom: 2px solid var(--spark); }
  header h1 { font-size: 1.3rem; color: var(--spark); letter-spacing: 2px; }
  header .status { margin-left: auto; font-size: .8rem; color: var(--muted); }
  #token-bar { background: var(--accent); padding: 8px 16px; display: flex;
               gap: 8px; align-items: center; font-size: .85rem; }
  #token-bar input { flex: 1; background: #111; color: var(--text); border: 1px solid var(--muted);
                     border-radius: var(--radius); padding: 4px 8px; font-size: .85rem; }
  .main { display: flex; flex: 1; overflow: hidden; }
  #sidebar { width: 220px; background: var(--surface); overflow-y: auto; padding: 12px;
             border-right: 1px solid #2a2a4a; }
  #sidebar h2 { font-size: .8rem; text-transform: uppercase; color: var(--muted);
                margin-bottom: 8px; letter-spacing: 1px; }
  .btn-group { display: flex; flex-direction: column; gap: 4px; margin-bottom: 16px; }
  .btn { background: var(--accent); color: var(--text); border: none; border-radius: var(--radius);
         padding: 8px 10px; font-size: .8rem; cursor: pointer; text-align: left;
         transition: background .15s; }
  .btn:hover { background: var(--spark); }
  .btn.danger { background: #5a1e2e; }
  .btn.danger:hover { background: #8b2535; }
  #chat-panel { flex: 1; display: flex; flex-direction: column; }
  #messages { flex: 1; overflow-y: auto; padding: 16px; display: flex;
              flex-direction: column; gap: 10px; }
  .msg { max-width: 80%; padding: 10px 14px; border-radius: var(--radius); font-size: .9rem;
         line-height: 1.5; }
  .msg.user { background: var(--accent); align-self: flex-end; }
  .msg.bot { background: var(--surface); border: 1px solid #2a2a4a; align-self: flex-start; }
  .msg.bot .tool-tag { font-size: .75rem; color: var(--spark); margin-bottom: 4px; }
  .msg.error { background: #3a1020; border-color: var(--spark); }
  .msg.system { background: #1a2a1a; font-size: .8rem; color: var(--muted); align-self: center; }
  #input-bar { display: flex; gap: 8px; padding: 12px; border-top: 1px solid #2a2a4a; }
  #input-bar input { flex: 1; background: var(--surface); color: var(--text);
                     border: 1px solid #2a2a4a; border-radius: var(--radius);
                     padding: 10px 14px; font-size: .95rem; }
  #input-bar input:focus { outline: none; border-color: var(--spark); }
  #send-btn { background: var(--spark); color: #fff; border: none; border-radius: var(--radius);
              padding: 10px 18px; font-size: .9rem; cursor: pointer; }
  #send-btn:hover { opacity: .85; }
  #send-btn:disabled { opacity: .4; cursor: default; }
  .dry-tag { font-size: .7rem; background: #2a4a2a; color: #8f8; border-radius: 4px;
             padding: 2px 6px; margin-left: 8px; }
</style>
</head>
<body>
<header>
  <h1>⚡ SPARK</h1>
  <span class="status" id="status-line">Connecting…</span>
</header>
<div id="token-bar" style="display:none">
  <input id="token-input" type="hidden" value="__SPARK_TOKEN__" autocomplete="off">
</div>
<div class="main">
  <div id="sidebar">
    <h2>Routines</h2>
    <div class="btn-group">
      <button class="btn" onclick="doTool('tool_routine',{action:'load',name:'morning'})">🌅 Morning</button>
      <button class="btn" onclick="doTool('tool_routine',{action:'load',name:'homework'})">📚 Homework</button>
      <button class="btn" onclick="doTool('tool_routine',{action:'load',name:'bedtime'})">🌙 Bedtime</button>
      <button class="btn" onclick="doTool('tool_routine',{action:'next'})">▶ Next step</button>
      <button class="btn" onclick="doTool('tool_routine',{action:'status'})">? Current step</button>
    </div>
    <h2>Regulation</h2>
    <div class="btn-group">
      <button class="btn" onclick="doTool('tool_quiet',{action:'start'})">🤫 Quiet mode</button>
      <button class="btn" onclick="doTool('tool_quiet',{action:'end'})">✅ End quiet</button>
      <button class="btn" onclick="doTool('tool_breathe',{type:'simple',rounds:2})">💨 Breathe</button>
      <button class="btn" onclick="doTool('tool_breathe',{type:'box',rounds:2})">📦 Box breathe</button>
      <button class="btn" onclick="doTool('tool_dopamine_menu',{energy:'medium',context:'free'})">🎲 Ideas</button>
      <button class="btn" onclick="doTool('tool_sensory_check',{action:'ask'})">🔍 Body check</button>
    </div>
    <h2>Check-in</h2>
    <div class="btn-group">
      <button class="btn" onclick="doTool('tool_checkin',{action:'ask'})">😊 How are you?</button>
      <button class="btn" onclick="doTool('tool_celebrate',{})">🎉 Celebrate!</button>
      <button class="btn" onclick="doTool('tool_repair',{})">🤝 Repair</button>
    </div>
    <h2>Transitions</h2>
    <div class="btn-group">
      <button class="btn" onclick="doTool('tool_transition',{action:'warn',minutes:5,label:'next thing'})">⏰ 5 min warn</button>
      <button class="btn" onclick="doTool('tool_transition',{action:'warn',minutes:2,label:'next thing'})">⏰ 2 min warn</button>
      <button class="btn" onclick="doTool('tool_transition',{action:'arrived'})">✅ Arrived</button>
    </div>
    <h2>Robot</h2>
    <div class="btn-group">
      <button class="btn" onclick="doTool('tool_status',{})">📊 Status</button>
      <button class="btn" onclick="doTool('tool_sonar',{})">📡 Sonar</button>
      <button class="btn" onclick="doTool('tool_time',{})">🕐 Time</button>
      <button class="btn" onclick="doTool('tool_emote',{name:'happy'})">😄 Happy</button>
      <button class="btn" onclick="doTool('tool_emote',{name:'idle'})">😐 Idle</button>
      <button class="btn danger" onclick="doTool('tool_stop',{})">⛔ Stop</button>
    </div>
    <h2>Calendar</h2>
    <div class="btn-group">
      <button class="btn" onclick="doTool('tool_gws_calendar',{action:'today'})">📅 Today</button>
      <button class="btn" onclick="doTool('tool_gws_calendar',{action:'next'})">➡ Next event</button>
    </div>
    <h2>Services</h2>
    <div id="services-panel" class="btn-group">
      <span class="muted" style="font-size:0.8em">Loading…</span>
    </div>
  </div>
  <div id="chat-panel">
    <div id="messages">
      <div class="msg system">SPARK web interface — chat or tap a button to get started.</div>
    </div>
    <div id="input-bar">
      <input id="chat-input" type="text" placeholder="Type a message to SPARK…" autocomplete="off">
      <button id="send-btn" onclick="sendChat()">Send</button>
    </div>
  </div>
</div>
<script>
const api = (path, opts={}) => {
  const token = document.getElementById('token-input').value.trim();
  return fetch(path, {
    headers: {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
    ...opts,
  });
};

const msgs = document.getElementById('messages');
function addMsg(cls, html) {
  const d = document.createElement('div');
  d.className = 'msg ' + cls;
  d.innerHTML = html;
  msgs.appendChild(d);
  msgs.scrollTop = msgs.scrollHeight;
}

async function pollStatus() {
  try {
    const r = await api('/api/v1/session');
    if (r.ok) {
      const s = await r.json();
      const persona = s.persona || 'spark';
      const mood = s.obi_mood || '—';
      const routine = s.obi_routine ? ` • ${s.obi_routine}` : '';
      const dry = document.getElementById('status-line').dataset.dry === '1' ? ' <span class="dry-tag">DRY</span>' : '';
      document.getElementById('status-line').innerHTML = `${persona}${routine} • mood: ${mood}${dry}`;
    }
  } catch(e) {}
}
setInterval(pollStatus, 5000);
pollStatus();

async function doTool(tool, params, dry=undefined) {
  addMsg('system', `→ ${tool}`);
  try {
    const r = await api('/api/v1/tool', {
      method: 'POST',
      body: JSON.stringify({tool, params, dry}),
    });
    const data = await r.json();
    let out = '';
    if (data.stdout) {
      try { out = JSON.stringify(JSON.parse(data.stdout), null, 2); } catch { out = data.stdout; }
    }
    const cls = data.status === 'ok' || data.status === 'accepted' ? 'bot' : 'error';
    addMsg(cls, `<div class="tool-tag">${tool}</div><pre style="white-space:pre-wrap;font-size:.8rem">${out || data.detail || JSON.stringify(data)}</pre>`);
  } catch(e) {
    addMsg('error', 'Network error: ' + e.message);
  }
}

async function sendChat() {
  const inp = document.getElementById('chat-input');
  const text = inp.value.trim();
  if (!text) return;
  inp.value = '';
  addMsg('user', text);
  document.getElementById('send-btn').disabled = true;
  try {
    const r = await api('/api/v1/chat', {
      method: 'POST',
      body: JSON.stringify({text}),
    });
    const data = await r.json();
    const cls = data.status === 'ok' ? 'bot' : 'error';
    const toolTag = data.tool ? `<div class="tool-tag">${data.tool}</div>` : '';
    let out = data.tool_output || data.error || JSON.stringify(data);
    try { out = JSON.stringify(JSON.parse(out), null, 2); } catch {}
    addMsg(cls, `${toolTag}<pre style="white-space:pre-wrap;font-size:.8rem">${out}</pre>`);
  } catch(e) {
    addMsg('error', 'Network error: ' + e.message);
  }
  document.getElementById('send-btn').disabled = false;
}

document.getElementById('chat-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') sendChat();
});

// Service management
const SERVICES = ['px-alive','px-mind','px-wake-listen','px-api-server'];
const SVC_ICONS = {'px-alive':'🤖','px-mind':'🧠','px-wake-listen':'👂','px-api-server':'🌐'};

async function loadServices() {
  try {
    const r = await api('/api/v1/services');
    const data = await r.json();
    const panel = document.getElementById('services-panel');
    panel.innerHTML = '';
    (data.services || []).forEach(s => {
      const active = s.active === 'active';
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:center;gap:6px;margin-bottom:4px;font-size:.8em';
      const dot = active ? '<span style="color:#4caf50">●</span>' : '<span style="color:#e94560">●</span>';
      const icon = SVC_ICONS[s.service] || '⚙';
      row.innerHTML = `${dot} ${icon} <span style="flex:1">${s.service.replace('px-','')}</span>
        <button class="btn" style="padding:2px 8px;font-size:.75em" onclick="svcAction('${s.service}','restart')">↺</button>
        ${active ? `<button class="btn danger" style="padding:2px 8px;font-size:.75em" onclick="svcAction('${s.service}','stop')">■</button>` : `<button class="btn" style="padding:2px 8px;font-size:.75em;background:#2a5a2a" onclick="svcAction('${s.service}','start')">▶</button>`}`;
      panel.appendChild(row);
    });
  } catch(e) {
    document.getElementById('services-panel').innerHTML = '<span class="muted" style="font-size:.8em">Error loading</span>';
  }
}

async function svcAction(service, action) {
  addMsg('system', `→ service ${action}: ${service}`);
  try {
    const r = await api(`/api/v1/services/${service}/${action}`, {method:'POST'});
    const data = await r.json();
    const cls = data.status === 'ok' ? 'bot' : 'error';
    addMsg(cls, `<div class="tool-tag">${service}</div><pre style="white-space:pre-wrap;font-size:.75rem">${action}: ${data.status}${data.stderr ? '\\n' + data.stderr : ''}</pre>`);
    setTimeout(loadServices, 2000);
  } catch(e) {
    addMsg('error', 'Service error: ' + e.message);
  }
}

loadServices();
setInterval(loadServices, 15000);
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def web_ui():
    """Serve the SPARK web dashboard. Token injected server-side — no paste required."""
    token = os.environ.get("PX_API_TOKEN", "")
    html = _HTML_UI.replace("__SPARK_TOKEN__", token)
    return HTMLResponse(content=html)
