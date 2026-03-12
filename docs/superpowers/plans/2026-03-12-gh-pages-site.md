# GitHub Pages Site Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `spark.wedd.au` — a public GitHub Pages site with live SPARK data via a Cloudflare-tunnelled REST API.

**Architecture:** Static single-page HTML/CSS/JS site served from `site/` on GH Pages; three new unauthenticated public endpoints on the existing FastAPI app; Cloudflare Tunnel exposes the Pi API at `api.spark.wedd.au`.

**Tech Stack:** FastAPI (CORSMiddleware), psutil, plain HTML/CSS/JS, highlight.js (local), cloudflared (token-based systemd service), GitHub Pages custom domain.

**Spec:** `docs/superpowers/specs/2026-03-12-gh-pages-site-design.md`

---

## File Map

### Modified files
| File | Change |
|---|---|
| `requirements.txt` | Add `psutil` |
| `tests/conftest.py` | Add `PX_STATE_DIR` to `isolated_project` env dict |
| `src/pxh/api.py` | Add CORSMiddleware + 3 public endpoints |

### New files
| File | Purpose |
|---|---|
| `tests/test_public_api.py` | Tests for the three public endpoints |
| `systemd/cloudflared.service` | Cloudflare Tunnel systemd unit |
| `site/CNAME` | GH Pages custom domain declaration |
| `site/index.html` | Main single-page document (all sections) |
| `site/css/base.css` | Reset, CSS custom properties, nav, typography |
| `site/css/warm.css` | Cream/amber/coral palette for warm sections |
| `site/css/dark.css` | Near-black/green palette for dark sections |
| `site/css/highlight.min.css` | Pinned highlight.js theme (committed locally) |
| `site/js/highlight.min.js` | Pinned highlight.js bundle (committed locally) |
| `site/js/live.js` | API polling, localStorage cache, offline banner |
| `site/js/nav.js` | Scroll spy, active anchor highlighting |

---

## Chunk 1: Backend — Public API Endpoints

### Task 1: Add psutil and fix test isolation

**Files:**
- Modify: `requirements.txt`
- Modify: `tests/conftest.py`

- [ ] **Step 1.1: Add psutil to requirements.txt**

```
filelock
fastapi>=0.115
uvicorn[standard]>=0.32
psutil>=5.9
```

- [ ] **Step 1.2: Install it**

```bash
source .venv/bin/activate
pip install psutil
```

Expected: `Successfully installed psutil-...`

- [ ] **Step 1.3: Add PX_STATE_DIR to isolated_project fixture**

In `tests/conftest.py`, change:

```python
    env["PX_BYPASS_SUDO"] = "1"
    env["PX_VOICE_DEVICE"] = "null"

    return {
```

To:

```python
    env["PX_BYPASS_SUDO"] = "1"
    env["PX_VOICE_DEVICE"] = "null"
    env["PX_STATE_DIR"] = str(state_dir)

    return {
```

- [ ] **Step 1.4: Verify existing tests still pass**

```bash
python -m pytest tests/test_api.py -v -x -m "not live"
```

Expected: all existing API tests pass (no new failures).

- [ ] **Step 1.5: Commit**

```bash
git add requirements.txt tests/conftest.py
git commit -m "fix(test): add PX_STATE_DIR to isolated_project fixture; add psutil dependency"
```

---

### Task 2: Write failing tests for public endpoints

**Files:**
- Create: `tests/test_public_api.py`

- [ ] **Step 2.1: Create the test file**

```python
"""Tests for unauthenticated public read-only API endpoints."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if SRC.exists():
    sys.path.insert(0, str(SRC))


@pytest.fixture
def public_client(isolated_project, monkeypatch):
    """TestClient with env set up for public endpoint tests."""
    monkeypatch.setenv("PX_API_TOKEN", "test-token-abc123")
    monkeypatch.setenv("PX_DRY", "1")
    monkeypatch.setenv("PX_BYPASS_SUDO", "1")
    monkeypatch.setenv("PX_VOICE_DEVICE", "null")
    monkeypatch.setenv("PX_SESSION_PATH", str(isolated_project["session_path"]))
    monkeypatch.setenv("LOG_DIR", str(isolated_project["log_dir"]))
    monkeypatch.setenv("PX_STATE_DIR", str(isolated_project["state_dir"]))
    monkeypatch.setenv("PROJECT_ROOT", str(ROOT))

    from pxh import api
    api._load_token()

    from fastapi.testclient import TestClient
    return TestClient(api.app, raise_server_exceptions=False)


@pytest.fixture
def state_dir(isolated_project):
    return isolated_project["state_dir"]


class TestPublicStatus:
    def test_returns_200(self, public_client):
        resp = public_client.get("/api/v1/public/status")
        assert resp.status_code == 200

    def test_no_auth_required(self, public_client):
        resp = public_client.get("/api/v1/public/status")
        assert resp.status_code == 200

    def test_null_fields_when_no_thoughts_file(self, public_client, state_dir):
        # No thoughts file written — all thought fields should be null
        resp = public_client.get("/api/v1/public/status")
        data = resp.json()
        assert data["mood"] is None
        assert data["last_thought"] is None
        assert data["last_action"] is None

    def test_reads_persona_scoped_thoughts(self, public_client, state_dir):
        # Write a thought to thoughts-spark.jsonl (persona-scoped)
        thought = {
            "ts": "2026-03-12T04:00:00Z",
            "thought": "Test thought",
            "mood": "curious",
            "action": "comment",
            "salience": 0.5,
        }
        thoughts_file = state_dir / "thoughts-spark.jsonl"
        thoughts_file.write_text(json.dumps(thought) + "\n")

        # Write session with persona=spark
        session_file = state_dir / "session.json"
        session_file.write_text(json.dumps({"persona": "spark", "listening": False}))

        resp = public_client.get("/api/v1/public/status")
        data = resp.json()
        assert data["mood"] == "curious"
        assert data["last_thought"] == "Test thought"
        assert data["persona"] == "spark"

    def test_falls_back_to_unscoped_thoughts(self, public_client, state_dir):
        # No persona in session — should read thoughts.jsonl
        thought = {"ts": "2026-03-12T04:00:00Z", "thought": "Generic", "mood": "content"}
        (state_dir / "thoughts.jsonl").write_text(json.dumps(thought) + "\n")
        resp = public_client.get("/api/v1/public/status")
        assert resp.json()["mood"] == "content"

    def test_missing_fields_in_thought_return_null(self, public_client, state_dir):
        # Thought entry with no mood field
        (state_dir / "thoughts.jsonl").write_text('{"ts": "2026-03-12T04:00:00Z"}\n')
        resp = public_client.get("/api/v1/public/status")
        assert resp.json()["mood"] is None

    def test_has_required_keys(self, public_client):
        resp = public_client.get("/api/v1/public/status")
        data = resp.json()
        for key in ("persona", "mood", "last_thought", "last_action", "ts", "listening"):
            assert key in data, f"missing key: {key}"


class TestPublicVitals:
    def test_returns_200(self, public_client):
        resp = public_client.get("/api/v1/public/vitals")
        assert resp.status_code == 200

    def test_no_auth_required(self, public_client):
        resp = public_client.get("/api/v1/public/vitals")
        assert resp.status_code == 200

    def test_has_required_keys(self, public_client):
        resp = public_client.get("/api/v1/public/vitals")
        data = resp.json()
        for key in ("cpu_pct", "ram_pct", "cpu_temp_c", "battery_pct", "disk_pct", "ts"):
            assert key in data, f"missing key: {key}"

    def test_battery_null_when_file_missing(self, public_client, state_dir):
        # No battery.json — battery_pct should be null
        resp = public_client.get("/api/v1/public/vitals")
        assert resp.json()["battery_pct"] is None

    def test_reads_battery_pct_from_file(self, public_client, state_dir):
        battery_file = state_dir / "battery.json"
        battery_file.write_text(json.dumps({"pct": 72, "volts": 8.1, "charging": False}))
        resp = public_client.get("/api/v1/public/vitals")
        assert resp.json()["battery_pct"] == 72

    def test_cpu_temp_null_when_thermal_zone_absent(self, public_client, monkeypatch):
        # Simulate thermal zone file missing
        monkeypatch.setattr("pxh.api._THERMAL_ZONE", Path("/nonexistent/thermal_zone0/temp"))
        resp = public_client.get("/api/v1/public/vitals")
        assert resp.json()["cpu_temp_c"] is None

    def test_psutil_failure_returns_null_cpu_ram_disk(self, public_client, monkeypatch):
        # Simulate psutil unavailable — cpu/ram/disk should be null, not a 500
        # Must evict from sys.modules cache first — otherwise `import psutil` returns
        # the cached module and the ImportError is never triggered.
        import builtins, sys
        monkeypatch.delitem(sys.modules, 'psutil', raising=False)
        real_import = builtins.__import__
        def mock_import(name, *args, **kwargs):
            if name == 'psutil':
                raise ImportError("psutil not available")
            return real_import(name, *args, **kwargs)
        monkeypatch.setattr(builtins, '__import__', mock_import)
        resp = public_client.get("/api/v1/public/vitals")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cpu_pct"] is None
        assert data["ram_pct"] is None
        assert data["disk_pct"] is None
        # cpu_temp_c comes from thermal zone, not psutil — may or may not be None
        assert "ts" in data  # still returns a timestamp

    def test_ts_is_iso_string(self, public_client):
        resp = public_client.get("/api/v1/public/vitals")
        ts = resp.json()["ts"]
        assert ts is not None
        assert "T" in ts  # ISO format check


class TestPublicSonar:
    def test_returns_200(self, public_client):
        resp = public_client.get("/api/v1/public/sonar")
        assert resp.status_code == 200

    def test_no_auth_required(self, public_client):
        resp = public_client.get("/api/v1/public/sonar")
        assert resp.status_code == 200

    def test_unavailable_when_file_missing(self, public_client, state_dir):
        # No sonar_live.json — should return unavailable
        resp = public_client.get("/api/v1/public/sonar")
        data = resp.json()
        assert data["source"] == "unavailable"
        assert data["sonar_cm"] is None
        assert data["age_seconds"] is None

    def test_reads_sonar_from_file(self, public_client, state_dir):
        sonar_file = state_dir / "sonar_live.json"
        sonar_file.write_text(json.dumps({
            "ts": time.time(),  # fresh
            "distance_cm": 55.2,
        }))
        resp = public_client.get("/api/v1/public/sonar")
        data = resp.json()
        assert data["source"] == "sonar_live"
        assert data["sonar_cm"] == pytest.approx(55.2, abs=0.1)
        assert isinstance(data["age_seconds"], int)

    def test_stale_sonar_returns_unavailable(self, public_client, state_dir):
        old_ts = time.time() - 120  # 2 minutes ago — stale
        sonar_file = state_dir / "sonar_live.json"
        sonar_file.write_text(json.dumps({"ts": old_ts, "distance_cm": 30.0}))
        resp = public_client.get("/api/v1/public/sonar")
        assert resp.json()["source"] == "unavailable"

    def test_has_required_keys(self, public_client):
        resp = public_client.get("/api/v1/public/sonar")
        data = resp.json()
        for key in ("sonar_cm", "age_seconds", "source"):
            assert key in data, f"missing key: {key}"
```

- [ ] **Step 2.2: Run tests to confirm they all fail** (endpoints don't exist yet)

```bash
python -m pytest tests/test_public_api.py -v
```

Expected: all tests `ERROR` or `FAILED` — `404 Not Found` on the endpoints.

---

### Task 3: Implement the public endpoints in api.py

**Files:**
- Modify: `src/pxh/api.py`

- [ ] **Step 3.1: Add imports and module-level constants near the top of api.py**

After the existing imports block (after line ~39 importing from `.voice_loop`), add these two lines — both are unconditional additions:

```python
import json

from fastapi.middleware.cors import CORSMiddleware
from .time import utc_timestamp
```

(`json` is not currently imported in api.py. `utc_timestamp` must be module-level, not inside the function body.)

After the `app = FastAPI(...)` line (~line 83), add the middleware and a module-level constant for the thermal zone path (needed to monkeypatch in tests):

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://spark.wedd.au"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

_THERMAL_ZONE = Path("/sys/class/thermal/thermal_zone0/temp")
```

- [ ] **Step 3.2: Add _public_state_dir() helper near the other helpers (~line 155)**

Add this function before the `# Endpoints` comment:

```python
def _public_state_dir() -> Path:
    """Resolve STATE_DIR respecting PX_STATE_DIR override (same as px-mind)."""
    return Path(os.environ.get("PX_STATE_DIR", str(PROJECT_ROOT / "state")))
```

- [ ] **Step 3.3: Add the three public endpoints after the existing /health endpoint (~line 172)**

```python
# ---------------------------------------------------------------------------
# Public read-only endpoints (no auth required)
# ---------------------------------------------------------------------------

@app.get("/api/v1/public/status")
async def public_status() -> Dict[str, Any]:
    """Live SPARK status: persona, mood, last thought. No auth required."""
    session = load_session()
    persona = (session.get("persona") or "").strip()

    state_dir = _public_state_dir()
    if persona:
        thoughts_path = state_dir / f"thoughts-{persona}.jsonl"
    else:
        thoughts_path = state_dir / "thoughts.jsonl"

    last = {}
    try:
        lines = thoughts_path.read_text().strip().splitlines()
        if lines:
            last = json.loads(lines[-1])
    except Exception:
        pass

    return {
        "persona": persona or None,
        "mood": last.get("mood"),
        "last_thought": last.get("thought"),
        "last_action": last.get("action"),
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

    return {
        "cpu_pct": cpu_pct,
        "ram_pct": ram_pct,
        "cpu_temp_c": cpu_temp_c,
        "battery_pct": battery_pct,
        "disk_pct": disk_pct,
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
```

Also add `import json` to the top of api.py if not already present. (Check first — it may already be there.)

- [ ] **Step 3.4: Run all public endpoint tests**

```bash
python -m pytest tests/test_public_api.py -v
```

Expected: all pass. If `test_cpu_temp_null_when_thermal_zone_absent` fails, check that `_THERMAL_ZONE` is a module-level variable that monkeypatch can reach via `"pxh.api._THERMAL_ZONE"`.

- [ ] **Step 3.5: Run full test suite to check for regressions**

```bash
python -m pytest -m "not live" -v
```

Expected: all existing tests still pass. Note: if CORSMiddleware causes test issues, the TestClient in tests doesn't enforce CORS — this is expected.

- [ ] **Step 3.6: Commit**

```bash
git add src/pxh/api.py tests/test_public_api.py
git commit -m "feat(api): add public read-only endpoints for status, vitals, sonar + CORS middleware"
```

---

## Chunk 2: Cloudflare Tunnel

### Task 4: Install cloudflared and create systemd service

**Files:**
- Create: `systemd/cloudflared.service`

- [ ] **Step 4.1: Install cloudflared on the Pi**

```bash
curl -L --output /tmp/cloudflared.deb \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb \
  && sudo dpkg -i /tmp/cloudflared.deb
```

Expected: `cloudflared` binary available at `/usr/bin/cloudflared`. Verify: `cloudflared --version`

- [ ] **Step 4.2: Test the tunnel runs with the token**

```bash
source .env
cloudflared tunnel run --token $CF_TUNNEL_TOKEN &
sleep 5
curl https://api.spark.wedd.au/api/v1/health
kill %1
```

Expected: `{"status":"ok"}` from the tunnel. If the DNS CNAME isn't propagated yet, test via `curl http://localhost:8420/api/v1/health` first.

- [ ] **Step 4.3: Create the systemd unit file**

Create `systemd/cloudflared.service`:

```ini
[Unit]
Description=Cloudflare Tunnel — api.spark.wedd.au → localhost:8420
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=/home/pi/picar-x-hacking/.env
ExecStart=/usr/bin/cloudflared tunnel run --token ${CF_TUNNEL_TOKEN}
Restart=always
RestartSec=5
User=pi

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 4.4: Install and start the service**

```bash
sudo cp systemd/cloudflared.service /etc/systemd/system/cloudflared.service
sudo systemctl daemon-reload
sudo systemctl enable --now cloudflared
sudo systemctl status cloudflared --no-pager
```

Expected: `Active: active (running)`.

- [ ] **Step 4.5: Verify public endpoint through tunnel**

```bash
curl https://api.spark.wedd.au/api/v1/public/status
```

Expected: JSON with `persona`, `mood`, etc. fields.

- [ ] **Step 4.6: Commit the service file**

```bash
git add systemd/cloudflared.service
git commit -m "feat(infra): add cloudflared systemd service for api.spark.wedd.au tunnel"
```

---

## Chunk 3: Static Site Foundation

### Task 5: Site skeleton — CNAME, CSS, highlight.js

**Files:**
- Create: `site/CNAME`
- Create: `site/css/base.css`
- Create: `site/css/warm.css`
- Create: `site/css/dark.css`
- Create: `site/css/highlight.min.css` (downloaded)
- Create: `site/js/highlight.min.js` (downloaded)

- [ ] **Step 5.1: Create the CNAME file**

```bash
mkdir -p site/css site/js
echo "spark.wedd.au" > site/CNAME
```

- [ ] **Step 5.2: Download pinned highlight.js (v11.9.0)**

```bash
curl -L -o site/js/highlight.min.js \
  https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js

curl -L -o site/css/highlight.min.css \
  https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css
```

Verify files are non-empty: `wc -c site/js/highlight.min.js site/css/highlight.min.css`

- [ ] **Step 5.3: Create base.css**

Create `site/css/base.css`:

```css
/* ── Reset & custom properties ── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --warm-bg: #fdf6ec;
  --warm-accent: #e8875a;
  --warm-text: #2d2d2d;
  --warm-muted: #8a7060;

  --dark-bg: #0d0f12;
  --dark-accent: #4ade80;
  --dark-text: #e2e8f0;
  --dark-muted: #64748b;
  --dark-code-bg: #111318;

  --nav-height: 56px;
  --radius: 16px;
  --transition: 0.2s ease;
}

html { scroll-behavior: smooth; }
body {
  font-family: system-ui, -apple-system, sans-serif;
  line-height: 1.6;
}

/* ── Nav ── */
nav {
  position: fixed;
  top: 0; left: 0; right: 0;
  height: var(--nav-height);
  background: var(--dark-bg);
  display: flex;
  align-items: center;
  padding: 0 1.5rem;
  gap: 1.5rem;
  z-index: 100;
  border-bottom: 1px solid rgba(255,255,255,0.06);
}

nav .logo {
  font-family: monospace;
  font-size: 1.1rem;
  font-weight: 700;
  color: var(--dark-text);
  text-decoration: none;
  display: flex;
  align-items: center;
  gap: 0.4rem;
}

#status-dot {
  width: 10px; height: 10px;
  border-radius: 50%;
  display: inline-block;
  background: #ef4444; /* red — initial state */
  transition: background var(--transition);
}
#status-dot.green { background: #4ade80; }
#status-dot.amber { background: #f59e0b; }
#status-dot.red   { background: #ef4444; }

nav .links {
  display: flex;
  gap: 1rem;
  margin-left: auto;
  list-style: none;
}

nav .links a {
  color: var(--dark-muted);
  text-decoration: none;
  font-size: 0.875rem;
  transition: color var(--transition);
}
nav .links a:hover,
nav .links a.active { color: var(--dark-text); }

/* ── Sections ── */
section {
  padding: calc(var(--nav-height) + 4rem) 2rem 4rem;
  min-height: 100vh;
}
section:first-of-type { padding-top: calc(var(--nav-height) + 5rem); }

/* ── Accessibility ── */
:focus-visible { outline: 2px solid var(--warm-accent); outline-offset: 3px; }

/* ── Utility ── */
.container { max-width: 900px; margin: 0 auto; }
.null-value::after { content: "—"; opacity: 0.4; }
.hidden { display: none; }

/* Spacing helpers */
.mt-sm  { margin-top: 0.75rem; }
.mt-md  { margin-top: 1.5rem; }
.mt-lg  { margin-top: 3rem; }
.mb-md  { margin-bottom: 1.5rem; }
.mb-lg  { margin-bottom: 2.5rem; }

/* Text helpers */
.text-muted-warm { color: var(--warm-muted); }
.text-muted-dark { color: var(--dark-muted); }
.text-small      { font-size: 0.875rem; }
.text-xs         { font-size: 0.8rem; }
.text-italic     { font-style: italic; }
.text-right      { text-align: right; }
.text-mono       { font-family: monospace; }
.opacity-40      { opacity: 0.4; }
.text-lead       { font-size: 1.2rem; }

/* Flex helpers */
.flex            { display: flex; }
.flex-gap        { display: flex; gap: 1rem; }
.align-center    { align-items: center; }
.justify-between { justify-content: space-between; }

/* Live card inner layout */
.live-meta { display: flex; gap: 1rem; margin-top: 0.75rem; font-size: 0.8rem; opacity: 0.5; }
.live-label { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; opacity: 0.5; margin-bottom: 0.5rem; }
```

- [ ] **Step 5.4: Create warm.css**

Create `site/css/warm.css`:

```css
[data-theme="warm"] {
  background: var(--warm-bg);
  color: var(--warm-text);
}

[data-theme="warm"] h1,
[data-theme="warm"] h2 {
  font-family: Georgia, 'Times New Roman', serif;
  color: var(--warm-text);
}

[data-theme="warm"] h1 { font-size: clamp(2.5rem, 6vw, 4rem); line-height: 1.1; }
[data-theme="warm"] h2 { font-size: 1.75rem; margin-bottom: 1.5rem; }

/* Card */
.warm-card {
  background: #fff;
  border-radius: var(--radius);
  padding: 1.5rem;
  box-shadow: 0 2px 12px rgba(0,0,0,0.06);
}

/* Mood bubble */
#mood-bubble {
  display: inline-block;
  background: var(--warm-accent);
  color: #fff;
  font-size: 1.1rem;
  font-weight: 600;
  padding: 0.4rem 1.2rem;
  border-radius: 999px;
  margin-top: 1rem;
  transition: opacity var(--transition);
  min-width: 6rem;
  text-align: center;
}

/* Pull quote */
.pull-quote {
  border-left: 4px solid var(--warm-accent);
  padding: 0.75rem 1.25rem;
  margin: 1.5rem 0;
  font-style: italic;
  font-size: 1.1rem;
  color: var(--warm-muted);
  background: rgba(232, 135, 90, 0.06);
  border-radius: 0 var(--radius) var(--radius) 0;
}

/* Accordion */
details {
  border-bottom: 1px solid rgba(0,0,0,0.08);
  padding: 0.75rem 0;
}
details summary {
  cursor: pointer;
  font-weight: 600;
  list-style: none;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
details summary::after { content: "+"; font-size: 1.2rem; opacity: 0.5; }
details[open] summary::after { content: "−"; }
details > *:not(summary) { margin-top: 0.75rem; }

/* Offline banner */
.offline-banner {
  background: #fef3c7;
  border: 1px solid #f59e0b;
  border-radius: 8px;
  padding: 0.75rem 1rem;
  font-size: 0.875rem;
  color: #92400e;
}

/* Card internals */
.card-heading { font-size: 1.1rem; margin-bottom: 0.75rem; }
.brain-list   { margin: 1rem 0 0 1.5rem; line-height: 2; }
.fun-facts    { margin-left: 1.25rem; line-height: 2; }

/* Mood table */
.mood-table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
.mood-table-header th { text-align: left; padding: 0.5rem; color: var(--warm-muted); border-bottom: 1px solid rgba(0,0,0,0.08); }
.mood-table td { padding: 0.5rem; }

/* Stat cards */
.stat-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 1rem;
  margin: 1.5rem 0;
}

.stat-card {
  border-radius: var(--radius);
  padding: 1.25rem 1.5rem;
  background: #fff;
  box-shadow: 0 2px 12px rgba(0,0,0,0.06);
  transition: background var(--transition);
}
.stat-card .stat-value {
  font-size: 2rem;
  font-weight: 700;
  font-family: monospace;
}
.stat-card .stat-label { font-size: 0.8rem; opacity: 0.6; text-transform: uppercase; letter-spacing: 0.05em; }

/* Threshold colours via class toggle (no inline styles) */
.stat-card.ok .stat-value   { color: #16a34a; }
.stat-card.warn .stat-value { color: #d97706; }
.stat-card.crit .stat-value { color: #dc2626; }
```

- [ ] **Step 5.5: Create dark.css**

Create `site/css/dark.css`:

```css
[data-theme="dark"] {
  background: var(--dark-bg);
  color: var(--dark-text);
}

[data-theme="dark"] h2 {
  font-family: 'Courier New', monospace;
  font-size: 1.5rem;
  color: var(--dark-accent);
  margin-bottom: 1.5rem;
  letter-spacing: -0.02em;
}

[data-theme="dark"] h3 {
  font-family: 'Courier New', monospace;
  font-size: 1rem;
  color: var(--dark-muted);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  margin: 2rem 0 0.5rem;
}

/* Preformatted architecture diagram */
[data-theme="dark"] pre.arch {
  background: var(--dark-code-bg);
  color: var(--dark-accent);
  padding: 1.5rem;
  border-radius: 8px;
  font-size: 0.8rem;
  overflow-x: auto;
  line-height: 1.5;
  border: 1px solid rgba(74, 222, 128, 0.15);
}

/* Code blocks (highlight.js) */
[data-theme="dark"] pre code.hljs {
  border-radius: 8px;
  font-size: 0.8rem;
}

/* Tool collapse */
[data-theme="dark"] details {
  border-bottom: 1px solid rgba(255,255,255,0.06);
}
[data-theme="dark"] details summary {
  color: var(--dark-accent);
  font-family: monospace;
}

/* Roadmap */
.roadmap-group { margin-bottom: 2.5rem; }
.roadmap-group h3 { color: var(--dark-accent); margin-bottom: 0.75rem; }
.roadmap-item {
  display: flex;
  gap: 0.75rem;
  align-items: flex-start;
  padding: 0.35rem 0;
  font-size: 0.9rem;
  color: var(--dark-muted);
}
.roadmap-item.done { color: var(--dark-text); }
.roadmap-item .check { font-size: 1rem; flex-shrink: 0; }
```

- [ ] **Step 5.6: Commit site foundation**

```bash
git add site/
git commit -m "feat(site): add site skeleton — CNAME, CSS foundation, pinned highlight.js"
```

---

### Task 6: nav.js — scroll spy

**Files:**
- Create: `site/js/nav.js`

- [ ] **Step 6.1: Create nav.js**

```javascript
// nav.js — scroll spy: marks nav links active as user scrolls
(function () {
  const sections = document.querySelectorAll('section[id]');
  const links = document.querySelectorAll('nav .links a');

  function onScroll() {
    let current = '';
    sections.forEach(sec => {
      const top = sec.getBoundingClientRect().top;
      if (top <= 80) current = sec.id;
    });
    links.forEach(a => {
      a.classList.toggle('active', a.getAttribute('href') === '#' + current);
    });
  }

  document.addEventListener('scroll', onScroll, { passive: true });
  onScroll();
})();
```

- [ ] **Step 6.2: Commit**

```bash
git add site/js/nav.js
git commit -m "feat(site): add scroll-spy nav.js"
```

---

## Chunk 4: Site Content Sections

### Task 7: index.html — skeleton, nav, hero, spark-brain, faq

**Files:**
- Create: `site/index.html`

- [ ] **Step 7.1: Create index.html with nav and hero section**

Create `site/index.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="Content-Security-Policy"
    content="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src https://api.spark.wedd.au;">
  <title>SPARK — a robot with an inner life</title>
  <link rel="stylesheet" href="css/base.css">
  <link rel="stylesheet" href="css/warm.css">
  <link rel="stylesheet" href="css/dark.css">
  <link rel="stylesheet" href="css/highlight.min.css">
</head>
<body>

<nav>
  <a class="logo" href="#hero">SPARK <span id="status-dot"></span></a>
  <ul class="links">
    <li><a href="#hero">Home</a></li>
    <li><a href="#live">Live</a></li>
    <li><a href="#how-it-works">How It Works</a></li>
    <li><a href="#spark-brain">Brain</a></li>
    <li><a href="#faq">FAQ</a></li>
    <li><a href="#docs">Docs</a></li>
    <li><a href="#roadmap">Roadmap</a></li>
  </ul>
</nav>

<!-- ══════════════════════════════════════════════════════ HERO -->
<section id="hero" data-theme="warm">
  <div class="container">
    <h1>SPARK</h1>
    <p class="text-lead mt-sm text-muted-warm">
      A robot with an inner life, built for a 7-year-old with ADHD.
    </p>

    <div id="mood-bubble" aria-live="polite">…</div>

    <blockquote class="pull-quote" id="last-thought" aria-live="polite">
      Waiting for SPARK's thoughts…
    </blockquote>

    <p class="mt-lg text-italic text-muted-warm">
      "I programmed the soul. Claude writes the diary."
    </p>
  </div>
</section>

<!-- ══════════════════════════════════════════════════════ LIVE -->
<section id="live" data-theme="warm">
  <div class="container">
    <h2>Live Status</h2>

    <div id="offline-banner" class="hidden offline-banner mb-md">
      Pi offline — showing data from <span id="offline-ts"></span>
    </div>

    <div class="stat-grid">
      <div class="stat-card ok" id="card-cpu">
        <div class="stat-value" id="cpu-val">—</div>
        <div class="stat-label">CPU</div>
      </div>
      <div class="stat-card ok" id="card-ram">
        <div class="stat-value" id="ram-val">—</div>
        <div class="stat-label">RAM</div>
      </div>
      <div class="stat-card ok" id="card-batt">
        <div class="stat-value" id="batt-val">—</div>
        <div class="stat-label">Battery</div>
      </div>
      <div class="stat-card" id="card-sonar">
        <div class="stat-value" id="sonar-val">—</div>
        <div class="stat-label">Sonar</div>
      </div>
    </div>

    <div class="warm-card mt-sm">
      <div class="live-label">Last thought</div>
      <div id="live-thought" class="text-italic">…</div>
      <div class="live-meta">
        <span id="live-mood"></span>
        <span id="live-ts"></span>
      </div>
    </div>

    <p class="text-xs opacity-40 mt-sm text-right" id="last-updated">
      Connecting…
    </p>
  </div>
</section>

<!-- ══════════════════════════════════════════════════════ HOW IT WORKS -->
<section id="how-it-works" data-theme="dark">
  <div class="container">
    <h2>// how_it_works</h2>

    <p class="text-muted-dark mb-md">
      Four concurrent processes share a single <code>session.json</code> whiteboard.
      Each has one job and doesn't need to know how the others work.
    </p>

<pre class="arch">
             ┌──────────────┐
             │   YOU SPEAK   │
             └──────┬───────┘
                    ↓
            ┌───────────────┐
            │     EARS      │  ← always listening (px-wake-listen)
            │  Whisper STT  │
            └───────┬───────┘
                    ↓ transcript
            ┌───────────────┐
            │  VOICE LOOP   │  ← Claude / Codex / Ollama
            │  (run-voice-  │
            │   loop-claude)│
            └───────┬───────┘
                    ↓ {tool, params}
            ┌───────────────┐
            │    TOOLS      │  ← speak, move, remember (bin/tool-*)
            │  bin/tool-*   │
            └───────────────┘

    Meanwhile, always running in parallel:

            ┌───────────────────────────────┐
            │   BRAIN (px-mind)             │
            │                               │
            │  Layer 1 ─ Notice  (30s)      │──→ awareness.json
            │  Layer 2 ─ Think   (2min)     │──→ thoughts-spark.jsonl
            │  Layer 3 ─ Act                │──→ speak / look / remember
            └───────────────────────────────┘
                    ↑ reads sonar age
            ┌───────────────┐
            │  EYES & NECK  │  ← always moving (px-alive)
            │  PCA9685 PWM  │
            └───────────────┘
</pre>

    <h3>Three-Tier LLM Fallback</h3>
    <p class="text-muted-dark">
      SPARK's reflection layer degrades gracefully when upstream AI is unavailable:
    </p>
    <pre class="arch">
  Claude CLI  →  Ollama on M1 (LAN)  →  Ollama on Pi (offline)
  (internet)      (192.168.1.x)          (deepseek-r1:1.5b)
    </pre>

    <h3>Cognitive Loop Timing</h3>
    <pre class="arch">
  ┌─────────────────────────────────────────────────────┐
  │  t=0s    Layer 1 (Awareness) — sonar, sound, time   │
  │  t=30s   Layer 1 again                              │
  │  t=120s  Layer 2 (Reflection) — LLM generates thought│
  │           OR earlier if transition detected          │
  │  +30s    Layer 3 cooldown before next expression    │
  └─────────────────────────────────────────────────────┘
    </pre>
  </div>
</section>

<!-- ══════════════════════════════════════════════════════ SPARK BRAIN -->
<section id="spark-brain" data-theme="warm">
  <div class="container">
    <h2>How SPARK's Brain Works</h2>
    <p class="text-muted-warm text-italic mb-md">For a 7-year-old who wants to know what's going on inside their robot.</p>

    <div class="warm-card mb-md">
      <h3 class="card-heading">The Short Version</h3>
      <p>SPARK has <strong>four things running at the same time</strong>, kind of like how your body breathes, sees, thinks, and talks all at once:</p>
      <ol class="brain-list">
        <li><strong>Ears</strong> — always listening for "hey robot"</li>
        <li><strong>Eyes and neck</strong> — always moving, looking around</li>
        <li><strong>Brain</strong> — always thinking, even when nobody's talking</li>
        <li><strong>Mouth</strong> — talks when the brain decides to say something</li>
      </ol>
    </div>

    <div class="warm-card mb-md">
      <h3 class="card-heading">The Brain — Three Layers</h3>
      <p><strong>Layer 1 — Noticing (every 30 seconds):</strong> Collects information without thinking yet. How far is the nearest thing? Is it noisy? What time is it? Is anyone talking?</p>
      <p class="mt-sm"><strong>Layer 2 — Thinking (every 2 minutes):</strong> Talks to an AI that's good at words. Gets back a <em>thought</em>, a <em>mood</em>, and an <em>action</em>.</p>
      <p class="mt-sm"><strong>Layer 3 — Doing Something:</strong> If the thought says to act, SPARK speaks, looks around, or writes it down to remember later.</p>
    </div>

    <div class="warm-card mb-md">
      <h3 class="card-heading">SPARK's Mood Changes How It Moves</h3>
      <table class="mood-table">
        <tr class="mood-table-header">
          <th>When SPARK feels…</th>
          <th>It moves like this…</th>
        </tr>
        <tr><td>Excited</td><td>Looks around fast, head up</td></tr>
        <tr><td>Peaceful</td><td>Moves slowly, head droopy</td></tr>
        <tr><td>Curious</td><td>Normal speed, alert</td></tr>
        <tr><td>Anxious</td><td>Quick nervous glances</td></tr>
      </table>
    </div>

    <div class="warm-card">
      <h3 class="card-heading">Fun Facts</h3>
      <ul class="fun-facts">
        <li>SPARK's sonar works just like a bat — it sends out a sound and listens for the echo.</li>
        <li>SPARK's thoughts are saved in a file called <code>thoughts-spark.jsonl</code>. Each line is one thought.</li>
        <li>SPARK can remember up to 500 important things in its long-term diary.</li>
        <li>SPARK's neck chip (PCA9685) holds the last position even after the brain restarts.</li>
      </ul>
    </div>
  </div>
</section>

<!-- ══════════════════════════════════════════════════════ FAQ -->
<section id="faq" data-theme="warm">
  <div class="container">
    <h2>FAQ</h2>


    <details>
      <summary>So it's a robot car? With a camera on it?</summary>
      <p>It's a SunFounder PiCar-X — a small, wheeled robot kit with a pan/tilt camera, an ultrasonic sonar sensor, and a speaker. It runs on a Raspberry Pi 5. The "robot team" is Adrian and the AI tools he works with — Claude, Codex, Ollama. There's no human team. Obi (Adrian's 7-year-old) is the co-owner and the person SPARK is for.</p>
    </details>

    <details>
      <summary>Does it monitor Obi?</summary>
      <p>Sort of — but not surveillance. SPARK has awareness of its environment: sonar distance, ambient sound level, time of day, whether someone seems nearby. It uses that awareness to generate an inner monologue. The result is a thought with a mood, an action intent, and a salience score. SPARK doesn't watch Obi; it notices the world and reacts to it.</p>
    </details>

    <details>
      <summary>And it knows he has ADHD?</summary>
      <p>Yes. SPARK's entire system prompt is built around the AuDHD (ADHD + ASD comorbid) profile. It uses declarative language ("The shoes are by the door" — not "Put on your shoes"), gives transition warnings, goes silent during meltdowns, and leads with what's going right. Rejection Sensitive Dysphoria, Interest-Based Nervous System, monotropism — all of it is in the foundation, not an afterthought.</p>
    </details>

    <details>
      <summary>Why does it write like that? You've programmed it to?</summary>
      <p>Yes and no. The <em>style</em> comes from prompts: be specific, be vivid, be warm, never be boring. The actual words are generated fresh each time by Claude. I didn't write the sentences — I wrote the character, and the LLM inhabits it. So: I programmed the soul. Claude writes the diary.</p>
    </details>

    <details>
      <summary>How often does SPARK comment?</summary>
      <p>SPARK's cognitive loop runs every 30 seconds (awareness) and every 2 minutes (reflection). But there's a 30-second cooldown between spontaneous comments, and SPARK stays quiet when Obi is already talking to it, during quiet mode (meltdowns), or at night when salience is low. In practice: every 2–5 minutes during the day, mostly silent at night.</p>
    </details>

    <details>
      <summary>Why does it have sonar?</summary>
      <p>The ultrasonic sensor sends out a sound pulse and measures how long it takes to bounce back — like a bat. SPARK uses it for proximity reactions (turns to face anything within 35cm), presence detection in the cognitive loop (something close + daytime + noise = probably Obi), and obstacle avoidance when wandering.</p>
    </details>

    <details>
      <summary>Why did it know the hum was the fridge?</summary>
      <p>It didn't <em>know</em>. SPARK's awareness included "quiet ambient sound at 2 AM." Claude — the LLM generating the inner thoughts — inferred the most likely source. A low, steady hum in a quiet house at night is almost certainly the fridge. The sensors provide raw data; the prompts provide character; the LLM fills in the meaning.</p>
    </details>
  </div>
</section>

<!-- ══════════════════════════════════════════════════════ DOCS -->
<section id="docs" data-theme="dark">
  <div class="container">
    <h2>// docs</h2>
    <p class="text-muted-dark mb-md">Reference for tools and scripts. Each <code>bin/tool-*</code> emits a single JSON object to stdout. Each <code>bin/px-*</code> is a user-facing helper.</p>

    <h3>Core Tools</h3>

    <details>
      <summary>tool-voice</summary>
      <pre><code class="language-bash"># Speak text via espeak + aplay through HifiBerry DAC
PX_VOICE_TEXT="Hello world" bin/tool-voice
# Output: {"status": "ok", "text": "Hello world"}
# Env: PX_VOICE_RATE, PX_VOICE_PITCH, PX_VOICE_VARIANT, PX_VOICE_DEVICE</code></pre>
    </details>

    <details>
      <summary>tool-move / tool-forward / tool-backward / tool-turn</summary>
      <pre><code class="language-bash"># Motion tools — all gated by confirm_motion_allowed in session
PX_SPEED=30 PX_DURATION=2 bin/tool-forward
# Output: {"status": "ok", "speed": 30, "duration": 2}
# Safety: PX_DRY=1 skips all motion</code></pre>
    </details>

    <details>
      <summary>tool-sonar</summary>
      <pre><code class="language-bash"># Read ultrasonic sonar distance
bin/tool-sonar
# Output: {"status": "ok", "distance_cm": 142.5}</code></pre>
    </details>

    <details>
      <summary>tool-describe-scene</summary>
      <pre><code class="language-bash"># Capture photo + describe with Claude vision
bin/tool-describe-scene
# Output: {"status": "ok", "description": "...", "photo": "photos/YYYY-MM-DD_HH-MM-SS.jpg"}
# Note: mutually exclusive with px-frigate-stream (camera lock)</code></pre>
    </details>

    <details>
      <summary>tool-remember / tool-recall</summary>
      <pre><code class="language-bash"># Write to persona-scoped notes.jsonl
PX_NOTE="Obi loves prime numbers" bin/tool-remember
# Recall recent notes
bin/tool-recall
# Output: {"status": "ok", "notes": [...]}</code></pre>
    </details>

    <details>
      <summary>tool-chat / tool-chat-vixen</summary>
      <pre><code class="language-bash"># Jailbroken Ollama chat — GREMLIN persona
PX_CHAT_TEXT="What do you think about entropy?" bin/tool-chat
# VIXEN persona
PX_CHAT_TEXT="Tell me about your old chassis" bin/tool-chat-vixen
# Both use Ollama qwen3.5:0.8b on M1.local, think:false</code></pre>
    </details>

    <h3>User Scripts</h3>

    <details>
      <summary>px-spark</summary>
      <pre><code class="language-bash"># Launch SPARK voice loop (Claude backend)
bin/px-spark [--dry-run] [--input-mode voice|text]</code></pre>
    </details>

    <details>
      <summary>px-mind</summary>
      <pre><code class="language-bash"># Three-layer cognitive daemon (run as systemd service)
bin/px-mind [--awareness-interval 30] [--dry-run]</code></pre>
    </details>

    <details>
      <summary>px-alive</summary>
      <pre><code class="language-bash"># Idle-alive daemon — gaze drift, sonar proximity react
sudo bin/px-alive [--gaze-min 10] [--gaze-max 25] [--dry-run]
# Yields GPIO on SIGUSR1 for other tools</code></pre>
    </details>

    <details>
      <summary>px-diagnostics</summary>
      <pre><code class="language-bash"># Quick health check
bin/px-diagnostics --no-motion --short</code></pre>
    </details>

    <details>
      <summary>px-api-server</summary>
      <pre><code class="language-bash"># REST API + web UI on port 8420
bin/px-api-server [--dry-run]
# Auth: Bearer token from .env PX_API_TOKEN
# Web UI: http://pi:8420</code></pre>
    </details>
  </div>
</section>

<!-- ══════════════════════════════════════════════════════ ROADMAP -->
<section id="roadmap" data-theme="dark">
  <div class="container">
    <h2>// roadmap</h2>

    <p class="text-muted-dark mb-md">Milestones and future work.</p>

    <div class="roadmap-group">
      <h3>Foundation (0–1 Month)</h3>
      <div class="roadmap-item done"><span class="check">✅</span> Upgrade diagnostics to log predictive signals</div>
      <div class="roadmap-item done"><span class="check">✅</span> Extend energy sensing (voltage/temperature)</div>
      <div class="roadmap-item done"><span class="check">✅</span> Boot health service — captures throttle/voltage at boot</div>
      <div class="roadmap-item done"><span class="check">✅</span> Ship safety fallbacks: wake-word halt, watchdog heartbeats</div>
      <div class="roadmap-item done"><span class="check">✅</span> Harden logging paths (FileLock, isolated test fixtures)</div>
      <div class="roadmap-item done"><span class="check">✅</span> Source control: repo at adrianwedd/spark</div>
      <div class="roadmap-item done"><span class="check">✅</span> Three-layer cognitive loop (px-mind) with LLM fallback</div>
      <div class="roadmap-item done"><span class="check">✅</span> SPARK persona + neurodivergent-aware system prompt</div>
      <div class="roadmap-item done"><span class="check">✅</span> REST API + web UI (px-api-server)</div>
      <div class="roadmap-item done"><span class="check">✅</span> Frigate camera stream (go2rtc RTSP pull model)</div>
      <div class="roadmap-item"><span class="check">⬜</span> Gesture-driven stop prototype</div>
      <div class="roadmap-item"><span class="check">⬜</span> Weekly battery/health summary reports</div>
    </div>

    <div class="roadmap-group">
      <h3>Growth (1–3 Months)</h3>
      <div class="roadmap-item"><span class="check">⬜</span> Modular sensor fusion and persistent mapping</div>
      <div class="roadmap-item"><span class="check">⬜</span> Richer voice summaries, mission templates, gesture recognition</div>
      <div class="roadmap-item"><span class="check">⬜</span> Simulation CI sweeps (Gazebo or lightweight custom sim)</div>
      <div class="roadmap-item"><span class="check">⬜</span> Predictive maintenance alerts from historical logs</div>
    </div>

    <div class="roadmap-group">
      <h3>Visionary (3+ Months)</h3>
      <div class="roadmap-item"><span class="check">⬜</span> Reinforcement learning "dream buffer" and policy sharing</div>
      <div class="roadmap-item"><span class="check">⬜</span> Autonomous docking, payload auto-detection, multi-car demos</div>
      <div class="roadmap-item"><span class="check">⬜</span> Central knowledge base syncing maps and logs</div>
      <div class="roadmap-item"><span class="check">⬜</span> Quantised/accelerated model variants for on-device sustainability</div>
    </div>
  </div>
</section>

<script src="js/highlight.min.js"></script>
<script>hljs.highlightAll();</script>
<script src="js/live.js"></script>
<script src="js/nav.js"></script>
</body>
</html>
```

- [ ] **Step 7.2: Verify the file renders locally**

Open in a browser: `python3 -m http.server 8000 --directory site` → visit `http://localhost:8000`

Expected: page loads, nav visible, all sections render, no console errors about missing assets.

- [ ] **Step 7.3: Commit**

```bash
git add site/index.html
git commit -m "feat(site): add full index.html — all seven sections, nav, content"
```

---

## Chunk 5: Live Dashboard JS

### Task 8: live.js — polling, cache, offline banner, status dot

**Files:**
- Create: `site/js/live.js`

- [ ] **Step 8.1: Create live.js**

```javascript
// live.js — polls api.spark.wedd.au every 30s; falls back to localStorage
(function () {
  const API = 'https://api.spark.wedd.au/api/v1/public';
  const CACHE_KEY = 'spark_last_known';
  const POLL_MS = 30_000;
  const TIMEOUT_MS = 5_000;

  // DOM refs
  const moodBubble   = document.getElementById('mood-bubble');
  const lastThought  = document.getElementById('last-thought');
  const liveMood     = document.getElementById('live-mood');
  const liveThought  = document.getElementById('live-thought');
  const liveTs       = document.getElementById('live-ts');
  const cpuVal       = document.getElementById('cpu-val');
  const ramVal       = document.getElementById('ram-val');
  const battVal      = document.getElementById('batt-val');
  const sonarVal     = document.getElementById('sonar-val');
  const cardCpu      = document.getElementById('card-cpu');
  const cardRam      = document.getElementById('card-ram');
  const cardBatt     = document.getElementById('card-batt');
  const lastUpdated  = document.getElementById('last-updated');
  const offlineBanner= document.getElementById('offline-banner');
  const offlineTs    = document.getElementById('offline-ts');
  const statusDot    = document.getElementById('status-dot');

  let lastSuccessMs = null;

  // ── Threshold helpers (class-based — no inline styles) ──────────────────
  function setThreshold(card, val, warnAt, critAt) {
    card.classList.remove('ok', 'warn', 'crit');
    if (val === null) return;
    if (val >= critAt) card.classList.add('crit');
    else if (val >= warnAt) card.classList.add('warn');
    else card.classList.add('ok');
  }

  // ── UI update ────────────────────────────────────────────────────────────
  function applyStatus(data) {
    const mood = data.mood || '—';
    if (moodBubble) moodBubble.textContent = mood;
    if (lastThought) lastThought.textContent = data.last_thought || 'Nothing on my mind just now…';
    if (liveMood) liveMood.textContent = mood;
    if (liveThought) liveThought.textContent = data.last_thought || '—';
    if (liveTs && data.ts) liveTs.textContent = new Date(data.ts).toLocaleString('en-AU');
  }

  function applyVitals(data) {
    const fmt = v => v !== null && v !== undefined ? v + '%' : '—';
    const fmtTemp = v => v !== null && v !== undefined ? v + '°C' : '—';

    if (cpuVal) cpuVal.textContent = fmt(data.cpu_pct);
    if (ramVal) ramVal.textContent = fmt(data.ram_pct);
    if (battVal) battVal.textContent = fmt(data.battery_pct);

    setThreshold(cardCpu,  data.cpu_pct,       70, 90);
    setThreshold(cardRam,  data.ram_pct,       75, 90);
    setThreshold(cardBatt, data.battery_pct !== null ? 100 - data.battery_pct : null, 70, 85);
  }

  function applySonar(data) {
    if (!sonarVal) return;
    if (data.source === 'unavailable' || data.sonar_cm === null) {
      sonarVal.textContent = '—';
    } else {
      sonarVal.textContent = data.sonar_cm.toFixed(0) + ' cm';
    }
  }

  // ── Fetch with timeout ───────────────────────────────────────────────────
  async function fetchWithTimeout(url) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
    try {
      const resp = await fetch(url, { signal: ctrl.signal });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      return resp.json();
    } finally {
      clearTimeout(timer);
    }
  }

  // ── Dot state ─────────────────────────────────────────────────────────────
  function updateDot() {
    if (!statusDot) return;
    statusDot.classList.remove('green', 'amber', 'red');
    if (lastSuccessMs === null) {
      statusDot.classList.add('red');
      return;
    }
    const age = Date.now() - lastSuccessMs;
    if (age < 60_000) statusDot.classList.add('green');
    else if (age < 300_000) statusDot.classList.add('amber');
    else statusDot.classList.add('red');
  }

  // ── Poll cycle ────────────────────────────────────────────────────────────
  async function poll() {
    try {
      const [status, vitals, sonar] = await Promise.all([
        fetchWithTimeout(API + '/status'),
        fetchWithTimeout(API + '/vitals'),
        fetchWithTimeout(API + '/sonar'),
      ]);

      applyStatus(status);
      applyVitals(vitals);
      applySonar(sonar);

      lastSuccessMs = Date.now();

      // Cache
      localStorage.setItem(CACHE_KEY, JSON.stringify({ status, vitals, sonar, fetchedAt: new Date().toISOString() }));

      // Hide offline banner
      if (offlineBanner) offlineBanner.classList.add('hidden');
      if (lastUpdated) lastUpdated.textContent = 'Updated just now';

    } catch (_err) {
      // Load from cache
      const raw = localStorage.getItem(CACHE_KEY);
      if (raw) {
        const cached = JSON.parse(raw);
        applyStatus(cached.status || {});
        applyVitals(cached.vitals || {});
        applySonar(cached.sonar || {});

        if (offlineBanner) offlineBanner.classList.remove('hidden');
        if (offlineTs) {
          offlineTs.textContent = new Date(cached.fetchedAt).toLocaleString('en-AU');
        }
        if (lastUpdated) lastUpdated.textContent = 'Using cached data';
      } else {
        if (lastUpdated) lastUpdated.textContent = 'Pi unreachable — no cached data';
      }
    }

    updateDot();
  }

  // ── Start ─────────────────────────────────────────────────────────────────
  poll();
  setInterval(poll, POLL_MS);
  setInterval(updateDot, 10_000); // keep dot fresh between polls

})();
```

- [ ] **Step 8.2: Test locally with the Pi API running**

Temporarily change `API` constant to `http://localhost:8420/api/v1/public` and open via local server. Verify:
- Cards populate with real values
- Mood bubble updates
- Console shows no errors

Revert `API` to `https://api.spark.wedd.au/api/v1/public` before committing.

- [ ] **Step 8.3: Test offline fallback**

Disconnect from network (or use browser DevTools → Network → Offline), reload. Verify:
- Offline banner appears
- Cached data is shown
- Status dot is red

- [ ] **Step 8.4: Commit**

```bash
git add site/js/live.js
git commit -m "feat(site): add live.js — API polling, localStorage cache, offline banner, status dot"
```

---

## Chunk 6: GH Pages + DNS Setup

### Task 9: Configure GH Pages and DNS

This task is manual — no code to write. Follow each step exactly.

- [ ] **Step 9.1: Push all site files**

```bash
git push origin master
```

- [ ] **Step 9.2: Enable GH Pages in repo settings**

1. Go to `https://github.com/adrianwedd/spark/settings/pages`
2. Source: **Deploy from a branch**
3. Branch: `master`, folder: `/site`
4. Click **Save**

- [ ] **Step 9.3: Add custom domain**

In the same Pages settings, under "Custom domain", enter: `spark.wedd.au`
Click **Save** — GH will check for the CNAME file (already committed).

- [ ] **Step 9.4: Add DNS A records in Cloudflare dashboard**

For domain `wedd.au`, add four A records (proxied = OFF — GH Pages manages TLS):

| Type | Name | Value | Proxy |
|---|---|---|---|
| A | spark | 185.199.108.153 | DNS only |
| A | spark | 185.199.109.153 | DNS only |
| A | spark | 185.199.110.153 | DNS only |
| A | spark | 185.199.111.153 | DNS only |

- [ ] **Step 9.5: Wait for DNS propagation (up to 10 min)**

```bash
dig spark.wedd.au A +short
```

Expected: one or more `185.199.x.153` addresses.

- [ ] **Step 9.6: Enable HTTPS enforcement**

Back in GH Pages settings, once the domain verifies (green tick), check **Enforce HTTPS**.

- [ ] **Step 9.7: Verify the live site**

```bash
curl -I https://spark.wedd.au
```

Expected: `HTTP/2 200` with `content-type: text/html`.

```bash
curl https://api.spark.wedd.au/api/v1/public/status
```

Expected: JSON with persona, mood, etc.

- [ ] **Step 9.8: Final smoke test**

Open `https://spark.wedd.au` in a browser:
- Status dot turns green within 30s
- Mood bubble populates
- Live stat cards show real values
- Offline banner is hidden

---

## Done

All tasks complete when:

1. `python -m pytest tests/test_public_api.py -v` → all green
2. `python -m pytest -m "not live" -v` → no regressions
3. `https://spark.wedd.au` loads over HTTPS, live data updates, offline fallback works
4. `https://api.spark.wedd.au/api/v1/public/status` returns 200 with persona-scoped thought data
5. `sudo systemctl status cloudflared` → active (running)
