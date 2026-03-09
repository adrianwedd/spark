# SPARK UI Redesign — Design Document
_2026-03-09_

## Context

The existing web UI at port 8420 is a dark cyberpunk single-page dashboard with a 220px sidebar of 25+ small buttons and a chat panel. It was built as a dev tool and reads that way. The audience is:

- **Obi** (7, ADHD/ASD) — uses it on a tablet he holds; needs big touch targets, friendly language, SPARK's personality
- **Adrian** (parent/dev) — needs raw tool access, logs, service management, device controls, parental overrides

The redesign keeps the same FastAPI backend and single-page architecture. All wiring (`/api/v1/tool`, `/api/v1/chat`, `/api/v1/session`, `/api/v1/services`) stays unchanged. Only the HTML/CSS/JS frontend is replaced.

---

## Visual Language

- **Background:** `#12111a` (deep space purple-black) — calm, not clinical
- **SPARK accent:** `#00d4aa` (electric teal) — everything SPARK-touched
- **Typography:** Nunito (Google Fonts) — rounded, friendly, not babyish
- **Button shape:** Large pill/rounded-rect, min 64px tall, generous padding
- **Mood-responsive theming:** Quiet mode → muted blues; excited → warmer tones pulse subtly
- **Out:** Hot pink cyberpunk, monospace, dense text, tiny emoji decorations

---

## Layout: Bottom Tab Bar

Fixed bottom navigation, always visible:

```
[  💬 Chat  |  ⚡ Actions  |  🤖 SPARK  |  🔧 ··· ]
```

- Active tab glows teal; inactive tabs are dim
- 🔧 shows a subtle lock icon — visible to Obi, PIN-gated
- Tab bar is the outermost chrome; each tab is a full viewport

---

## Tab 1 — 💬 Chat

- SPARK's avatar (large animated teal circle + mood emoji) pinned to top
- Scrollable message feed below with large text
- Message bubbles: Obi = right-aligned solid teal; SPARK = left-aligned dark surface with tool tag
- Input bar pinned to bottom: large rounded field + mic icon + send button
- SPARK's name and current mood shown above each response
- Disabled during processing; shows "SPARK is thinking…" indicator

---

## Tab 2 — ⚡ Actions

Scrollable vertical list of grouped sections. Each section has a large coloured header tile.

### 🧘 "I need help" *(teal)*
Breathe with me · Go quiet · End quiet · Body check · What can I do? · Make things better

### 📋 "Our routines" *(warm orange)*
Morning · Homework · Bedtime · Next step · What's the plan? · 5 min warning · 2 min warning · I'm here now

### 💛 "How are we doing?" *(yellow)*
Check in · Celebrate! · What's today? · Next thing · What time is it? · What's the weather?

### 🤖 "Move SPARK!" *(purple)*

**RC Controls (top of section):**
```
           ▲ Forward
    ◄ Left        Right ►
           ▼ Backward

    Speed: ━━━━━●━━  40

    [⭕ Circle]  [∞ Figure-8]  [🎲 Wander]
              [ ⛔ STOP ]
```
- D-pad fires `tool_drive` with steer/direction on press; hold repeats every 500ms; release sends `tool_stop`
- Speed slider: range 10–50, persists during session
- STOP: large red pill, always visible
- One-shot buttons below: Circle, Figure-8, Wander, Look left/right/up, Happy face, Do a trick, Take a photo, What do you see?

### 🔊 "Sounds & memory" *(blue)*
Play a sound · Set a timer · Remember this · What do you remember?

**Tool coverage:** All 31 Obi-appropriate tools exposed.
Excluded to Adrian panel: `tool_api_start/stop`, `tool_chat` (Gremlin), `tool_chat_vixen`, `tool_qa`

---

## Tab 3 — 🤖 SPARK (The Face)

Full-screen read-only display of SPARK's inner state.

```
        ●  thinking...

       ( 🤔 )
      ~~~~~~~~

  "I'm wondering what that
   sound was outside."

  ─────────────────────────
  mood: curious   sonar: 80cm
  persona: spark  evening
```

- Giant mood emoji (80px) in a soft glowing circle; colour matches mood
- Status ring: gentle pulse = thinking; solid = idle; fast = listening
- Thought bubble: latest entry from `thoughts-spark.jsonl`, live-updated
- Stat row: mood word, sonar distance, time of day — no technical jargon
- Polls every 5 seconds

---

## Tab 4 — 🔧 Adrian (PIN-gated)

PIN entry on first tap each session. Four sub-sections via inner tab bar:

### ⚙️ Services
- start / stop / restart for all four services
- Reboot device button (confirm dialog)
- Shutdown device button (confirm dialog)

### 🛠 Tools
- Dropdown of all 35 tools
- Dynamic param fields per tool selection
- Run button → JSON output panel

### 📋 Logs
- Live-tail viewer (last 100 lines, auto-scroll toggle)
- Toggle between: px-mind, px-wake-listen, px-alive, tool-voice, tool-describe_scene

### 👨‍👩‍👧 Parental
- Motion allowed toggle
- Quiet mode toggle
- Persona selector (spark / gremlin / vixen / none)
- DRY mode toggle
- `tool_gws_sheets_log` quick-entry form (mood, event, notes)

---

## Backend Changes Required

- `GET /api/v1/logs/{service}` — tail last N lines of a named log file
- `POST /api/v1/device/{action}` — reboot / shutdown (sudoers entry required)
- `POST /api/v1/pin/verify` — PIN check (PIN stored in `.env` as `PX_ADMIN_PIN`)
- RC hold-repeat: handled entirely client-side (JS `setInterval` on pointerdown, `tool_stop` on pointerup)

---

## Files Changed

| File | Change |
|------|--------|
| `src/pxh/api.py` | Replace HTML template; add `/logs/{service}`, `/device/{action}`, `/pin/verify` endpoints |
| `/etc/sudoers.d/picar-x-services` | Add `systemctl reboot` and `shutdown -h now` to NOPASSWD list |

No new files. No changes to voice loop, tools, or state management.

---

## Out of Scope

- Native mobile app (PWA stretch goal, not now)
- WebSocket live updates (polling at 5s is sufficient)
- Multi-user auth
- Conversation history persistence across page reload
