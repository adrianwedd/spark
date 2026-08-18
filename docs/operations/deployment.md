# Deployment and Services

**Owns:** what runs on the Pi, as whom, and how it is installed.
`systemd/`, `bin/px-env`, the Cloudflare tunnel.

---

## Invariant

### Every `bin/` script sources `bin/px-env`

It sets `PROJECT_ROOT`, `LOG_DIR`, adds `$PROJECT_ROOT/src` and
`/home/pi/picar-x` to `PYTHONPATH`, defines `yield_alive`, and sets the default
audio device. A script that does not source it will fail in ways that look like
import errors.

### `bin/` scripts run under `/usr/bin/python3`, not the venv

`picarx` and `robot_hat` live in **system** site-packages. The one exception is
`bin/px-wake-listen`, which needs `.venv`.

Development and tests use the venv: `source .venv/bin/activate`.

### `.env` is not loaded by `px-env`

Secrets are loaded by `bin/px-mind` and some other daemons, **not** by
`bin/px-env`. A tool run standalone may therefore lack `OLLAMA_CLOUD_API_KEY`
and similar, and will fail in a way that looks like a network problem.

### The services

| Service | Script | User | Restart |
|---|---|---|---|
| `px-alive` | `bin/px-alive` | root | always, 10s (`StartLimitIntervalSec=0`) |
| `px-wake-listen` | `bin/px-wake-listen` | pi | always, 10s |
| `px-battery-poll` | `bin/px-battery-poll` | root | always, 10s |
| `px-mind` | `bin/px-mind` | pi | always, 10s |
| `px-brain` | `bin/px-brain` | pi | always, 10s (`KillMode=process`) |
| `px-post` | `bin/px-post` | pi | always, 30s |
| `px-api-server` | `bin/px-api-server` | pi | always, 2s |
| `px-frigate-stream` | `bin/px-frigate-stream` | pi | always, 10s |
| `px-evolve` | `bin/px-evolve` | pi | on-failure, 30s |
| `px-blog` | `bin/px-blog` | pi | on-failure, 30s |
| `px-tts-glados` | GLaDOS TTS :7861 | pi | always, 10s |
| `cloudflared` | tunnel → spark-api.wedd.au | pi | always, 10s |

**The root/pi split is why several state directories are `1777`** — see
[operations/state-and-runtime](state-and-runtime.md).

Unit files and the pip-cleanup timer are documented in
[systemd/README.md](../../systemd/README.md). Install to
`/etc/systemd/system/`, then `daemon-reload` and `enable --now`.

### `px-brain` uses `KillMode=process` on purpose

Restarting the supervisor must not kill the tmux sessions it supervises. A
consequence: restarting the service does **not** reload a changed system
prompt, because prompts bake in at session launch. Kill the session.

### The API is single-worker

`src/pxh/api.py` on port 8420. **Not multi-worker safe** — it holds in-process
state (rate-limit stores, job table). Do not add workers.

- Public rate limit: 120 req/min per IP; `/api/v1/public/chat` is 10 msg/10min
- `X-Forwarded-For` is trusted from `127.0.0.1`/`::1` **only** — never from
  Cloudflare
- PIN verify returns a session token (4h TTL); the raw Bearer token is never
  exposed to a browser
- Per-IP PIN lockout (`state/pin_lockout.json`): 3 failures → 5 min, 10 → 30
  min, capped at 1000 IPs / 10k rate-limit entries with oldest-first eviction
- Device reboot/shutdown is two-step: `POST /api/v1/device/{action}` returns a
  nonce, confirmed within 60s

### The site auto-deploys from `master`

Cloudflare Pages serves `site/`. **A merge to `master` publishes the public
site**, so treat `site/` changes as outward-facing.

- `site/css/colors.css` — single-source 12-mood palette. All JS reads
  `getComputedStyle().getPropertyValue('--mood-' + mood)`; **never hardcode
  hex**.
- `site/js/config.js` — the single API base URL. **Never hardcode URLs in JS.**
- `site/workers/og-rewrite.js` — rewrites OG meta server-side, because social
  crawlers do not execute JS.

### Address the announce relay by IP

`ANNOUNCE_RELAY_URL` in `spark_config.py` points at `192.168.0.249:7862`.
**Never `M5.local`** — Nest speakers fetch the audio URL themselves and cannot
resolve mDNS. The relay's own `RELAY_PUBLIC_BASE_URL` must match, or every
audio URL it hands out points at the wrong address.

Health check from the Pi: `curl http://192.168.0.249:7862/health`

### Other surfaces

Thin surfaces that publish or receive, each with one thing worth knowing:

| Surface | Entry point | The gotcha |
|---|---|---|
| Social posting | `bin/px-post` | watches `thoughts-spark.jsonl` (salience ≥0.7 or a spoken action) behind a Claude QA gate. **Ambiguous QA responses default to pass** — the gate is a safety net, not a quality bar. |
| Blog | `bin/px-blog`, `bin/tool-blog` | writes a `state/blog.json` envelope served at `GET /api/v1/public/blog`; OG meta is rewritten by the same Cloudflare Worker pattern as `/thought/*`. |
| MCP server | `bin/mcp-server` | 5 **read-only** tools over stdio (`spark_status`, `spark_thoughts`, `spark_awareness`, `spark_sonar`, `spark_vitals`), registered in `.mcp.json`. |
| Home Assistant | `ha/custom_components/spark_conversation/` | routes Nest/Hub Max voice through `POST /api/v1/public/chat`. HA 2026.x wants `supported_languages` as a `@property` and `AddConfigEntryEntitiesCallback`. |
| Obi chat | `POST /api/v1/obi-chat` | authenticated, 10s rate gate, both sides logged; text sanitised — see [privacy](../architecture/privacy.md). |
| Location push | cron on M5.local | pushes `state/findmyhub.json` to the Pi every 5 min. Its contents are **excluded from reflection** — see [privacy](../architecture/privacy.md). |

Per-script detail: [docs/SCRIPTS.md](../SCRIPTS.md).

---

## Why it looks like this

*History, not rule.*

`px-brain.service` was believed for about eleven hours to have an
authentication problem. It had never been installed. Check `systemctl status`
before debugging the thing the failure appears to be about.

The relay moved from a wired `.100` to `.249` when M5's wired adapter was
unplugged, and the mDNS prohibition was learned from Nest speakers silently
failing to fetch audio they had been handed a `.local` URL for.
