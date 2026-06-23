# Announce Pipeline — Data-voice announcements over Google Nest

**Date:** 2026-06-19
**Status:** Design — revised after multi-model QA (hermes / codex / agy)
**Sub-project:** A of 2 (B = `message_adrian` channel, separate spec later)

## Goal

Let SPARK speak through the household Google Nest devices in a chosen voice
(the cloned **`data`** voice from the local Afterwords TTS server on M5), as a
**deliberate "announce" capability** — not ambient mirroring of all robot
chatter.

The original idea also asked for SPARK to *listen* via Nest. That half is **out
of scope**: Google shut down custom Conversational Actions in 2023, and a bare
Nest speaker cannot be an HA Assist "listen" endpoint (HA can't intercept the
Nest microphone). A listen path is deferred to a separate spike.

## Constraints discovered during exploration (verified)

1. **Afterwords TTS is localhost-only** — `127.0.0.1:7860` on M5
   (`192.168.0.100`), launchd-managed, 203 voices including `data`. Verified by
   `lsof` (binds `127.0.0.1` only) and by the Pi failing to reach M5:7860 by
   **both hostname and IP** (`http 000`). Note: `bin/tool-voice` *defaults*
   `PX_TTS_SPARK`/`PX_TTS_VIXEN` to `http://M5.local:7860`, but that path is
   **currently dead in production** (connection fails → silent espeak fallback).
   The relay therefore also revives cross-machine TTS that tool-voice can't
   currently reach. Keeping afterwords localhost-bound is a deliberate security
   choice (vs. rebinding it to `0.0.0.0`); the relay is the LAN front door.
2. **Synth latency: ~2–8s warm, ~33s cold** (model load), measured. Too slow to
   stream live to a Nest — audio must be **pre-synthesized to a complete file,
   then cast**.
3. **No server-side caching / non-deterministic output** — identical text yields
   different bytes per call. Caching added at the relay.
4. **`data` voice synthesizes** via `GET /synthesize?text=...&voice=data&lang=en`
   → `audio/wav` (24 kHz mono PCM). `lang` IS in the contract (openapi
   `SynthesizeRequest`, default `en`).
5. **Chromecast can't send auth headers** when fetching media → the audio URL it
   loads must be unauthenticated.
6. **Google Nest can't resolve `.local`/mDNS** (hardcoded public DNS) → the
   `audio_url` must use M5's **IP** (`192.168.0.100`), never `M5.local`.
7. **No HA speaker group exists** — casting to multiple distinct `media_player`
   entities plays them out of sync (echo). v1 targets a **single** entity.
8. **Castable entity is ambiguous** — the entities exposing `group_members`
   (Cast integration) are the `_2` variants (e.g. `media_player.nest_hub_max_2`),
   so the entity that actually casts may not be the unsuffixed name. **Must be
   validated live** (see Pre-implementation gates).

## Pre-implementation validation gates

Two integration risks must be proven with a throwaway test **before** building,
because failure invalidates parts of the design:

- **G1 — WAV-on-Cast.** Manually `media_player.play_media` a static 24 kHz WAV
  URL (served from M5 by IP) to a Nest and confirm it audibly plays. If it
  doesn't, the relay must transcode to MP3/AAC (`Content-Type: audio/mpeg`) —
  fold that into the relay before proceeding.
- **G2 — Correct entity + media_content_type.** Determine which entity ID
  actually casts (`nest_hub_max` vs `nest_hub_max_2`) and which
  `media_content_type` works (`audio/wav` MIME vs HA's `"music"`). Pin both in
  config from the test result.

## Architecture

```
SPARK trigger                       M5 (192.168.0.100)
(voice loop / px-mind /     ┌──────────────────────────────────┐
 message_obi)               │  announce-relay  :7862 (LAN)      │
      │                     │     POST /announce {text,voice}   │
      ▼                     │        │  (per-key synth lock)    │
  bin/tool-announce ───POST─┼────────┘                          │
   (Pi, picar.local)        │        ▼                          │
      │  audio_url           │  afterwords :7860 (localhost)     │
      │  (IP-based)          │     GET /synthesize → WAV         │
      │                     │   validate → atomic write          │
      │                     │   public:  cache/<hash>.wav        │
      │                     │   private: priv/<random>.wav       │
      │                     │     GET /audio/<id>.wav (open)     │
      │                     └──────────────────────────────────┘
      ▼
  HA REST: media_player.play_media(audio_url → single Nest/group)
      ▼
  Nest plays the data-voice WAV
```

### Component 1 — M5 announce relay (`m5/announce-relay/`)

Small FastAPI service deployed to M5, launchd-managed alongside afterwords,
bound `0.0.0.0:7862` (LAN). **Port 7862**, not 7861 (Pi's `px-tts-glados` uses
7861 — avoid the confusion). Keeps afterwords unexposed; only this thin relay is
on the LAN.

**`POST /announce`**
- Auth: `Authorization: Bearer <ANNOUNCE_RELAY_TOKEN>`.
- Body: `{"text": str, "voice": str = "data", "cache": bool = true}`. `lang`
  fixed to `en` for v1 (param reserved, not exposed).
- Hardening: enforce `RELAY_ALLOWED_VOICES` (default `["data"]`) — reject
  unknown voices even with a valid token; max text bytes; simple per-token rate
  limit; cap total cache dir size.
- Behavior:
  - **Sanitize** text (strip markdown/emoji/control chars/`<>` `*`) before hash
    + synth, so afterwords never gets garbage.
  - `key = sha256(f"{voice}|en|{text}")`
  - **public** (`cache:true`): file `cache/<key[:16]>.wav`. Per-key in-process
    lock so two identical requests don't double-synth; on hit return cached.
  - **private** (`cache:false`): file `priv/<random-uuid>.wav` in a **separate
    dir** — never the predictable `cache/` path, so private text can't collide
    with or be served under a guessable public hash.
  - Synth: `GET http://127.0.0.1:7860/synthesize?text=<enc>&voice=<voice>&lang=en`.
    **Validate** before caching: HTTP 200, `Content-Type: audio/wav`, nonzero
    length, RIFF/WAVE header. Reject+error otherwise (don't cache error pages).
    Serialize synth jobs (afterwords is single-model) to bound concurrency.
  - Atomic write (`.tmp` + `os.replace`).
- Response 200: `{"audio_url": "http://192.168.0.100:7862/audio/<id>.wav",
  "voice", "cached", "duration_s"}` (IP-based URL — see constraint #6).
- Errors: `400` empty/oversized/bad-voice, `401` bad token, `429` rate-limited,
  `502` afterwords error, `504` synth timeout.

**`GET /audio/<id>.wav`** — static WAV, **no auth** (Chromecast). Strictly
validate `<id>` against `^[a-f0-9-]{16,36}\.wav$` and resolve within the audio
dirs only (**no path traversal**). `Content-Type: audio/wav`, `404` if missing.
Public hashes are **not secret** (likely phrases are guessable) — acceptable for
home-LAN, non-private announcements; privacy for DMs comes from the random
`priv/` namespace + short TTL.

**`GET /health`** — `{"status":"ok","afterwords": bool, "cache_files": int}`.

**Cache hygiene:** a **background janitor** (startup + interval, not only on
`/announce`) deletes `cache/*.wav` older than `RELAY_CACHE_TTL_DAYS` (7) and
`priv/*.wav` older than `RELAY_PRIVATE_TTL_MIN` (**3 min**, tightened per QA).

**Keep-warm:** **default OFF** for v1 (a 240 s synth loop keeps M5's GPU busy
and blocks Mac sleep). First announce after idle eats the ~33 s cold start; the
async caller path (below) keeps that off the robot's critical loop. Optional
`RELAY_KEEP_WARM=1` (separate `warm/` dir excluded from `cache_files`); a future
refinement can gate it on `awareness.json` presence.

**Deployment:** `m5/announce-relay/com.spark.announce-relay.plist` +
`install.sh`; config from a local `.env`. Pure-stdlib where practical;
FastAPI/uvicorn as the one dependency (documented in a `requirements.txt`).

### Component 2 — Pi tool (`bin/tool-announce`)

Standard `tool-*` pattern (bash + embedded Python heredoc, `/usr/bin/python3`);
**stdlib `urllib.request` only** (system Python lacks `requests`), matching
`mind.py`. Emits one JSON object. The voice-loop action key is `tool_announce`
(underscore); the bin file is `tool-announce` (hyphen) — same convention as
`tool-voice`/`tool_voice`.

- Env in: `PX_ANNOUNCE_TEXT` (required), `PX_ANNOUNCE_TARGETS` (optional),
  `PX_ANNOUNCE_PRIVATE` (`1` for DM audio → relay `cache:false`).
- **Liveness (critical):** synth can take tens of seconds. The tool MUST
  `yield_alive` and set `state/exploring.json` while it waits, exactly like
  other slow tools, so `px-alive` doesn't kill/restart it mid-call. Separate
  timeouts: **connect 5 s** (fast-fail if relay/M5 down) and **read 70 s**
  (survives a cold 33 s synth + overhead). Callers (voice loop / px-mind) must
  not impose a shorter timeout.
- Flow:
  1. Resolve voice (hardcoded `data` v1) + targets: env → `ANNOUNCE_DEFAULT_TARGETS`.
     Reject any target not in `ANNOUNCE_ALLOWED_TARGETS`.
  2. `POST {ANNOUNCE_RELAY_URL}/announce` (token) → `audio_url`.
  3. **No `volume_set` in v1** (dropped per QA — it leaves Nest volume stuck;
     restore is out of scope).
  4. **Pre-check** target `media_player` state via HA; if a target is
     `playing`, log that the announce is **destructive** to current playback
     (documented behavior; save/restore deferred). If `unavailable`, skip that
     target and log — don't fail the whole call.
  5. `POST {HA_BASE_URL}/api/services/media_player/play_media` per the exact HA
     shape below.
  6. `update_session(...)` to record the announce (all tools do).
- `PX_DRY=1` → skip relay + HA, emit `{"status":"dry", ...}`.
- Output: `{"status":"ok|dry|error", "audio_url", "targets", "voice", "cached",
  "duration_s"}` / `{"status":"error","error":...}`. Never raises — the robot's
  espeak path is fully independent of relay/HA failure.

**Exact HA REST payloads** (verified shape; `urllib`, `Authorization: Bearer
$PX_HA_TOKEN`):

```
POST {HA_BASE_URL}/api/services/media_player/play_media
{ "entity_id": "media_player.<castable>",
  "media_content_id": "http://192.168.0.100:7862/audio/<id>.wav",
  "media_content_type": "<audio/wav | music — pinned by gate G2>" }
```

Treat success as HTTP 2xx; log the returned state array. (`entity_id` accepts a
string or list; v1 uses one entity per constraint #7.)

### Component 3 — Config (`src/pxh/spark_config.py`)

```python
ANNOUNCE_ENABLED         = False  # ships off; flip True once relay is live on M5
ANNOUNCE_RELAY_URL       = "http://192.168.0.100:7862"   # IP, not M5.local (#6)
ANNOUNCE_VOICE           = "data"
# v1: single entity to avoid multi-target echo (#7); IDs pinned by gate G2.
ANNOUNCE_DEFAULT_TARGETS = ["media_player.nest_hub_max"]
ANNOUNCE_ALLOWED_TARGETS = ["media_player.nest_hub_max", "media_player.nest_mini",
                            "media_player.googlehome1094"]
ANNOUNCE_MAX_CHARS       = 200   # ~15-20s audio; bounds synth time + URL/log
ANNOUNCE_CONNECT_TIMEOUT = 5
ANNOUNCE_READ_TIMEOUT    = 70
HA_BASE_URL              = "http://homeassistant.local:8123"
NIGHT_SILENCE_START_H    = 19    # was hardcoded; sourced here, applied in HOBART_TZ
NIGHT_SILENCE_END_H      = 7
```

Secrets (`ANNOUNCE_RELAY_TOKEN`, `PX_HA_TOKEN`) live in `.env`, not config.

### Component 4 — Triggers (thin dispatch into `tool-announce`)

1. **Voice loop** — new `tool_announce` (6-step *Adding a New Tool* checklist:
   `ALLOWED_TOOLS` + `TOOL_COMMANDS`, `validate_action` branch, prompt docs,
   persona docs, dry-run test). `validate_action`: clamp text to
   `ANNOUNCE_MAX_CHARS`, reject targets ∉ `ANNOUNCE_ALLOWED_TARGETS`, voice
   hardcoded `data` (no allowlist machinery in v1 — YAGNI). Announce on request.
2. **px-mind expression** — add `announce` to the valid-action list (22nd),
   gated by suppressors + a cooldown. **Dispatch non-blocking** (fire the slow
   synth off the awareness/expression critical path) so a 33 s cold synth never
   stalls the cognitive loop.
3. **message_obi delivery** — the existing nudge additionally fires an announce
   with `PX_ANNOUNCE_PRIVATE=1` (relay `priv/` namespace) so Obi *hears* it.
   `thoughts-spark.jsonl` redaction is unchanged; audio is the intended channel.

## Safety / suppressors

- **Night silence** applies to *all* announce paths (incl. user-initiated),
  using `HOBART_TZ` and the `NIGHT_SILENCE_*` bounds from `spark_config` — never
  hardcoded offsets.
- **Quiet time / school / bedtime** gate the **autonomous** px-mind `announce`;
  a user-initiated voice-loop announce bypasses those but still obeys night
  silence.
- `PX_DRY=1` gates all network egress.
- **Target safety:** `validate_action` rejects targets ∉ `ANNOUNCE_ALLOWED_TARGETS`
  so the LLM can't drive arbitrary HA entities. Relay independently enforces
  `RELAY_ALLOWED_VOICES` (defense in depth — don't rely only on the Pi side).

## Error handling

| Failure | Behavior |
|---|---|
| M5 asleep / relay down | 5 s connect-timeout → `{status:error}`, logged; caller continues |
| afterwords error / bad output | relay `502` → tool error (validated, not cached) |
| Synth cold | 70 s read budget + yield_alive; else tool error |
| HA down / token 401 | tool error, logged |
| One Nest `unavailable` | skip + log that target; other targets proceed |
| Concurrent announces | relay per-key lock + serialized synth; HA calls best-effort |

No failure crashes px-mind or the voice loop; the espeak path is independent.

## Privacy notes

- afterwords stays localhost; only the thin relay is LAN-exposed.
- `/audio` is unauthenticated (Chromecast). Public cache hashes are **guessable
  for common phrases — treated as non-secret**. DM audio (`message_obi`, later
  `message_adrian`) uses the random `priv/` namespace + 3 min TTL + background
  janitor, so spoken DM content is short-lived and unguessable on disk.

## Testing

- **Relay** (`m5/announce-relay/tests/`, mock afterwords HTTP): synth params,
  output validation (rejects non-WAV/empty), atomic write, public cache hit +
  per-key lock, private random namespace, token + voice-allowlist enforcement,
  **path-traversal rejection on `/audio`**, janitor TTL (public + private),
  `/health`.
- **`tool-announce`** (`tests/test_tools.py`, `isolated_project`): dry-run →
  `status:dry`, no network; mocked relay+HA asserting exact payloads + target
  resolution; target-allowlist rejection; unavailable-target skip; `update_session`
  written; error paths (relay down, HA 401).
- **`voice_loop.validate_action`**: text clamp, target allowlist → env vars.
- **px-mind**: `announce` dispatch (non-blocking), suppressor + night-silence
  gating via `spark_config` bounds.

## Deployment / rollout

- **Gates G1 + G2 first** (throwaway live test) — pin entity + media type, prove
  WAV-on-Cast (or add transcode).
- Relay → M5 via launchd plist + install script.
- Spark code → Pi via `git pull`. `tool-announce` + voice-loop tool need no
  restart (fresh subprocess per wake event); **px-mind restart** for the new
  `announce` action.
- `ANNOUNCE_ENABLED=False` until the relay is live on M5, then flip `True`.

## Out of scope (this sub-project)

- Listen-via-Nest (deferred spike).
- `message_adrian` channel (Sub-project B — mirrors `message_obi`: cognitive
  action, exponential backoff, public-feed redaction, dashboard surface,
  delivered via this pipeline in the `data` voice).
- Matching SPARK's espeak/GLaDOS robot voice on Nest.
- Per-room LLM targeting; multi-room synchronized casting (needs an HA speaker
  group — create one and target it as a single entity later).
- Save/restore of prior Nest playback (announce is destructive in v1).
- Reviving `tool-voice`'s dead M5:7860 network-TTS path (the relay makes it
  possible; wiring it is a separate change).

## QA revisions applied (hermes / codex / agy)

mDNS→IP URL (codex+agy blocker); relay port 7861→7862 (hermes); constraint #1
reframed + verified (hermes); exact HA payload + `media_content_type` gate
(codex+hermes); drop `volume_set` (all three); single-target/echo + speaker-group
note (agy); `yield_alive`/non-blocking liveness (agy); path-traversal guard on
`/audio` (agy); `RELAY_ALLOWED_VOICES` (codex) + drop Pi voice allowlist
(hermes YAGNI); private/public namespace split + janitor + 3 min TTL
(codex+hermes+agy); split connect/read timeouts (all); per-key synth lock +
serialize (codex+hermes); `DEFAULT`/`ALLOWED` target split (codex);
`ANNOUNCE_ENABLED=False` in code block (codex); validate synth output before
cache (codex); keep-warm default-off + separate dir (codex+agy+hermes); `_2`
canonical-entity gate (hermes); `MAX_CHARS`→200 + `duration_s` (hermes); night
silence via `HOBART_TZ`+config (hermes); `atomic_write`/`urllib`/`update_session`
reuse (hermes); text sanitization (agy); relay rate-limit/size caps (codex);
unavailable-target skip (hermes). Architecture (M5 relay) endorsed by codex +
hermes; agy's reverse-proxy alternative rejected (re-exposes afterwords, loses
caching/keep-warm locality).
```
