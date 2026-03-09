# SPARK UI Redesign Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the dark cyberpunk dev-panel UI with a mobile-first tab-bar interface split between Obi's friendly big-button experience and Adrian's PIN-gated admin panel.

**Architecture:** All changes are in `src/pxh/api.py`. Three new backend endpoints + full replacement of the `_HTML_UI` string. No new files. No changes to voice loop, state, or tools.

**Tech Stack:** FastAPI, vanilla JS (no framework), Nunito Google Font, CSS custom properties for theming.

---

## Task 1: Backend — Log tail endpoint

**Files:**
- Modify: `src/pxh/api.py` (after the services section, ~line 430)
- Test: `tests/test_api.py`

**Step 1: Write the failing tests**

Add to `tests/test_api.py`:

```python
class TestLogs:
    def test_log_rejects_invalid_service(self, api_client, auth_headers):
        r = api_client.get("/api/v1/logs/../../etc/passwd", headers=auth_headers)
        assert r.status_code == 400

    def test_log_requires_auth(self, api_client):
        r = api_client.get("/api/v1/logs/px-mind")
        assert r.status_code in (401, 403)

    def test_log_missing_file_returns_empty(self, api_client, auth_headers):
        r = api_client.get("/api/v1/logs/px-alive", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["lines"] == [] or isinstance(r.json()["lines"], list)
```

**Step 2: Run to confirm failure**
```bash
.venv/bin/python -m pytest tests/test_api.py::TestLogs -v
# Expected: FAIL — 404 not found
```

**Step 3: Add the endpoint to api.py** (after `control_service` function, ~line 430)

```python
_LOG_ALLOWLIST = {
    "px-mind", "px-wake-listen", "px-alive",
    "tool-voice", "tool-describe_scene",
}

@app.get("/api/v1/logs/{service}", dependencies=[Depends(_verify_token)])
async def tail_log(service: str, lines: int = 100) -> JSONResponse:
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
```

**Step 4: Run tests**
```bash
.venv/bin/python -m pytest tests/test_api.py::TestLogs -v
# Expected: PASS
```

**Step 5: Commit**
```bash
git add src/pxh/api.py tests/test_api.py
git commit -m "feat(api): add /logs/{service} tail endpoint"
```

---

## Task 2: Backend — Device control + sudoers

**Files:**
- Modify: `src/pxh/api.py`
- Modify: `/etc/sudoers.d/picar-x-services` (via bash)
- Test: `tests/test_api.py`

**Step 1: Write failing tests**

```python
class TestDevice:
    def test_bad_action_rejected(self, api_client, auth_headers):
        r = api_client.post("/api/v1/device/format", headers=auth_headers)
        assert r.status_code == 400

    def test_requires_auth(self, api_client):
        r = api_client.post("/api/v1/device/reboot")
        assert r.status_code in (401, 403)

    def test_reboot_calls_systemctl(self, api_client, auth_headers, monkeypatch):
        import pxh.api as a
        calls = []
        monkeypatch.setattr(a.subprocess, "run",
            lambda *args, **kw: calls.append(args) or type("R",(),{"returncode":0,"stderr":""})())
        r = api_client.post("/api/v1/device/reboot", headers=auth_headers)
        assert r.status_code == 200
        assert any("reboot" in str(c) for c in calls)
```

**Step 2: Run to confirm failure**
```bash
.venv/bin/python -m pytest tests/test_api.py::TestDevice -v
```

**Step 3: Add endpoint**

```python
_DEVICE_ACTIONS = {
    "reboot":   ["sudo", "systemctl", "reboot"],
    "shutdown": ["sudo", "shutdown", "-h", "now"],
}

@app.post("/api/v1/device/{action}", dependencies=[Depends(_verify_token)])
async def device_control(action: str) -> JSONResponse:
    if action not in _DEVICE_ACTIONS:
        raise HTTPException(status_code=400, detail=f"unknown action: {action}")
    result = subprocess.run(_DEVICE_ACTIONS[action], capture_output=True, text=True)
    return JSONResponse(content={
        "ok": result.returncode == 0,
        "action": action,
        "stderr": result.stderr.strip()[:200],
    })
```

**Step 4: Add sudoers entries**
```bash
sudo bash -c 'echo "pi ALL=(ALL) NOPASSWD: /bin/systemctl reboot" >> /etc/sudoers.d/picar-x-services'
sudo bash -c 'echo "pi ALL=(ALL) NOPASSWD: /sbin/shutdown -h now" >> /etc/sudoers.d/picar-x-services'
sudo visudo -c
```

**Step 5: Run tests + commit**
```bash
.venv/bin/python -m pytest tests/test_api.py::TestDevice -v
git add src/pxh/api.py tests/test_api.py
git commit -m "feat(api): /device/reboot|shutdown endpoint + sudoers"
```

---

## Task 3: Backend — PIN verify endpoint

First, add `PX_ADMIN_PIN=1234` to `.env` (change to desired PIN).

**Files:**
- Modify: `src/pxh/api.py`
- Test: `tests/test_api.py`

**Step 1: Write failing tests**

```python
class TestPin:
    def test_correct_pin(self, api_client, auth_headers, monkeypatch):
        monkeypatch.setenv("PX_ADMIN_PIN", "9999")
        r = api_client.post("/api/v1/pin/verify", json={"pin":"9999"}, headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_wrong_pin(self, api_client, auth_headers, monkeypatch):
        monkeypatch.setenv("PX_ADMIN_PIN", "9999")
        r = api_client.post("/api/v1/pin/verify", json={"pin":"0000"}, headers=auth_headers)
        assert r.status_code == 403

    def test_requires_auth(self, api_client):
        r = api_client.post("/api/v1/pin/verify", json={"pin":"1234"})
        assert r.status_code in (401, 403)
```

**Step 2: Run to confirm failure**
```bash
.venv/bin/python -m pytest tests/test_api.py::TestPin -v
```

**Step 3: Add Pydantic model + endpoint**

```python
class PinRequest(BaseModel):
    pin: str

@app.post("/api/v1/pin/verify", dependencies=[Depends(_verify_token)])
async def verify_pin(body: PinRequest) -> JSONResponse:
    expected = os.environ.get("PX_ADMIN_PIN", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="PX_ADMIN_PIN not configured")
    if not secrets.compare_digest(body.pin.strip(), expected):
        raise HTTPException(status_code=403, detail="wrong PIN")
    return JSONResponse(content={"ok": True})
```

**Step 4: Run full suite + commit**
```bash
.venv/bin/python -m pytest -m "not live" -q
# Expected: 110+ passed
git add src/pxh/api.py tests/test_api.py
git commit -m "feat(api): PIN verify endpoint"
```

---

## Task 4: Frontend foundation — CSS design system + tab bar shell

Replace the entire `_HTML_UI` string (lines 431–680 in api.py) with the new shell.
The new string starts: `_HTML_UI = """<!DOCTYPE html>` and must end with `</html>"""`

**The base structure to use as starting point:**

```python
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
  <div id="panel-chat"    class="tab-panel active"><!-- CHAT --></div>
  <div id="panel-actions" class="tab-panel"><!-- ACTIONS --></div>
  <div id="panel-spark"   class="tab-panel"><!-- SPARK FACE --></div>
  <div id="panel-admin"   class="tab-panel"><!-- ADMIN --></div>
</div>
<nav id="tab-bar">
  <button class="tab-btn active" id="tab-chat"    onclick="sw('chat')"><span class="ti">💬</span>Chat</button>
  <button class="tab-btn"        id="tab-actions" onclick="sw('actions')"><span class="ti">⚡</span>Actions</button>
  <button class="tab-btn"        id="tab-spark"   onclick="sw('spark')"><span class="ti">🤖</span>SPARK</button>
  <button class="tab-btn"        id="tab-admin"   onclick="sw('admin')"><span class="ti">🔧🔒</span>Adrian</button>
</nav>
<input type="hidden" id="tok" value="__SPARK_TOKEN__">
<script>
const tok=()=>document.getElementById('tok').value;
const api=(path,opts={})=>fetch(path,{headers:{'Authorization':'Bearer '+tok(),'Content-Type':'application/json',...(opts.headers||{})}, ...opts}).then(r=>r.json());
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
```

**Verify smoke test:**
```bash
sudo systemctl restart px-api-server
curl -s http://localhost:8420/ | grep -c "Nunito"
# Expected: 1
```

**Commit:**
```bash
git add src/pxh/api.py
git commit -m "feat(ui): CSS design system + tab bar shell"
```

---

## Task 5: Chat tab

Replace `<!-- CHAT -->` inside `panel-chat` with the full chat UI HTML, then add the JS.

**HTML to insert inside the div:**
```html
    <div id="av-bar" style="padding:12px 16px 8px;display:flex;align-items:center;gap:12px;background:var(--surface);border-bottom:1px solid var(--surface2);flex-shrink:0">
      <div id="av-ring" style="width:52px;height:52px;border-radius:50%;border:3px solid var(--spark);display:flex;align-items:center;justify-content:center;font-size:28px;flex-shrink:0;animation:pulse-ring 2s ease-in-out infinite">🤔</div>
      <div><div style="font-size:13px;font-weight:800;color:var(--spark)">SPARK</div><div id="av-mood" style="font-size:11px;color:var(--muted)">curious · ready</div></div>
    </div>
    <div id="msgs" style="flex:1;overflow-y:auto;padding:12px 16px;display:flex;flex-direction:column;gap:10px"></div>
    <div style="padding:10px 12px;background:var(--surface);border-top:1px solid var(--surface2);flex-shrink:0;display:flex;gap:8px">
      <input id="ci" type="text" placeholder="Talk to SPARK…" style="flex:1;background:var(--surface2);border:2px solid transparent;border-radius:24px;padding:12px 18px;font-family:inherit;font-size:15px;color:var(--text);outline:none" onfocus="this.style.borderColor='var(--spark)'" onblur="this.style.borderColor='transparent'" onkeydown="if(event.key==='Enter')sendChat()">
      <button onclick="sendChat()" id="sbtn" class="btn btn-spark" style="width:auto;padding:12px 20px;border-radius:24px;flex-shrink:0">Send</button>
    </div>
```

**JS to add inside the `<script>` tag:**
```javascript
function addMsg(role,content,tool){
  const feed=document.getElementById('msgs');
  const d=document.createElement('div');
  const isU=role==='user';
  d.style.cssText='max-width:85%;padding:12px 16px;border-radius:'+(isU?'18px 18px 4px 18px':'18px 18px 18px 4px')+';background:'+(isU?'var(--spark)':'var(--surface2)')+';color:'+(isU?'#0a1a15':'var(--text)')+';align-self:'+(isU?'flex-end':'flex-start')+';font-size:15px;line-height:1.5;font-weight:'+(isU?'700':'600');
  if(tool){const t=document.createElement('div');t.style.cssText='font-size:10px;font-weight:800;color:var(--spark);margin-bottom:4px';t.textContent='▸ '+tool.replace('tool_','').replace(/_/g,' ').toUpperCase();d.appendChild(t)}
  let txt=content;try{txt=JSON.stringify(JSON.parse(content),null,2)}catch{}
  const p=document.createElement('pre');p.style.cssText='white-space:pre-wrap;font-family:inherit;font-size:inherit';p.textContent=txt;d.appendChild(p);
  feed.appendChild(d);feed.scrollTop=feed.scrollHeight;
}
async function sendChat(){
  const inp=document.getElementById('ci');const text=inp.value.trim();if(!text)return;
  inp.value='';inp.disabled=true;document.getElementById('sbtn').disabled=true;
  addMsg('user',text);
  const th=document.createElement('div');th.id='thinking';th.style.cssText='color:var(--muted);font-size:13px;align-self:flex-start;padding:8px 4px';th.textContent='SPARK is thinking…';document.getElementById('msgs').appendChild(th);
  try{const r=await api('/api/v1/chat',{method:'POST',body:JSON.stringify({text})});document.getElementById('thinking')?.remove();addMsg('spark',r.result?.stdout||r.error||JSON.stringify(r),r.result?.tool)}
  catch{document.getElementById('thinking')?.remove();addMsg('spark','Something went wrong.')}
  inp.disabled=false;document.getElementById('sbtn').disabled=false;inp.focus();
}
```

**Commit:**
```bash
git add src/pxh/api.py
git commit -m "feat(ui): chat tab with avatar, bubbles, thinking indicator"
```

---

## Task 6: Actions tab — groups 1, 3, 5

Replace `<!-- ACTIONS -->` inside `panel-actions` with:

```html
    <div style="padding:12px 16px 80px;display:flex;flex-direction:column;gap:6px">
      <div class="sec-hdr" style="color:var(--spark)">🧘 I need help</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
        <button class="btn btn-spark" onclick="doTool('tool_breathe',{rounds:2})">💨 Breathe with me</button>
        <button class="btn btn-spark" onclick="doTool('tool_quiet',{mode:'on'})">🤫 Go quiet</button>
        <button class="btn btn-muted"  onclick="doTool('tool_quiet',{mode:'off'})">✅ End quiet</button>
        <button class="btn btn-spark" onclick="doTool('tool_sensory_check',{})">🧠 Body check</button>
        <button class="btn btn-spark" onclick="doTool('tool_dopamine_menu',{energy:'medium'})">🎲 What can I do?</button>
        <button class="btn btn-spark" onclick="doTool('tool_repair',{})">🤝 Make things better</button>
      </div>
      <div class="sec-hdr" style="color:var(--yellow)">💛 How are we doing?</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
        <button class="btn btn-yellow" onclick="doTool('tool_checkin',{})">😊 How are you?</button>
        <button class="btn btn-yellow" onclick="doTool('tool_celebrate',{})">🎉 Celebrate!</button>
        <button class="btn btn-yellow" onclick="doTool('tool_gws_calendar',{action:'today'})">📅 What's today?</button>
        <button class="btn btn-yellow" onclick="doTool('tool_gws_calendar',{action:'next'})">➡️ Next thing</button>
        <button class="btn btn-yellow" onclick="doTool('tool_time',{})">🕐 What time is it?</button>
        <button class="btn btn-yellow" onclick="doTool('tool_weather',{})">⛅ Weather</button>
      </div>
      <div class="sec-hdr" style="color:var(--blue)">🔊 Sounds & memory</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
        <button class="btn btn-blue" onclick="doTool('tool_play_sound',{sound:'happy'})">🎵 Play a sound</button>
        <button class="btn btn-blue" onclick="promptTimer()">⏱️ Set a timer</button>
        <button class="btn btn-blue" onclick="promptRemember()">💭 Remember this</button>
        <button class="btn btn-blue" onclick="doTool('tool_recall',{})">🔍 What do you remember?</button>
      </div>
      <!-- routines + move SPARK added next -->
    </div>
```

**JS to add:**
```javascript
async function doTool(tool,params){
  const r=await api('/api/v1/tool',{method:'POST',body:JSON.stringify({tool,params,dry:false})});
  const out=r.stdout||r.error||JSON.stringify(r);
  sw('chat');addMsg('spark',out,tool);
}
function promptTimer(){
  const label=prompt('Timer name?');if(!label)return;
  const mins=prompt('How many minutes?');if(!mins||isNaN(mins))return;
  doTool('tool_timer',{duration_s:parseFloat(mins)*60,label});
}
function promptRemember(){const t=prompt('What should SPARK remember?');if(t)doTool('tool_remember',{text:t})}
```

**Commit:**
```bash
git add src/pxh/api.py
git commit -m "feat(ui): actions tab — I need help, how we doing, sounds & memory"
```

---

## Task 7: Actions tab — Routines group

Append inside the outer actions div (before its closing `</div>`):

```html
      <div class="sec-hdr" style="color:var(--orange)">📋 Our routines</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
        <button class="btn btn-orange" onclick="doTool('tool_routine',{action:'start',routine:'morning'})">🌅 Morning</button>
        <button class="btn btn-orange" onclick="doTool('tool_routine',{action:'start',routine:'homework'})">📚 Homework</button>
        <button class="btn btn-orange" onclick="doTool('tool_routine',{action:'start',routine:'bedtime'})">🌙 Bedtime</button>
        <button class="btn btn-orange" onclick="doTool('tool_routine',{action:'next'})">➡️ Next step</button>
        <button class="btn btn-orange" onclick="doTool('tool_routine',{action:'status'})">❓ What's the plan?</button>
        <button class="btn btn-muted"  onclick="doTool('tool_routine',{action:'stop'})">⏹️ Stop routine</button>
      </div>
      <div class="sec-hdr" style="color:var(--orange)">⏰ Transitions</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
        <button class="btn btn-orange" onclick="doTool('tool_transition',{minutes:5})">⏰ 5 min warning</button>
        <button class="btn btn-orange" onclick="doTool('tool_transition',{minutes:2})">⏰ 2 min warning</button>
        <button class="btn btn-orange" onclick="doTool('tool_transition',{action:'arrived'})">✅ I'm here now</button>
      </div>
```

**Commit:**
```bash
git add src/pxh/api.py
git commit -m "feat(ui): actions tab — routines and transitions group"
```

---

## Task 8: Actions tab — Move SPARK! (RC D-pad)

Append inside the outer actions div:

```html
      <div class="sec-hdr" style="color:var(--purple)">🤖 Move SPARK!</div>
      <div style="background:var(--surface2);border-radius:var(--radius);padding:16px;margin-bottom:8px">
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;grid-template-rows:auto auto auto;gap:8px;max-width:240px;margin:0 auto 12px">
          <div></div>
          <button class="btn btn-purple" style="min-height:64px;font-size:24px" onpointerdown="rcStart('forward',0)" onpointerup="rcStop()" onpointerleave="rcStop()">▲</button>
          <div></div>
          <button class="btn btn-purple" style="min-height:64px;font-size:24px" onpointerdown="rcStart('forward',-28)" onpointerup="rcStop()" onpointerleave="rcStop()">◄</button>
          <button class="btn btn-danger" style="min-height:64px;font-size:20px;font-weight:900" onclick="doTool('tool_stop',{})">⛔</button>
          <button class="btn btn-purple" style="min-height:64px;font-size:24px" onpointerdown="rcStart('forward',28)" onpointerup="rcStop()" onpointerleave="rcStop()">►</button>
          <div></div>
          <button class="btn btn-purple" style="min-height:64px;font-size:24px" onpointerdown="rcStart('backward',0)" onpointerup="rcStop()" onpointerleave="rcStop()">▼</button>
          <div></div>
        </div>
        <div style="display:flex;align-items:center;gap:10px;max-width:240px;margin:0 auto">
          <span style="font-size:12px;color:var(--muted);white-space:nowrap">Speed</span>
          <input type="range" id="rc-speed" min="10" max="50" value="30" style="flex:1;accent-color:var(--purple)" oninput="document.getElementById('rc-spd-val').textContent=this.value">
          <span id="rc-spd-val" style="font-size:13px;font-weight:800;color:var(--purple);min-width:28px">30</span>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
        <button class="btn btn-purple" onclick="doTool('tool_circle',{speed:30,duration:4})">⭕ Spin in a circle</button>
        <button class="btn btn-purple" onclick="doTool('tool_figure8',{speed:25,duration:6})">∞ Figure-8</button>
        <button class="btn btn-purple" onclick="doTool('tool_wander',{})">🎲 Explore the room</button>
        <button class="btn btn-purple" onclick="doTool('tool_perform',{performance:'dance'})">🕺 Do a trick</button>
        <button class="btn btn-purple" onclick="doTool('tool_look',{direction:'left'})">👈 Look left</button>
        <button class="btn btn-purple" onclick="doTool('tool_look',{direction:'right'})">👉 Look right</button>
        <button class="btn btn-purple" onclick="doTool('tool_look',{direction:'up'})">☝️ Look up</button>
        <button class="btn btn-purple" onclick="doTool('tool_emote',{emotion:'happy'})">😄 Happy face</button>
        <button class="btn btn-purple" onclick="doTool('tool_describe_scene',{})">📸 What do you see?</button>
        <button class="btn btn-purple" onclick="doTool('tool_sonar',{})">📡 How far away?</button>
      </div>
```

**JS to add:**
```javascript
let _rcT=null;
function rcStart(dir,steer){
  if(_rcT)return;
  const spd=parseInt(document.getElementById('rc-speed').value);
  const fire=()=>api('/api/v1/tool',{method:'POST',body:JSON.stringify({tool:'tool_drive',params:{direction:dir,speed:spd,duration:0.6,steer},dry:false})});
  fire();_rcT=setInterval(fire,500);
}
function rcStop(){if(_rcT){clearInterval(_rcT);_rcT=null}api('/api/v1/tool',{method:'POST',body:JSON.stringify({tool:'tool_stop',params:{},dry:false})})}
```

**Commit:**
```bash
git add src/pxh/api.py
git commit -m "feat(ui): RC D-pad + speed slider + one-shot move buttons"
```

---

## Task 9: SPARK face tab

Replace `<!-- SPARK FACE -->` inside `panel-spark` with:

```html
    <div style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:24px;gap:20px">
      <div id="f-status" style="font-size:13px;font-weight:800;color:var(--muted);letter-spacing:.1em;text-transform:uppercase">idle</div>
      <div id="f-ring" style="width:140px;height:140px;border-radius:50%;border:5px solid var(--spark);display:flex;align-items:center;justify-content:center;font-size:72px;box-shadow:0 0 30px rgba(0,212,170,.3);transition:border-color .5s,box-shadow .5s;animation:pulse-ring 2s ease-in-out infinite">🤔</div>
      <div style="background:var(--surface2);border-radius:var(--radius);padding:18px 20px;max-width:340px;width:100%;border-left:4px solid var(--spark)">
        <div style="font-size:11px;font-weight:800;color:var(--spark);margin-bottom:8px;letter-spacing:.05em">SPARK IS THINKING</div>
        <div id="f-thought" style="font-size:15px;line-height:1.6;font-style:italic">Loading…</div>
      </div>
      <div style="display:flex;gap:12px;flex-wrap:wrap;justify-content:center">
        <div class="spark-stat"><span id="st-mood">–</span><br><span class="stat-lbl">mood</span></div>
        <div class="spark-stat"><span id="st-sonar">–</span><br><span class="stat-lbl">sonar</span></div>
        <div class="spark-stat"><span id="st-period">–</span><br><span class="stat-lbl">time</span></div>
        <div class="spark-stat"><span id="st-persona">–</span><br><span class="stat-lbl">persona</span></div>
      </div>
    </div>
```

**JS to add:**
```javascript
const MOOD_EMOJI={curious:'🤔',content:'😌',alert:'👀',playful:'😄',contemplative:'🌙',bored:'😑',mischievous:'😏',lonely:'🥺',excited:'🤩',grumpy:'😤',peaceful:'☁️',anxious:'😰'};
const MOOD_COL={curious:'#00d4aa',content:'#5b9cf6',alert:'#f5a623',playful:'#f7d547',contemplative:'#9b7be8',bored:'#8884aa',mischievous:'#f5a623',lonely:'#5b9cf6',excited:'#f7d547',grumpy:'#e05c5c',peaceful:'#5b9cf6',anxious:'#e05c5c'};

async function pollFace(){
  try{
    const s=await api('/api/v1/session');
    const mood=(s.obi_mood||'curious').toLowerCase();
    document.getElementById('f-ring').textContent=MOOD_EMOJI[mood]||'🤔';
    const col=MOOD_COL[mood]||'var(--spark)';
    const ring=document.getElementById('f-ring');
    ring.style.borderColor=col;ring.style.boxShadow='0 0 30px '+col+'55';
    if(s.listening){ring.style.animation='ring-listen .5s ease-in-out infinite alternate';document.getElementById('f-status').textContent='listening…'}
    else{ring.style.animation='pulse-ring 2s ease-in-out infinite';document.getElementById('f-status').textContent='idle'}
    document.getElementById('st-mood').textContent=mood;
    document.getElementById('st-persona').textContent=s.persona||'spark';
    document.getElementById('av-mood').textContent=mood+' · ready';
    document.getElementById('av-ring').textContent=MOOD_EMOJI[mood]||'🤔';
  }catch{}
  try{
    const logs=await api('/api/v1/logs/px-mind?lines=50');
    const tl=[...(logs.lines||[])].reverse().find(l=>l.includes('[mind] thought:'));
    if(tl){const m=tl.match(/thought: (.+?)  mood=/);if(m)document.getElementById('f-thought').textContent=m[1]}
    const sl=[...(logs.lines||[])].reverse().find(l=>l.includes('sonar='));
    if(sl){
      const ms=sl.match(/sonar=(\d+)cm/);if(ms)document.getElementById('st-sonar').textContent=ms[1]+'cm';
      const mp=sl.match(/period=(\w+)/);if(mp)document.getElementById('st-period').textContent=mp[1];
    }
  }catch{}
}
setInterval(pollFace,5000);
```

**Commit:**
```bash
git add src/pxh/api.py
git commit -m "feat(ui): SPARK face tab — mood ring, thought bubble, stats"
```

---

## Task 10: Adrian panel — PIN + Services + Parental

Replace `<!-- ADMIN -->` inside `panel-admin` with the full admin HTML. Due to length, build it in two parts:

**Part A — PIN gate + inner tabs shell:**
```html
    <div id="pin-gate" style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:40px 24px;gap:16px">
      <div style="font-size:48px">🔧</div>
      <div style="font-size:20px;font-weight:800">Adrian's Panel</div>
      <div style="font-size:14px;color:var(--muted);text-align:center">Enter your PIN to continue.</div>
      <input id="pin-inp" type="password" inputmode="numeric" maxlength="8" placeholder="PIN"
        style="font-size:28px;letter-spacing:.3em;text-align:center;background:var(--surface2);border:2px solid var(--surface2);border-radius:var(--radius);padding:14px 20px;width:180px;color:var(--text);font-family:inherit;outline:none"
        onfocus="this.style.borderColor='var(--spark)'" onblur="this.style.borderColor='var(--surface2)'"
        onkeydown="if(event.key==='Enter')subPin()">
      <button class="btn btn-spark" style="width:180px" onclick="subPin()">Unlock</button>
      <div id="pin-err" style="color:var(--danger);font-size:13px;display:none">Wrong PIN — try again</div>
    </div>
    <div id="admin-body" style="display:none;flex-direction:column;height:100%">
      <div style="display:flex;background:var(--surface);border-bottom:1px solid var(--surface2);flex-shrink:0">
        <button class="atab-btn active" id="at-svc"      onclick="swA('svc')">⚙️ Services</button>
        <button class="atab-btn"        id="at-tools"    onclick="swA('tools')">🛠 Tools</button>
        <button class="atab-btn"        id="at-logs"     onclick="swA('logs')">📋 Logs</button>
        <button class="atab-btn"        id="at-parental" onclick="swA('parental')">👨‍👩‍👧 Parental</button>
      </div>
      <div id="ap-svc"      class="apanel active" style="padding:16px;overflow-y:auto">
        <div id="svc-list" style="display:flex;flex-direction:column;gap:10px;margin-bottom:16px"></div>
        <div class="sec-hdr" style="color:var(--danger)">⚠️ Device</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px">
          <button class="btn btn-danger" onclick="confirmDev('reboot')">🔄 Reboot Pi</button>
          <button class="btn btn-danger" onclick="confirmDev('shutdown')">⛔ Shutdown Pi</button>
        </div>
      </div>
      <div id="ap-tools" class="apanel" style="padding:16px;overflow-y:auto">
        <div class="sec-hdr" style="margin-bottom:8px">Raw Tool Runner</div>
        <select id="tool-sel" style="width:100%;background:var(--surface2);border:none;border-radius:8px;padding:10px 14px;color:var(--text);font-family:inherit;font-size:14px;margin-bottom:8px" onchange="this.value?document.getElementById('tool-params').style.display='block':document.getElementById('tool-params').style.display='none'"></select>
        <div id="tool-params" style="display:none">
          <textarea id="tool-prms" placeholder='{"key":"value"}' style="width:100%;background:var(--surface2);border:none;border-radius:8px;padding:10px 14px;color:var(--text);font-family:monospace;font-size:13px;min-height:80px;margin-bottom:8px;resize:vertical"></textarea>
          <button class="btn btn-spark" style="margin-bottom:12px" onclick="runAdminTool()">▶ Run</button>
        </div>
        <pre id="tool-out" style="background:var(--surface2);border-radius:8px;padding:12px;font-family:monospace;font-size:12px;color:var(--spark);white-space:pre-wrap;min-height:60px;overflow-y:auto;max-height:300px"></pre>
      </div>
      <div id="ap-logs" class="apanel" style="padding:16px;overflow-y:auto">
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">
          <button class="btn btn-muted" style="min-height:36px;padding:6px 12px;font-size:12px;width:auto" onclick="loadLog('px-mind')">🧠 Mind</button>
          <button class="btn btn-muted" style="min-height:36px;padding:6px 12px;font-size:12px;width:auto" onclick="loadLog('px-wake-listen')">👂 Wake</button>
          <button class="btn btn-muted" style="min-height:36px;padding:6px 12px;font-size:12px;width:auto" onclick="loadLog('px-alive')">🤖 Alive</button>
          <button class="btn btn-muted" style="min-height:36px;padding:6px 12px;font-size:12px;width:auto" onclick="loadLog('tool-voice')">🔊 Voice</button>
        </div>
        <pre id="log-out" style="background:var(--surface2);border-radius:8px;padding:12px;font-family:monospace;font-size:11px;white-space:pre-wrap;overflow-y:auto;max-height:calc(100vh - 220px);line-height:1.5"></pre>
      </div>
      <div id="ap-parental" class="apanel" style="padding:16px;overflow-y:auto;display:flex;flex-direction:column;gap:12px">
        <div class="sec-hdr">Motion</div>
        <button class="btn btn-muted" id="btn-motion" onclick="toggleMotion()">Loading…</button>
        <div class="sec-hdr">Quiet mode</div>
        <button class="btn btn-muted" id="btn-quiet" onclick="toggleQuiet()">Loading…</button>
        <div class="sec-hdr">Persona</div>
        <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px">
          <button class="btn btn-muted" onclick="setPersona('spark')">🌟 spark</button>
          <button class="btn btn-muted" onclick="setPersona('gremlin')">👹 gremlin</button>
          <button class="btn btn-muted" onclick="setPersona('vixen')">🦊 vixen</button>
          <button class="btn btn-muted" onclick="setPersona('')">◯ none</button>
        </div>
        <div class="sec-hdr">Log an event</div>
        <input id="sh-mood" placeholder="Mood" style="background:var(--surface2);border:none;border-radius:8px;padding:10px 14px;color:var(--text);font-family:inherit;font-size:14px">
        <input id="sh-detail" placeholder="What happened?" style="background:var(--surface2);border:none;border-radius:8px;padding:10px 14px;color:var(--text);font-family:inherit;font-size:14px">
        <button class="btn btn-blue" onclick="logEvt()">📝 Log to sheets</button>
      </div>
    </div>
```

**JS to add:**
```javascript
let _pinOk=false;
function showPin(){if(_pinOk)return;document.getElementById('pin-gate').style.display='flex';document.getElementById('admin-body').style.display='none'}
async function subPin(){
  const pin=document.getElementById('pin-inp').value;
  try{const r=await api('/api/v1/pin/verify',{method:'POST',body:JSON.stringify({pin})});
    if(r.ok){_pinOk=true;document.getElementById('pin-gate').style.display='none';document.getElementById('admin-body').style.display='flex';loadSvcs();loadParental();initTools()}
    else{document.getElementById('pin-err').style.display='block';document.getElementById('pin-inp').value=''}}
  catch{document.getElementById('pin-err').style.display='block'}
}
function swA(name){
  document.querySelectorAll('.atab-btn').forEach(b=>b.classList.remove('active'));
  document.querySelectorAll('.apanel').forEach(p=>p.classList.remove('active'));
  document.getElementById('at-'+name).classList.add('active');
  document.getElementById('ap-'+name).classList.add('active');
  if(name==='logs')loadLog('px-mind');
}
async function loadSvcs(){
  const r=await api('/api/v1/services');
  const list=document.getElementById('svc-list');list.textContent='';
  (r.services||[]).forEach(s=>{
    const on=s.status==='active';
    const n=s.service.replace('px-','');
    const ico={alive:'🤖',mind:'🧠','wake-listen':'👂','api-server':'🌐'}[n]||'⚙️';
    const row=document.createElement('div');
    row.style.cssText='display:flex;align-items:center;gap:10px;background:var(--surface2);padding:12px 14px;border-radius:12px';
    const dot=document.createElement('span');dot.style.cssText='font-size:10px;color:'+(on?'var(--spark)':'var(--danger)');dot.textContent='●';
    const ic=document.createElement('span');ic.style.cssText='font-size:18px';ic.textContent=ico;
    const nm=document.createElement('span');nm.style.cssText='flex:1;font-weight:700;font-size:14px';nm.textContent=n;
    const rb=document.createElement('button');rb.className='btn btn-muted';rb.style.cssText='min-height:36px;padding:6px 12px;font-size:12px;width:auto';rb.textContent='↺ Restart';rb.onclick=()=>svcAct(s.service,'restart');
    const tb=document.createElement('button');tb.className='btn '+(on?'btn-danger':'btn-spark');tb.style.cssText='min-height:36px;padding:6px 12px;font-size:12px;width:auto';tb.textContent=on?'■ Stop':'▶ Start';tb.onclick=()=>svcAct(s.service,on?'stop':'start');
    row.appendChild(dot);row.appendChild(ic);row.appendChild(nm);row.appendChild(rb);row.appendChild(tb);
    list.appendChild(row);
  });
}
async function svcAct(svc,act){await api('/api/v1/services/'+svc+'/'+act,{method:'POST'});setTimeout(loadSvcs,1500)}
async function confirmDev(act){if(confirm('Really '+act+' the Pi?'))await api('/api/v1/device/'+act,{method:'POST'})}
async function loadParental(){
  const s=await api('/api/v1/session');
  document.getElementById('btn-motion').textContent=s.confirm_motion_allowed?'✅ Motion: ON':'🚫 Motion: OFF';
  document.getElementById('btn-motion').className='btn '+(s.confirm_motion_allowed?'btn-spark':'btn-danger');
  document.getElementById('btn-quiet').textContent=s.spark_quiet_mode?'🤫 Quiet: ON':'💬 Quiet: OFF';
}
async function toggleMotion(){const s=await api('/api/v1/session');await api('/api/v1/session',{method:'PATCH',body:JSON.stringify({confirm_motion_allowed:!s.confirm_motion_allowed})});loadParental()}
async function toggleQuiet(){const s=await api('/api/v1/session');await api('/api/v1/session',{method:'PATCH',body:JSON.stringify({spark_quiet_mode:!s.spark_quiet_mode})});loadParental()}
async function setPersona(p){await api('/api/v1/session',{method:'PATCH',body:JSON.stringify({persona:p})})}
async function logEvt(){const mood=document.getElementById('sh-mood').value;const detail=document.getElementById('sh-detail').value;if(!detail)return;await doTool('tool_gws_sheets_log',{event_type:'note',detail,mood});document.getElementById('sh-mood').value='';document.getElementById('sh-detail').value=''}
async function initTools(){const r=await api('/api/v1/tools');const sel=document.getElementById('tool-sel');sel.textContent='';const def=document.createElement('option');def.value='';def.textContent='— select a tool —';sel.appendChild(def);(r.tools||[]).forEach(t=>{const o=document.createElement('option');o.value=t;o.textContent=t.replace('tool_','').replace(/_/g,' ');sel.appendChild(o)})}
async function runAdminTool(){const tool=document.getElementById('tool-sel').value;if(!tool)return;let params={};try{params=JSON.parse(document.getElementById('tool-prms').value||'{}')}catch{}const r=await api('/api/v1/tool',{method:'POST',body:JSON.stringify({tool,params,dry:false})});document.getElementById('tool-out').textContent=JSON.stringify(r,null,2)}
async function loadLog(svc){const r=await api('/api/v1/logs/'+svc+'?lines=100');const pre=document.getElementById('log-out');pre.textContent=(r.lines||[]).join('\n');pre.scrollTop=pre.scrollHeight}
setInterval(()=>{if(_pinOk&&document.getElementById('panel-admin').classList.contains('active'))loadSvcs()},15000);
```

**Commit:**
```bash
git add src/pxh/api.py
git commit -m "feat(ui): Adrian panel — PIN, services, tools, logs, parental"
```

---

## Task 11: Final wiring + QA

**Step 1: Run full test suite**
```bash
.venv/bin/python -m pytest -m "not live" -q
# Expected: 110+ passed, 0 failed
```

**Step 2: Manual QA checklist (on real tablet/phone)**
- [ ] All 4 tabs switch correctly, no flicker
- [ ] Chat: type message, press Enter → reply appears with tool tag
- [ ] Actions "I need help": tap "Breathe with me" → switches to Chat, shows result
- [ ] Actions "Move SPARK!": Speed slider label updates; hold ▲ → SPARK moves; release → stops
- [ ] Actions "Move SPARK!": ⛔ button works immediately
- [ ] Actions: ⭕ Circle, ∞ Figure-8, 🎲 Wander all work
- [ ] SPARK tab: mood emoji visible, thought bubble shows a real thought, stats populate
- [ ] SPARK tab: mood colour changes when session mood is updated
- [ ] Admin: tapping tab shows PIN gate
- [ ] Admin: wrong PIN shows error, clears input
- [ ] Admin: correct PIN reveals panel
- [ ] Admin Services: all 4 services shown with status dots
- [ ] Admin Services: Restart button triggers restart, list refreshes
- [ ] Admin Services: Reboot Pi shows confirm dialog
- [ ] Admin Tools: select tool_status → Run → JSON output appears
- [ ] Admin Logs: tap Mind → log lines appear, most recent at bottom
- [ ] Admin Parental: Motion toggle changes state and button colour
- [ ] Admin Parental: Log to sheets calls tool_gws_sheets_log

**Step 3: Push**
```bash
git push
```

---

## Implementation Notes

- The entire frontend is the `_HTML_UI` Python string in `src/pxh/api.py`. Be careful with quote escaping — use single quotes in JS to avoid conflicting with Python's triple-double-quoted string.
- Always restart after changes: `sudo systemctl restart px-api-server`
- Test on real mobile device — desktop may look fine but touch targets need real testing.
- `doTool()` intentionally switches to Chat tab to show results — Obi sees responses in conversation context.
- PIN is stored in `.env` as `PX_ADMIN_PIN`. Add before Task 3.
- RC hold-repeat: 500ms interval, `tool_drive` duration 0.6s — slight overlap prevents motion gaps.
- The `svc-list` and similar elements use DOM methods (createElement, appendChild, textContent) to avoid XSS risks from innerHTML with untrusted content.
