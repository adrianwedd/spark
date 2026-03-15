# px-mind Tool Expansion — Design Spec

**Date:** 2026-03-15
**Status:** Approved

## Goal

Expand px-mind's `expression()` function from 8 actions to 14, giving SPARK a richer behavioural repertoire beyond just speaking. All target tools already exist as `bin/tool-*` scripts.

## New Actions

| Action | Tool Script | GPIO? | Charging Gate | Absent Gate | Description |
|--------|-------------|-------|---------------|-------------|-------------|
| `play_sound` | `tool-play-sound` | No | No | Yes | Play mood-appropriate sound effect |
| `photograph` | `tool-photograph` | Camera | No | No | Take photo, describe scene, speak |
| `emote` | `tool-emote` | Servos | Yes | No | Physical emotional expression |
| `look_around` | `tool-look` | Servos | Yes | No | Move head to look in a direction |
| `time_check` | `tool-time` | No | No | Yes | Proactively announce the time |
| `calendar_check` | `tool-gws-calendar` | No | No | Yes | Check and announce upcoming events |

### Existing Actions (unchanged)

`wait`, `greet`, `comment`, `remember`, `look_at` (voice-only), `weather_comment`, `scan`, `explore`

## Implementation Details

### 1. Mood Mappings

**Mood → sound** (for `play_sound`):
- curious, alert → `beep`
- happy, excited, playful → `tada`
- content, peaceful → `chime`
- all others → `chime` (fallback)

**Mood → emote** (for `emote`):
- Direct: happy→happy, curious→curious, alert→alert, excited→excited, contemplative→thinking, peaceful→shy
- Fallback: `idle`

### 2. Expression Dispatch

Each new action gets an `elif` branch in `expression()`:

- **play_sound**: Map mood to sound name → set `PX_SOUND` env → call `tool-play-sound`
- **photograph**: Call `tool-photograph` → if OK, call `tool-describe-scene` → speak description via `tool-voice`. Needs `yield_alive` (tool-photograph already does this). Do NOT gate on charging — camera works while plugged in.
- **emote**: Map mood to emote name → set `PX_EMOTE` env → call `tool-emote`. Needs `yield_alive` (tool-emote handles via px-emote).
- **look_around**: Pick random pan (-40 to 40) and tilt (-10 to 30) → set `PX_PAN`/`PX_TILT` env → call `tool-look`. Optionally speak the thought after. Needs `yield_alive` (tool-look handles via px-look).
- **time_check**: Call `tool-time` (it speaks internally).
- **calendar_check**: Set `PX_CALENDAR_ACTION=next` → call `tool-gws-calendar` (it speaks internally).

### 3. Gate Updates

**Charging gate** (line ~2073): Add `emote` and `look_around` to the existing set:
```python
if _charging and action in ("scan", "look_at", "explore", "emote", "look_around"):
```

**Absent gate** (line ~2063): Add `play_sound`, `time_check`, `calendar_check`:
```python
if _obi_mode == "absent" and action in ("greet", "comment", "weather_comment", "scan", "play_sound", "time_check", "calendar_check"):
```

### 4. Prompt Updates

Update action lists in all four prompt locations:

1. **REFLECTION_SYSTEM** (default persona) — add descriptions:
   - `"play_sound"` — play a sound that matches your mood (no words)
   - `"photograph"` — take a photo of what's in front of you
   - `"emote"` — express your mood physically (head movement, pose)
   - `"look_around"` — physically move your head to look somewhere
   - `"time_check"` — announce what time it is
   - `"calendar_check"` — check what's coming up today

2. **REFLECTION_SYSTEM_GREMLIN** — same action list
3. **REFLECTION_SYSTEM_VIXEN** — same action list
4. **_SPARK_REFLECTION_SUFFIX** — update the action enum

### 5. Timeouts

- `tool-play-sound`: 15s (audio playback)
- `tool-photograph` + `tool-describe-scene`: 60s (camera + Claude vision)
- `tool-emote`: 15s (servo movement)
- `tool-look`: 15s (servo movement)
- `tool-time`: 15s (speaks)
- `tool-gws-calendar`: 30s (network + speaks)

### 6. Safety

- All tools support `PX_DRY=1` — no hardware changes in dry-run
- `tool-photograph` checks Frigate stream exclusivity before opening camera
- `tool-look` and `tool-emote` handle `yield_alive` internally
- `explore` remains gated on charging in both `_can_explore()` AND `expression()`
- New servo actions gated on charging in `expression()`
- No autonomous photo loops — `photograph` action has natural LLM selection frequency + expression cooldown (2 min)

## Files Modified

1. **`bin/px-mind`**:
   - `expression()`: 6 new `elif` branches
   - Charging gate: add `emote`, `look_around`
   - Absent gate: add `play_sound`, `time_check`, `calendar_check`
   - `REFLECTION_SYSTEM`: expand action list + descriptions
   - `REFLECTION_SYSTEM_GREMLIN`: expand action list
   - `REFLECTION_SYSTEM_VIXEN`: expand action list
   - `_SPARK_REFLECTION_SUFFIX`: expand action enum

## No Files Created

All tools already exist. No new bin scripts needed.

## Testing

- Existing 301 tests should pass (no structural changes)
- Dry-run test: `bin/px-mind --dry-run` should cycle through new actions
- Manual verify: restart px-mind service, watch logs for new action types
