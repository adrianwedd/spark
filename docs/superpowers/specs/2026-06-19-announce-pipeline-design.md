# Announce Pipeline — Data-voice announcements over Google Nest

**Date:** 2026-06-19
**Status:** Design (awaiting approval)
**Sub-project:** A of 2 (B = `message_adrian` channel, separate spec later)

## Goal

Let SPARK speak through the household Google Nest devices in a chosen voice
(the cloned **`data`** voice from the local Afterwords TTS server on M5), as a
**deliberate "announce" capability** — not ambient mirroring of all robot
chatter.

The original idea also asked for SPARK to *listen* via Nest. That half is **out
of scope**: Google shut down custom Conversational Actions in 2023, and a bare
Nest speaker cannot be an HA Assist "listen" endpoint (HA can't intercept the
Nest microphone). Only HA Voice / ESP satellites or the HA mobile app can. A
listen path is deferred to a separate spike.

## Constraints discovered during exploration

These shaped the architecture and must be respected:

1. **Afterwords TTS is localhost-only.** The server runs at `127.0.0.1:7860`
   on M5 (`192.168.1.171`), launchd-managed, 203 cloned voices including
   `data`. The Pi resolves `M5.local` but **cannot reach port 7860**.
2. **Synth latency: ~2–8s warm, ~33s cold** (model load). Too slow for a Nest
   to stream the synth URL live — Chromecast would time out, especially cold.
   Audio must be **pre-synthesized to a complete file, then cast**.
3. **No server-side caching / non-deterministic output** — identical text
   yields different bytes each call. We add caching at the relay.
4. **`data` voice synthesizes** via `GET /synthesize?text=...&voice=data`
   returning `audio/wav` (24 kHz mono PCM).
5. **Chromecast cannot send auth headers** when fetching media. The audio URL
   it loads must be unauthenticated.
6. **Nest devices already enumerated in HA**: `media_player.nest_hub_max`,
   `media_player.nest_mini`, `media_player.googlehome1094` (Office Mini), each
   with a `_2` duplicate. HA exposes `media_player.play_media`,
   `media_player.volume_set`, and `tts.speak`.

## Architecture

```
SPARK trigger                       M5 (192.168.1.171)
(voice loop / px-mind /     ┌──────────────────────────────────┐
 message_obi)               │  announce-relay  :7861 (LAN)      │
      │                     │     POST /announce {text,voice}   │
      ▼                     │        │                          │
  bin/tool-announce ───POST─┼────────┘                          │
   (Pi, picar.local)        │        ▼                          │
      │  audio_url           │  afterwords :7860 (localhost)     │
      │                     │     GET /synthesize → WAV         │
      │                     │        ▼                          │
      │                     │  cache: announce/<hash>.wav       │
      │                     │     GET /audio/<hash>.wav (open)  │
      │                     └──────────────────────────────────┘
      ▼
  HA REST: media_player.play_media(audio_url → Nest targets)
      │
      ▼
  Nest Hub Max / Nest Mini  play the data-voice WAV
```

### Component 1 — M5 announce relay (`m5/announce-relay/`)

A small FastAPI service deployed to M5, launchd-managed alongside afterwords.
Lives in the spark repo (versioned with the feature); could later move to the
afterwords repo. Bound to `0.0.0.0:7861` (LAN). It keeps afterwords itself
**unexposed** — only this thin relay is on the LAN.

**`POST /announce`**
- Auth: `Authorization: Bearer <ANNOUNCE_RELAY_TOKEN>` (shared secret).
- Body: `{"text": str, "voice": str = "data", "lang": str = "en", "cache": bool = true}`
- Behavior:
  - `hash = sha256(f"{voice}|{lang}|{text}").hexdigest()[:16]`
  - if `cache` and `announce/<hash>.wav` exists and fresh → return it (`cached: true`)
  - else `GET http://127.0.0.1:7860/synthesize?text=<enc>&voice=<voice>&lang=<lang>`,
    write atomically to `announce/<hash>.wav`
  - `cache: false` (private channels) → synth to a random-named file, short TTL
- Response 200: `{"audio_url": "http://M5.local:7861/audio/<file>.wav", "voice", "cached", "duration_s"}`
- Errors: `400` empty/oversized text, `401` bad token, `502` afterwords error,
  `504` synth timeout.

**`GET /audio/<file>.wav`** — static WAV, **no auth** (Chromecast constraint),
`Content-Type: audio/wav`, `404` if missing. Security rests on the 16-hex
(64-bit) unguessable filename on a home LAN.

**`GET /health`** — `{"status":"ok","afterwords": bool, "cached_files": int}`.

**Cache hygiene:** delete `announce/*.wav` older than `RELAY_CACHE_TTL_DAYS`
(default 7) on each `/announce` call. Private (`cache:false`) files use
`RELAY_PRIVATE_TTL_MIN` (default 30 min).

**Keep-warm (configurable, default on):** background task synthesizes a 1-word
phrase every `RELAY_KEEP_WARM_SEC` (default 240) to keep the model loaded and
avoid the ~33s cold hit. Disable with `RELAY_KEEP_WARM=0` (trades latency for
idle GPU/power on M5).

**Deployment:** `m5/announce-relay/com.spark.announce-relay.plist` +
`m5/announce-relay/install.sh`. Reads config from env / a `.env` next to it.

### Component 2 — Pi tool (`bin/tool-announce`)

Standard `tool-*` pattern (bash + embedded Python heredoc, `/usr/bin/python3`),
emits a single JSON object.

- Env in: `PX_ANNOUNCE_TEXT` (required), `PX_ANNOUNCE_TARGETS` (optional, JSON
  or comma list), `PX_ANNOUNCE_VOICE` (optional).
- Flow:
  1. Resolve voice/targets: env → `spark_config` defaults.
  2. `POST {ANNOUNCE_RELAY_URL}/announce` (token) → `audio_url`. Timeout
     `ANNOUNCE_SYNTH_TIMEOUT` (default 45s to survive cold synth).
  3. Optional `POST media_player/volume_set` (`ANNOUNCE_VOLUME`) to targets.
  4. `POST {HA_BASE_URL}/api/services/media_player/play_media` with
     `{"entity_id": targets, "media_content_id": audio_url, "media_content_type": "music"}`.
     Timeout 10s. Auth `PX_HA_TOKEN`.
- `PX_DRY=1` → skip relay + HA calls, emit `{"status":"dry", ...}`.
- Output: `{"status":"ok","audio_url","targets","voice","cached"}` or
  `{"status":"error","error":"..."}`. Never raises — the robot's own espeak
  speaker path is independent and unaffected by relay/HA failure.

### Component 3 — Config (`src/pxh/spark_config.py`)

```python
ANNOUNCE_ENABLED      = True
ANNOUNCE_RELAY_URL    = "http://M5.local:7861"
ANNOUNCE_VOICE        = "data"
ANNOUNCE_TARGETS      = ["media_player.nest_hub_max", "media_player.nest_mini"]
ANNOUNCE_VOLUME       = 0.5
ANNOUNCE_SYNTH_TIMEOUT = 45
ANNOUNCE_MAX_CHARS    = 300   # bounds synth time + URL/log size
```

Secrets (`ANNOUNCE_RELAY_TOKEN`, `PX_HA_TOKEN`) live in `.env`, not config.

### Component 4 — Triggers (thin dispatch into `tool-announce`)

1. **Voice loop** — new `tool_announce` following the 6-step *Adding a New Tool*
   checklist: `ALLOWED_TOOLS` + `TOOL_COMMANDS`, a `validate_action` branch,
   prompt docs, persona docs, a dry-run test. SPARK announces on request
   ("tell the house dinner's ready").
2. **px-mind expression** — add `announce` to the valid-action list (becomes the
   22nd). Autonomous announcements, gated by suppressors and a cooldown.
3. **message_obi delivery** — the existing `message_obi` nudge additionally
   fires an announce so Obi *hears* it. Uses `cache:false` (private). Redaction
   of `thoughts-spark.jsonl` is unchanged; audio is the intended delivery.

## Safety / suppressors

- **Night silence (hardcoded 19:00–07:00 Hobart)** applies to *all* announce
  paths, including user-initiated voice-loop ones — never blast the house at
  night.
- **Quiet time / school / bedtime** (calendar-driven) gate the **autonomous**
  px-mind `announce` action, same as other expression actions. A user-initiated
  voice-loop announce bypasses these (the user is present and asking) but still
  respects night silence.
- `PX_DRY=1` gates all network egress.
- **Target allowlist:** `validate_action` must reject any target not in
  `ANNOUNCE_TARGETS` (or a known-Nest allowlist) so the LLM cannot drive
  arbitrary HA `media_player` / entities. Voice locked to `ANNOUNCE_VOICE`
  unless in an allowed voice set. Text clamped to `ANNOUNCE_MAX_CHARS`.

## Error handling

| Failure | Behavior |
|---|---|
| M5 asleep / relay down | `tool-announce` → `{status:error}`, logged; caller continues |
| afterwords error | relay `502` → tool error |
| Synth cold-timeout | generous 45s budget + keep-warm; else tool error |
| HA down / token 401 | tool error, logged |
| Nest target `unavailable` | HA may `200` with no audio; log target states |

No failure crashes px-mind or the voice loop; SPARK's robot-speaker path is
fully independent.

## Privacy notes

- afterwords stays localhost; only the thin relay is LAN-exposed.
- `/audio` is unauthenticated by necessity (Chromecast). Mitigations:
  unguessable 64-bit hash filenames, home-LAN-only, TTL cleanup. Private
  message audio (`message_obi`, later `message_adrian`) uses `cache:false` +
  30-min TTL so spoken DM content is short-lived on disk.

## Testing

- **Relay** (`m5/announce-relay/tests/`): mock afterwords HTTP — assert synth
  params, atomic WAV write, cache hit on repeat, token enforcement on
  `/announce`, open `/audio`, `/health`, TTL cleanup.
- **`tool-announce`** (`tests/test_tools.py`, `isolated_project`): dry-run emits
  `status:dry` with no network; mocked relay+HA test asserting both payloads and
  target resolution; error paths (relay down, HA 401).
- **`voice_loop.validate_action`**: target allowlist rejection, text clamp,
  voice lock → env vars.
- **px-mind**: `announce` action dispatch + suppressor/night-silence gating.

## Deployment / rollout

- Relay → M5 via launchd plist + install script.
- Spark code → Pi via `git pull`. `tool-announce` and the voice-loop tool need
  no restart (fresh subprocess per wake event); **px-mind restart** required to
  pick up the new `announce` action.
- `ANNOUNCE_ENABLED` gates the whole feature; ships `False` until the relay is
  live on M5, then flipped `True`.

## Out of scope (this sub-project)

- Listen-via-Nest (deferred spike).
- `message_adrian` channel (Sub-project B — mirrors `message_obi`: cognitive
  action, exponential backoff, public-feed redaction, dashboard surface,
  delivered via this announce pipeline in the `data` voice).
- Matching SPARK's espeak/GLaDOS robot voice on Nest (the data voice is the
  intended household voice).
- Per-room dynamic targeting by the LLM (config default set only for v1).
- Save/restore of whatever the Nest was playing before an announce.
```
