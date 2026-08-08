# Hub Max announcement cards — design

**Date:** 2026-08-06
**Status:** approved, pending implementation plan
**Component:** `m5/announce-relay` (deployed copy: `~/announce-relay`)

## Problem

Announcements are audio-only. The Nest Hub Max has a screen and shows nothing —
the announcement arrives, the speech plays, and a glance at the device tells you
nothing about what was said or whether you already dealt with it.

Casting an image and then casting audio does not layer: the second `play_media`
replaces the first receiver app, so you get the picture or the voice, never both.
The only way to get a still image with synchronised speech on a Cast device is to
mux them into a video and cast that.

## Scope

A `POST /card` endpoint on the announce relay that renders a status card, muxes it
with the synthesised speech into an MP4, and serves it for casting. Plus the
Home Assistant routing change to prefer the Hub Max.

Out of scope: changes to `/announce` (untouched), the shack-music fix (separate,
already built and awaiting deployment), and any card for the minis (no screens).

## Visual style

Inherited from adrianwedd.com, not from the SPARK thought card. The palettes are
identical (`#1a181c` ground, `#e2ddd8` text, `#c48b6e` copper, `#968e96` muted,
`#3d3844` border), so this is a typography and layout decision, not a colour one:
the site's system-sans is legible across a room where the SPARK card's
Nimbus mono-italic is not.

`scripts/generate-og-images.py` in the adrianwedd.com repo is the direct
reference. Its `get_font()` macOS probe list is lifted verbatim — the relay runs
on the Mac, and the SPARK card's hardcoded Debian font paths would silently fall
through to a bitmap default there.

### Layout — 1280x800 (Hub Max native, no pillarboxing)

```
██████████████ 6px variant accent bar ██████████████

    8pm dose                          <- SF Pro Bold ~64pt, #e2ddd8
    ────────                          <- hairline, #3d3844

    Time for your evening meds —      <- SF Pro Regular ~32pt, #968e96
    the blue box on the kitchen
    bench.

    ● MEDS                    20:14   <- variant label + local time
██████████████ 6px variant accent bar ██████████████
```

Headline is a new caller-supplied field, distinct from the spoken body. The body
text is unbounded in practice (LLM-generated task reminders), so it wraps and
truncates; the headline is what carries at 2 metres.

### Variant accents

All four from the site's status tokens:

| Variant | Accent    | Token                       |
| ------- | --------- | --------------------------- |
| `meds`  | `#f87171` | `--color-status-error`      |
| `wan`   | `#c084fc` | `--color-status-experiment` |
| `task`  | `#c48b6e` | `--color-accent` (copper)   |
| `water` | `#4ade80` | `--color-status-active`     |

Unknown variant falls back to copper rather than erroring.

## Architecture

Two new modules, both pure functions with no HTTP knowledge, testable in isolation:

### `announce_relay/card.py`

`render_card(headline, body, variant, when) -> Path`

Pillow. Renders the layout above to a PNG in `PRIV_DIR`. Font resolution probes
the macOS paths from the site's OG script.

**One deliberate divergence from the reference:** if the truetype probe falls
through to `ImageFont.load_default()`, raise rather than continue. The site's
script tolerates the bitmap fallback because a slightly-wrong OG image still
functions; a bitmap-font card at 2 metres is illegible, and failing here routes
into the audio fallback below, which is the better outcome.

### `announce_relay/video.py`

`mux(png_path, wav_path) -> Path`

`ffmpeg -loop 1 -i card.png -i speech.wav -c:v libx264 -pix_fmt yuv420p -shortest`,
writing MP4 to `PRIV_DIR`. Adds ~1.5s trailing silence so Cast teardown does not
clip the final word. `yuv420p` is required — Chromecast will not decode the
`yuv444p` libx264 defaults to for some inputs.

ffmpeg is invoked with an explicit timeout and a non-zero exit is a normal,
handled outcome, not an exception to propagate.

### `POST /card`

Body: `{headline, body, variant, voice?}`. Same auth, same rate limit, same
sanitisation and byte cap as `/announce`, applied to headline and body
independently.

Flow: synthesise (reusing the existing serialised synth gate and cache) → render
card → mux → return.

Response: `{url, kind, duration_s}` where `kind` is `"video"` or `"audio"`.

**Fallback is the relay's job, not HA's.** If render or mux fails for any reason,
the endpoint returns the bare WAV URL with `kind: "audio"` and HTTP 200. A dose
reminder must not be lost because ffmpeg was missing or a font moved. HA templates
`media_content_type` off `kind`, so it plays whatever came back without needing to
know why.

MP4s are private-TTL artifacts like DM clips: written to `PRIV_DIR`, swept by the
existing janitor, and subject to the same serve-time TTL enforcement.

### `GET|HEAD /video/{name}`

Mirrors the existing `/audio/{name}` route exactly, including the path-traversal
guard, the serve-time private-TTL check, and — critically — the `HEAD` method.
Chromecast preflights with `HEAD` before playing; a 405 there makes the cast load
and then never start. This is already documented in `app.py` for audio and applies
identically to video. `FileResponse` handles Range requests, which Cast issues for
video but not for short WAVs.

## Home Assistant side

`script.announce` reorders its allowlist to try `nest_hub_max` first, then
`office_mini`, then `shed_mini` — still idle-checked, still allowlist-only, and
`shack_speakers` remains absent by construction. The Hub Max branch calls
`rest_command.announce_card`; the minis keep the existing `/announce` audio path.

## Testing

pytest, alongside the existing `test_app` / `test_store` / `test_synth`:

- `card.py` — renders at the right dimensions; each variant gets its accent;
  unknown variant falls back to copper; **truetype-unavailable raises** rather
  than producing a bitmap card; long body truncates without overflowing the frame.
- `video.py` — produces a playable MP4; duration is wav + tail; `yuv420p` is
  actually in the output stream; ffmpeg-missing and ffmpeg-nonzero both surface as
  handled failures.
- `app.py` — `/card` contract; auth and rate limiting match `/announce`;
  **render failure returns 200 with `kind: "audio"` and a working URL**;
  `/video/{name}` serves `HEAD`, rejects traversal, enforces private TTL.

Then three independent QA passes with the `hermes`, `agy`, and `codex` CLI agents
— once against the implementation plan before any code is written, and again
against the implementation before deployment.

## Deployment

`~/announce-relay` is an untracked deployed copy, currently byte-identical to
`m5/announce-relay` in this repo. Develop here, deploy by copying across, then
`launchctl kickstart -k gui/$(id -u)/com.spark.announce-relay`.

`.env` exists only in the deployed copy and is unreadable to the agent; any new
configuration keys must be applied by the operator by hand.

## Risks accepted

Operator chose Hub-Max-first routing knowing it makes most announcements depend on
the render+mux path. The `kind: "audio"` fallback is the mitigation and is
therefore load-bearing, not decorative — it is explicitly tested, and any change
that lets a render failure produce a non-200 is a regression.
