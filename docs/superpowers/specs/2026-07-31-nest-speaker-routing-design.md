# Nest Speaker Routing — Design

**Date:** 2026-07-31
**Status:** Approved for planning
**Goal:** SPARK speaks through the Nest speaker nearest Adrian, using the afterwords
`data` voice on M5. The onboard speaker becomes a fallback rather than the primary
output.

## Motivation

The onboard microphone cannot hear Adrian over ambient noise, and the onboard
speaker is easy to miss from another room. The Nest speakers are already in every
room that matters and already carry SPARK's *input* — what is missing is output.

## What already works (verified 2026-07-31)

`ha/custom_components/spark_conversation/conversation.py:74` returns replies via
`intent_response.async_set_speech()`. Home Assistant speaks those on whichever
satellite heard the command. **Conversational replies are already correctly
routed and need no work.** This design covers SPARK-initiated speech.

## Findings that shaped the design

All verified by querying the live HA instance (2440 entities) on 2026-07-31.

### There is no room presence sensing

Every person-occupancy entity is Frigate-derived from a camera: `driveway`,
`front_yard`, `front_entrance`, `garden`, `indoor_area`, `picar_x`, `picamera`,
`nest_hub_max`. Searching all domains for office/shed/shack/living returns only
lights, buttons and media players.

**Nest Minis expose exactly one entity each — a `media_player`. No sensors.**
The Hub Max is the only speaker with presence, and only because it has a camera
Frigate consumes.

Nest Minis do contain ultrasound proximity hardware (it drives the volume LEDs),
but Google has never exposed it through any API. It cannot reach HA. There is no
setting to enable and nothing pending.

### The configured announce targets are wrong

`ANNOUNCE_DEFAULT_TARGETS` and `ANNOUNCE_ALLOWED_TARGETS` name
`media_player.nest_mini`, which is **unavailable** in HA. As shipped, announce
would fail to resolve a working office target even after `ANNOUNCE_ENABLED` is
flipped.

### The announce pipeline has never run

M5 pings, but nothing listens on `:7862` (relay) or `:7860` (afterwords). Gates
G1 and G2 are unrun because there has been nothing to run them against.

## Prerequisites (manual, on M5 — not code)

- **P1** — Deploy afterwords with the `data` voice and the relay
  (`m5/announce-relay/install.sh`).
- **P2** — Run gate G1 (a static WAV actually casts to a Nest; transcode to MP3
  if not) and gate G2 (which entity casts, and the working
  `media_content_type`). Pin the results into `spark_config.py`.
- **P3** — Measure **warm** synth latency. `ANNOUNCE_READ_TIMEOUT` is 70s against
  a ~33s cold synth. Acceptable for an autonomous comment; far too slow for a
  conversational reply. Since all speech now routes to Nest, if warm synth is not
  ≲2s the voice-loop path needs a short fail-fast timeout and will drop to
  onboard often.
  - **MEASURED 2026-08-01 (deploy gate):** same short text end-to-end via
    `bin/tool-announce`: cold 11.6s, cache-hit 9.8s — the synth delta is
    ~1.7s; the floor is the yield_alive preamble + cast + the 2.2s
    volume-restore sleep. Novel-sentence routed speech via `bin/tool-voice`:
    **14.3s** wall. Over the 10s conversational-comfort bar, but the
    voice-loop path never routes anyway (`PX_VOICE_NO_ROUTE=1` in
    `execute_tool` — the human is at the robot), so only autonomous speech
    pays it. `SPEAKER_ROUTE_TIMEOUT_S=90` has ~6× headroom; no tuning
    needed.

## Architecture

Four small pieces, each testable in isolation.

### 1. `src/pxh/speaker_router.py` (new)

One job: return a single `media_player.*` entity, or `None`.

```
choose_target(last_heard, available, now) -> str | None
```

A pure function plus a thin HA availability fetch, so the logic tests without HA
or hardware. Resolution order:

1. `last_heard` room, if its age < `SPEAKER_STICKY_S` and the entity is available
2. `SPEAKER_DEFAULT_ROOM` (office), if available
3. first available entry in `ANNOUNCE_ALLOWED_TARGETS` order
4. `None` — caller falls back to the onboard speaker

### 2. `state/last_heard.json`

`{"room": "office", "ts": "<iso>"}`. Written when Adrian speaks to a Nest.

`conversation.py` has `hass`, so it resolves the **area name** locally and sends
it in the existing `POST /api/v1/public/chat` body; `api.py` records it. Sending
the area name rather than a raw `device_id` avoids maintaining a device-ID map
that breaks whenever a speaker is re-paired.

This is the only signal that identifies *Adrian* rather than "a human is in this
room" — which matters in a four-person house.

### 3. `bin/tool-voice` — the routing chokepoint

Attempts announce with the resolved target, falls back to onboard espeak on any
failure. Every existing caller inherits routing with no change, mirroring how
night silence is enforced in exactly one place today.

### 4. `src/pxh/spark_config.py`

```python
SPEAKER_ROOMS = {
    "office": "media_player.googlehome1094",        # "Office Mini"
    "shed":   "media_player.laura_s_room_speaker",  # "Shed Mini"
    "living": "media_player.nest_hub_max",
}
SPEAKER_DEFAULT_ROOM = "office"
SPEAKER_STICKY_S = 1800
```

Plus the P2/P3 corrections to `ANNOUNCE_DEFAULT_TARGETS` and
`ANNOUNCE_ALLOWED_TARGETS`. `ANNOUNCE_VOICE` stays `"data"`.

**`media_player.shack_speakers` is deliberately excluded.** It is 6 speakers on
one amp off a Chromecast Audio — 4 in the living room, 2 outside. Routing there
overlaps `nest_hub_max` and reintroduces exactly the echo that keeping v1
single-target avoids. It is also the one device genuinely used for music.

## Data flow

```
Adrian speaks to a Nest
  → HA conversation → spark_conversation resolves area
  → POST /api/v1/public/chat {…, area}
  → api.py writes state/last_heard.json

SPARK wants to speak
  → tool-voice → speaker_router.choose_target()
  → tool-announce (night-silence gate)
  → relay → afterwords `data` → cast to entity
  → on any failure: onboard espeak
```

## Interrupt and restore

**A Cast session generally cannot be resumed.** Casting to a Chromecast tears
down the running app's session — a Spotify stream ends, it is not paused. HA has
no generic save/restore for this. The design is explicit about the split:

Snapshot `state`, `volume_level` and `app_name` before casting; cast; sleep
`duration_s` + 1s; restore.

- **Volume is always restored.** Reliable, and the case that actually bites: an
  announcement at 0.87 followed by music at the wrong level.
- **Playback is best-effort.** If the snapshot said `playing`, issue
  `media_player.media_play`. Works when the app survived (Bluetooth Audio, a
  paused local session); will not work for a torn-down Spotify cast. Fails
  silently rather than erroring.

The relay already returns `duration_s` (`m5/announce-relay/announce_relay/app.py:96`),
so restore timing needs no M5 change.

Restore is **skipped entirely** when the announce was suppressed or failed —
nothing was interrupted.

True Spotify resume would require the Spotify integration re-issuing playback
(spotcast-style). Out of scope; it should not be smuggled into this work.

## Error handling

Every failure degrades one step and never raises.

| Condition | Behaviour |
|---|---|
| Relay unreachable / synth timeout | Onboard espeak, same turn. Fast-fail timeout on the voice-loop path (P3 sets the value); the autonomous path may keep the current 70s. |
| Chosen entity `unavailable` | Next candidate → default room → onboard |
| `last_heard.json` missing/malformed/stale | Treated as absent → default room. Staleness as absence prevents SPARK stranding itself talking to an empty shed. |
| Night silence | Unchanged, gated inside `tool-announce`. Routing happens *after* the gate, so a suppressed announce **must not** fall through to the onboard speaker. |
| HA unreachable | Skip the availability check, use the default room; the cast either works or falls back. |

## Testing

The router is pure, so most of this needs neither HA nor hardware.

- `choose_target()`: fresh last-heard wins; stale ignored; unavailable skipped;
  empty inputs return `None`.
- `tool-voice` falls back to onboard when the relay refuses connection (point it
  at a dead port).
- Night silence still suppresses, **and suppression does not trigger onboard
  fallback**.
- `api.py` records `{room, ts}` from a chat POST carrying an area. Area names are
  user-controlled strings reaching a file, so they pass through
  `_sanitize_chat_text()` and are matched against `SPEAKER_ROOMS` keys rather
  than trusted.
- Volume is restored after an interrupting announce; restore is skipped when the
  announce was suppressed.

## Out of scope

- Multi-room / simultaneous announce (v1 stays single-target to avoid echo)
- True Spotify session resume
- Adding mmWave or other room presence hardware
- Any change to the conversational reply path, which already works
