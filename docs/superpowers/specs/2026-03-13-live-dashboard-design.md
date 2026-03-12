# Live Dashboard — Design Spec
**Date:** 2026-03-13
**Project:** spark (picar-x-hacking)
**Status:** Draft

---

## Overview

Expand the public dashboard at `spark.wedd.au` from a 4-card stat grid into a rich, layered live display that communicates two things simultaneously: "this robot is alive right now" (ambient/emotional layer) and "look how much is going on under the hood" (technical layer). The technical layer is hidden by default behind a toggle, preserving the warm first impression for non-technical visitors.

---

## Goals

- Surface SPARK's inner state (mood, presence, thought) as the dominant visual signal
- Add environmental context (ambient sound, weather, time) as a secondary layer
- Expose system metrics with sparkline history as an opt-in technical layer
- Build 30-minute rolling history via backend ring buffer + localStorage accumulation
- Stay within existing constraints: vanilla JS, no CDN, strict CSP, dual warm/dark theme

---

## Architecture

### New Backend Additions

**`GET /api/v1/public/awareness`** — unauthenticated, CORS-enabled. Reads `state/awareness.json` and returns a flattened, normalised subset safe for public exposure. Field projection is explicit — not a raw passthrough:

```json
{
  "obi_mode": "calm",
  "person_present": true,
  "frigate_score": 0.74,
  "ambient_level": "quiet",
  "ambient_rms": 340,
  "weather": { "temp_c": 14.2, "wind_kmh": 12, "humidity_pct": 68, "summary": "Cloudy" },
  "minutes_since_speech": 4,
  "time_period": "night",
  "ts": "2026-03-13T..."
}
```

Field mapping from `awareness.json` (all projections made explicit):

| Response field | Source path in awareness.json | Notes |
|----------------|-------------------------------|-------|
| `obi_mode` | `awareness["obi_mode"]` | |
| `person_present` | `awareness["frigate"]["person_present"]` | `false` if frigate key absent |
| `frigate_score` | `awareness["frigate"]["score"]` | `null` if absent |
| `ambient_level` | `awareness["ambient_sound"]["level"]` | `null` if absent |
| `ambient_rms` | `awareness["ambient_sound"]["rms"]` | `null` if absent |
| `weather.temp_c` | `awareness["weather"]["temp_C"]` | Normalised to lowercase `temp_c` on the way out |
| `weather.wind_kmh` | `awareness["weather"]["wind_kmh"]` | |
| `weather.humidity_pct` | `awareness["weather"]["humidity_pct"]` | |
| `weather.summary` | `awareness["weather"]["summary"]` | |
| `minutes_since_speech` | `awareness["minutes_since_speech"]` | |
| `time_period` | `awareness["time_period"]` | |
| `ts` | `awareness["ts"]` | |

Any field that cannot be read (key missing, file unreadable) returns `null` for that field — not an error.

---

**`GET /api/v1/public/history`** — unauthenticated, CORS-enabled. Returns a JSON array of up to 60 readings from an in-memory ring buffer (`collections.deque`, maxlen=60). A background thread appends one reading every 30s. At 30s intervals, 60 readings = 30 minutes of history.

Sonar is read directly from `state/sonar_live.json` with the same age gate used by `/public/sonar` (age > 60s → `sonar_cm: null`).

```json
[
  { "ts": "...", "cpu_pct": 23.4, "cpu_temp_c": 52.1, "ram_pct": 41.2,
    "battery_pct": 87, "sonar_cm": 45.2, "ambient_rms": 340 },
  ...
]
```

Ring buffer is lost on restart (acceptable — localStorage fills the gap immediately). No persistence needed.

---

**`GET /api/v1/public/services`** — new unauthenticated endpoint (do **not** modify the existing `GET /api/v1/services` which is auth-required, returns `{"services": [list of dicts]}`, and is consumed by the embedded web UI at `api.py:903`). The new public endpoint returns a simplified normalised dict:

```json
{
  "px-mind": "active",
  "px-alive": "active",
  "px-wake-listen": "active",
  "px-battery-poll": "active",
  "px-api-server": "active"
}
```

Status values: `"active"` / `"activating"` / `"failed"` / `"inactive"` / `"unknown"`.

> **Implementation note:** The existing `_MANAGED_SERVICES` set contains four services — `px-battery-poll` is missing. The new public handler should query all five explicitly (no need to modify `_MANAGED_SERVICES` unless the authenticated endpoint also needs to track it).

```json
{
  "px-mind": "active",
  "px-alive": "active",
  "px-wake-listen": "active",
  "px-battery-poll": "active",
  "px-api-server": "active"
}
```

Status values: `"active"` / `"activating"` / `"failed"` / `"inactive"` / `"unknown"`. The existing authenticated `/api/v1/services` endpoint is unchanged.

---

### Frontend Structure

Three new/rewritten files (all under `site/js/` to match existing file layout):

| File | Responsibility |
|------|---------------|
| `site/js/live.js` | Polling orchestrator — parallel fetches, state merge, localStorage accumulation, drives renders |
| `site/js/charts.js` | All canvas drawing — sparklines, sonar arc, waveform bars, gauge arc |
| `site/js/dashboard.js` | DOM update functions — binds data to elements, manages toggle state |

**localStorage keys** (both must be named to avoid collision with existing `spark_last_known`):

| Key | Contents |
|-----|----------|
| `spark_last_known` | Existing snapshot cache — unchanged |
| `spark_history` | JSON array of rolling vitals readings (max 120 entries, same shape as `/public/history`) |
| `spark_machine_open` | `"true"` / `"false"` — persists MACHINE toggle state |

**Polling:** `live.js` fetches five endpoints in parallel every 30s with a 5s timeout each:
- `/api/v1/public/status`
- `/api/v1/public/vitals`
- `/api/v1/public/sonar` — real-time `sonar_cm` for the PRESENCE proximity arc (existing endpoint, unchanged)
- `/api/v1/public/awareness` — does **not** include `sonar_cm`; that comes from `/public/sonar` above
- `/api/v1/public/services` — new endpoint (see above)

`/api/v1/public/history` is fetched separately: once on page load and again when a sparkline is opened. It is not part of the 30s poll cycle.

Results merge into a single `state` object. Each endpoint failure degrades independently.

---

## Section 1: PRESENCE Band

Always visible. The dominant "alive" signal.

**Three-column layout** (stacks vertically on mobile):

### Mood Pulse (left)
- Large filled circle (~120px diameter) in current theme accent colour
- Slow CSS `scale` pulse animation via class swap (never inline `style.animationDuration`):

| CSS class | Cycle | Applied when |
|-----------|-------|-------------|
| `pulse-slow` | 4s | mood: peaceful, content |
| `pulse-mid` | 2.5s | mood: curious, contemplative |
| `pulse-fast` | 1.5s | mood: excited, active |

- Mood word in large type inside the circle
- Below: `obi_mode` as a human-readable line:
  - `absent` → "Obi's probably asleep"
  - `calm` → "Obi seems nearby"
  - `active` → "Obi is around"
  - `possibly-overloaded` → "Things seem busy"
  - `unknown` → line omitted entirely

### Last Thought (centre)
- Existing pull-quote, larger, more vertical breathing room
- Below the quote: mood word + salience as filled dots (●●●○○, 5-dot scale from `salience` 0–1) + "X min ago"
- Salience dots encode importance visually without needing to explain the concept

### Proximity (right)
- 180° SVG fan arc, top-down view. Arc fill is **inversely proportional to distance** — close object = large filled arc, distant object = thin sliver:
  - `sonar_cm` < 40 → full fan (180°), warm accent colour
  - 40–100cm → partial fill (~90°), neutral colour
  - > 150cm → thin sliver (~20°), cool/muted colour
  - Unavailable → empty arc outline only
- Below arc: Frigate indicator — person-icon character (filled = detected, hollow = not) + confidence % when detected. Hidden entirely if `person_present` is `null` (Frigate offline).

---

## Section 2: WORLD Band

Always visible. Sits below PRESENCE on a subtly differentiated background (CSS class `band-world`, not inline style).

### Ambient Sound (left ~40%)
- Row of ~40 thin vertical canvas bars spanning the column width
- Animated every 2s independently of API poll via `setInterval`
- Bar heights generated organically from current `ambient_rms` scalar (seeded deterministic random, not real audio samples) — high RMS → tall jagged bars, near-zero → nearly flat gentle drift
- `ambient_level` label to the left as plain text: "silent / quiet / moderate / loud"
- See Waveform Honesty Note below

### Weather Strip (centre ~35%)
- Single horizontal line: temperature + Unicode weather symbol (☀ ☁ 🌧 ❄ — character literals in HTML/JS, not images or background-image) + wind + humidity + one-word summary
- Source: `weather` object from `/public/awareness`; `temp_c` field (lowercase, already normalised by the endpoint)
- Hidden entirely if `weather` is `null` (not a broken-state placeholder)

### Time Context (right ~25%)
- Current AEDT time (computed client-side)
- `time_period` as a soft badge (CSS class swap, not inline style): "morning" / "afternoon" / "evening" / "night"
- "Last spoke X min ago" from `minutes_since_speech`. Shows "hasn't spoken recently" if > 30 min.

---

## Section 3: MACHINE Band

Hidden by default. Revealed by "show internals ↓" toggle. CSS `max-height` transition on expand/collapse. Toggle state persisted in `localStorage` as `spark_machine_open`.

**3×2 grid of metric tiles:**

| Tile | Visual | Sparkline |
|------|--------|-----------|
| CPU % | Horizontal bar + value | Click → 30-point sparkline (30 min) |
| CPU temp | Radial gauge arc (SVG, 0–85°C); CSS classes `gauge-ok` / `gauge-warn` (≥65°) / `gauge-crit` (≥75°) | None needed |
| RAM % | Horizontal bar + value | Click → 30-point sparkline (30 min) |
| Disk % | Horizontal bar + value; CSS class `warn` at ≥80%, `crit` at ≥90% | None (changes too slowly) |
| Battery | Horizontal bar + voltage in small text; `⚡` character literal when charging | Click → 30-point sparkline (30 min) |
| Services | Row of named status dots for each service; CSS classes `dot-ok` / `dot-warn` / `dot-err` | None |

**Inline sparkline behaviour:**
- Clicking an eligible tile fetches `/public/history` and merges with `spark_history` from localStorage (deduplicated by `ts`, sorted ascending). Draws at most 60 points = 30 minutes.
- Appends a `<canvas>` directly below the tile with a "last 30 min" label + min/max range
- Clicking again collapses. Only one sparkline open at a time (opening a second collapses the first).

Toggle label flips to "hide internals ↑" when expanded.

---

## Data Flow

```
On page load:
  read localStorage spark_last_known → hydrate state immediately (zero-flash)
  read localStorage spark_history → available for sparklines immediately
  fire first poll

Every 30s:
  parallel fetch (5s timeout each):
    /public/status    → mood, last_thought, persona, listening
    /public/vitals    → cpu_pct, cpu_temp_c, ram_pct, disk_pct, battery_pct
    /public/sonar     → sonar_cm (for PRESENCE proximity arc)
    /public/awareness → obi_mode, person_present, frigate_score, ambient_*, weather, time_period
    /public/services  → service status dict (new endpoint)
  merge → state object
  append {ts, cpu_pct, cpu_temp_c, ram_pct, battery_pct, sonar_cm, ambient_rms} to spark_history
    (keep last 120 entries; 120 × 30s = 60 min local buffer)
  render all three bands

Every 2s (independent setInterval):
  regenerate waveform bars from last known ambient_rms

On sparkline open:
  fetch /public/history
  merge with spark_history from localStorage (dedup by ts, sort asc)
  draw 60-point canvas sparkline

Backend (separate thread, every 30s):
  read psutil + state/sonar_live.json (age gate: null if > 60s) + state/battery.json
  append to deque(maxlen=60)
```

---

## Error Handling & Offline

- Each endpoint degrades independently — awareness offline doesn't blank vitals
- PRESENCE offline: hollow (unfilled, outline-only) pulse circle, greyed thought text, no obi_mode line
- WORLD offline: entire band collapses (CSS class `band-hidden`)
- MACHINE: shows last known values from localStorage with "X min ago" timestamp
- Existing "last updated" banner extended per-section: each band tracks its own last-successful-ts
- History endpoint failure → sparkline uses localStorage-only data; if localStorage also empty, tile shows "no history yet" message

---

## Waveform Honesty Note

The ambient sound waveform is **not** a real audio waveform — the API provides only an RMS scalar, not audio samples. The animation generates a plausible organic bar shape seeded from the RMS value each 2s cycle. This is aesthetic, not misleading: the `ambient_level` text label is the authoritative reading. The visual conveys "loud/quiet" without pretending to show actual microphone data.

---

## Files Changed

| File | Change |
|------|--------|
| `src/pxh/api.py` | Add `/public/awareness` (explicit field projection + key normalisation), `/public/history` (ring buffer + background thread), `/public/services` (new public endpoint, normalised dict — existing auth'd `/services` unchanged) |
| `site/js/live.js` | Rewrite — parallel polling of 5 endpoints, localStorage accumulation under `spark_history` key |
| `site/js/charts.js` | New — canvas: sparklines, waveform bars, gauge arc; SVG: proximity arc |
| `site/js/dashboard.js` | New — DOM updates, CSS class swaps (never inline styles), toggle state management |
| `site/index.html` | Replace `#status` section with three-band layout; update `<script>` tags to reference new files |

---

## Testing

**Backend (pytest):**
- `/public/awareness` returns correct flattened projection; `temp_c` (lowercase) present; `person_present` is `false` (not absent) when frigate key missing from awareness.json; any missing nested key returns `null` for that field, not a 500
- `/public/history` returns array; maxlen=60 enforced after 61 appends; `sonar_cm` is `null` when sonar_live.json is stale (> 60s)
- `/public/services` accessible without auth token; returns dict (not list); values are one of the five defined status strings; existing auth'd `/services` still returns `{"services": [list]}` unchanged

**Frontend (manual):**
- Each band renders correctly with live data in both warm and dark themes
- Offline degradation: kill px-api-server, verify each band degrades independently
- Sparkline: open/close on eligible tiles; only one open at a time; "no history yet" when localStorage empty
- localStorage: `spark_history` accumulates; `spark_machine_open` persists toggle across reload
- CSP: DevTools console shows zero CSP violations; no inline `style=` attributes set by JS

**Waveform:**
- Silent RMS (< 200) produces nearly flat bars
- Loud RMS (> 1500) produces tall varied bars
- Animation runs at 2s independently of API failures
