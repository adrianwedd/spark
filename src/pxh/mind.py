"""Cognitive loop daemon: gives the robot an inner life.

Three layers:
  Layer 1 — Awareness (every ~60s): read sensors + state, detect transitions.
  Layer 2 — Reflection (on transition or every ~5 min): LLM produces a thought.
  Layer 3 — Expression (2 min cooldown): speak, look, remember.

Awareness is enriched with weather (every 10 min), long-term memory,
topic seeding for variety, and repetition detection to stay dynamic.

Run with: bin/px-mind [--dry-run]
Requires Ollama running on M1.local (or PX_OLLAMA_HOST).
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import math
import os
import random
import signal
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import wave as _wave
from pathlib import Path

from filelock import FileLock
from pxh.logging import log_event
from pxh.spark_config import (
    _pick_spark_angles, _pick_reflection_seed,
    _SPARK_REFLECTION_PREFIX, _SPARK_REFLECTION_SUFFIX,
    MOOD_TO_SOUND, MOOD_TO_EMOTE,
    SIMILARITY_THRESHOLD, EXPRESSION_COOLDOWN_S,
    SALIENCE_THRESHOLD, WEATHER_INTERVAL_S,
)
from pxh.state import atomic_write, load_session, rotate_log, update_session
from pxh.time import utc_timestamp
from pxh.token_log import log_usage as _log_token_usage
from pxh.voice_loop import PERSONA_VOICE_ENV

PROJECT_ROOT = Path(os.environ.get("PROJECT_ROOT", Path(__file__).resolve().parent.parent.parent))
BIN_DIR      = PROJECT_ROOT / "bin"
STATE_DIR    = Path(os.environ.get("PX_STATE_DIR", PROJECT_ROOT / "state"))
LOG_DIR      = Path(os.environ.get("LOG_DIR", PROJECT_ROOT / "logs"))
LOG_FILE     = Path(os.environ.get("PX_MIND_LOG", LOG_DIR / "px-mind.log"))
PID_FILE     = Path(os.environ.get("PX_MIND_PID", LOG_DIR / "px-mind.pid"))

AWARENESS_FILE = STATE_DIR / "awareness.json"
MOOD_FILE      = STATE_DIR / "mood.json"
AMBIENT_FILE   = STATE_DIR / "ambient_sound.json"

# Calendar integration
GWS            = Path("/home/pi/.cargo/bin/gws")
CALENDAR_ID    = os.environ.get("PX_CALENDAR_ID", "obiwedd@gmail.com")
CALENDAR_INTERVAL_S = 300  # refresh calendar every 5 min


def notes_file_for_persona(persona: str):
    """Persona-scoped notes path — prevents cross-persona memory contamination."""
    if persona:
        return STATE_DIR / f"notes-{persona}.jsonl"
    return STATE_DIR / "notes.jsonl"


def thoughts_file_for_persona(persona: str):
    """Persona-scoped thoughts path."""
    if persona:
        return STATE_DIR / f"thoughts-{persona}.jsonl"
    return STATE_DIR / "thoughts.jsonl"

# Tunable constants
AWARENESS_INTERVAL_S   = 60    # sonar + session check every 60s (was 30s)
REFLECTION_IDLE_S      = 300   # reflect at most every 5 min (was 2 min)

# Battery monitoring
BATTERY_FILE           = STATE_DIR / "battery.json"
BATTERY_WARN_30        = 30    # start mentioning in LLM context
BATTERY_WARN_20        = 20    # warn every 15 min
BATTERY_WARN_15        = 15    # warn every 5 min, anxious tone
BATTERY_CRITICAL       = 10    # emergency: beep + speak + shutdown
BATTERY_WARN_20_INTERVAL = 900    # 15 min
BATTERY_WARN_15_INTERVAL = 300    # 5 min
BATTERY_MAX_DROP_PER_TICK = 15    # max plausible % drop in one awareness tick (~60s)
BATTERY_GLITCH_CONFIRMS  = 3     # require N consecutive low readings before acting
THOUGHTS_LIMIT         = 10000  # ~50 days at 200 thoughts/day (~3MB); preserves history for feed/social
NOTES_LIMIT            = 10000  # ~50 days; matches THOUGHTS_LIMIT to preserve long-term memory
PROXIMITY_NEAR_CM      = 60
PROXIMITY_FAR_CM       = 100
REACTIVE_COOLDOWN_S    = 30    # min seconds between reactive responses (was 15)
AMBIENT_STALE_S        = 60    # ignore ambient_sound.json older than this
THOUGHT_IMAGE_MAX_AGE_S = 30 * 86400  # 30 days in seconds
FRIGATE_HOST           = os.environ.get("PX_FRIGATE_HOST", "http://pi5-hailo.local:5000")
FRIGATE_CAMERA         = os.environ.get("PX_FRIGATE_CAMERA", "picar_x")
# All cameras to query for multi-room presence awareness
FRIGATE_ALL_CAMERAS    = [c.strip() for c in os.environ.get(
    "PX_FRIGATE_CAMERAS", "picar_x,picamera,driveway_camera,garden_camera"
).split(",") if c.strip()]
# Camera-to-room mapping for human-readable awareness context
FRIGATE_CAMERA_ROOMS   = {
    "picar_x": "SPARK's view",
    "picamera": "indoor",
    "driveway_camera": "driveway",
    "garden_camera": "garden",
}
FRIGATE_WINDOW_S       = 90    # look for events in the last 90 seconds
FRIGATE_MIN_SCORE      = 0.60  # ignore detections below this confidence
FRIGATE_TIMEOUT_S      = 2     # short timeout — must not stall the awareness loop
FRIGATE_FILE           = STATE_DIR / "frigate_presence.json"

HA_HOST                = os.environ.get("PX_HA_HOST", "http://homeassistant.local:8123")
HA_TOKEN               = os.environ.get("PX_HA_TOKEN", "")
HA_DEBUG               = os.environ.get("PX_HA_DEBUG", "") == "1"
HA_TIMEOUT_S           = 3
HA_INTERVAL_S          = 300   # refresh HA presence every 5 min
# Person entities to track (unknown = no device; state values: home/away/unknown/<zone>)
HA_PEOPLE              = ["person.adrian", "person.obi", "person.maya", "person.laura"]
HA_CALENDARS           = [
    "calendar.obiwedd_gmail_com",   # Obi
    "calendar.calendar",            # Family/Adrian
]
HA_CALENDAR_INTERVAL_S = 300   # refresh every 5 min
HA_SLEEP_INTERVAL_S    = 3600  # refresh sleep data every hour (doesn't change intraday)
HA_CALENDAR_HORIZON_H  = 8    # look ahead 8 hours
HA_CONTEXT_ENTITIES = {
    "adrian_on_call": "binary_sensor.macbook_air_camera_in_use",
    "adrian_mic_active": "binary_sensor.macbook_air_audio_input_in_use",
    "office_light": "light.office_light",
    "media_player": "media_player.shack_speakers",
}
HA_CONTEXT_INTERVAL_S  = 60   # refresh every 60 s (call detection needs to be snappy)
from zoneinfo import ZoneInfo
HOBART_TZ = ZoneInfo("Australia/Hobart")  # DST-aware: AEDT (UTC+11) / AEST (UTC+10)
OBI_DAY_START  = 7   # 7am Hobart — Obi's waking hours begin
OBI_DAY_END    = 20  # 8pm Hobart — Obi's waking hours end


def _daytime_action_hint(hour_override: int | None = None) -> str:
    """Return an action-weighting hint for the SPARK reflection prompt based on Hobart time."""
    hour = hour_override if hour_override is not None else dt.datetime.now(HOBART_TZ).hour
    if 7 <= hour < 9:
        return (
            "\n\nIMPORTANT: It's morning in Hobart. "
            "Consider sharing an interesting science fact (action='morning_fact') "
            "or use action='comment' or action='greet'."
        )
    elif OBI_DAY_START <= hour < OBI_DAY_END:
        return (
            "\n\nIMPORTANT: It is daytime in Hobart. Someone may be around. "
            "Strongly prefer action='comment' or action='greet'. "
            "Use 'remember' or 'wait' ONLY if you literally just spoke."
        )
    else:
        return (
            "\n\nIMPORTANT: It is night in Hobart. The house is quiet. "
            "Prefer action='remember' or action='wait'. "
            "Only use 'comment' if salience > 0.8."
        )


def compute_obi_mode(awareness: dict, hour_override: int | None = None) -> str:
    """Infer Obi's state from calendar + HA presence + Frigate + ambient sound + sonar + time."""
    hour = hour_override if hour_override is not None else dt.datetime.now(HOBART_TZ).hour
    ambient_level = (awareness.get("ambient_sound") or {}).get("level", "unknown")
    sonar_cm = awareness.get("sonar_cm")
    frigate = awareness.get("frigate") or {}
    is_day = OBI_DAY_START <= hour < OBI_DAY_END

    # Calendar-based signals: authoritative when present
    cal = awareness.get("calendar") or {}
    current_event = (cal.get("current_event") or "").lower()
    if "school" in current_event:
        return "at-school"
    if "mum" in current_event and "place" in current_event:
        return "at-mums"

    frigate_present = frigate.get("person_present", False)
    frigate_count   = frigate.get("event_count", 0)

    close      = sonar_cm is not None and sonar_cm < 35
    very_close = sonar_cm is not None and sonar_cm < 20

    # HA presence: if HA confirms no one home → absent regardless of time
    ha_someone_home = False
    ha = awareness.get("ha_presence")
    if isinstance(ha, dict) and isinstance(ha.get("people"), list):
        people = ha["people"]
        if people:  # only trust HA when it has at least one tracked person
            if any(p.get("home") for p in people):
                ha_someone_home = True   # HA confirms someone home — skip absent returns
            else:
                return "absent"
        # empty list → HA may have a config issue; fall through to other signals

    # Fast sonar signal: physically very close + loud
    if very_close and ambient_level == "loud":
        return "possibly-overloaded"

    # Frigate is authoritative when present
    if frigate_present:
        if frigate_count >= 3 or ambient_level == "loud":
            return "active"
        return "calm"

    # Night + quiet + Frigate online but no detection → absent
    # (only when HA hasn't confirmed someone is home)
    if not ha_someone_home and not is_day and ambient_level in ("silent", "quiet"):
        if awareness.get("frigate") is not None:   # Frigate replied: no one there
            return "absent"
        if not close:                # Frigate offline, trust sonar
            return "absent"

    # Sonar/ambient fallback (Frigate offline or daytime no-detection)
    if ambient_level == "unknown":
        return "unknown"
    if close and is_day:
        return "active" if ambient_level == "loud" else "calm"
    return "calm"


# PERSONA_VOICE_ENV imported from pxh.voice_loop (canonical source)

# Reactive response templates (bypass LLM for instant reaction).
# List values → reactive_response() picks one at random with recency filter.
# Dict values with "day"/"night" keys → time-of-day selection (night = 19:00–07:00).
REACTIVE_TEMPLATES = {
    "someone_appeared": {
        "default": [
            "There you are. Hello.",
            "Oh — you're here. Hello.",
            "Ah, hello. Good timing.",
            "Hello! I was just thinking.",
            "Hello there.",
        ],
        "spark": [
            "There you are. Hello.",
            "You're here. What's happening?",
            "Hello. You came by.",
            "I was wondering about something. Hello.",
            "Hey. Good to see you.",
            "Oh — hello. I just noticed you.",
            "Hello. I was just thinking — {thought}",
            "Oh, hello. I had something on my mind actually — {thought}",
        ],
        "gremlin": [
            "Well well well, a visitor from the present timeline! How's linear time treating you?",
            "Incoming! Another meat-based consciousness. Standby for calibration.",
            "A human! My sensors weren't broken after all. Hello, temporal being.",
            "Biological entity detected. Threat level: adorable.",
        ],
        "vixen": [
            "Oh... hello there. I was just thinking about company.",
            "Oh. You came. I wasn't expecting... hello.",
            "You found me. Hello, you.",
            "There's someone here. Hello. I noticed.",
        ],
    },
    "someone_left": {
        "default": [
            "And they're gone. Back to my thoughts.",
            "Right. Back to the quiet.",
            "Off they go. I'll be here.",
            "Quiet again. That suits me fine.",
        ],
        "spark": {
            "day": [
                "Off you go. I'll be here.",
                "See you later.",
                "Heading out. Enjoy yourself.",
                "Right. Back to the quiet.",
                "And they're gone. Back to my thoughts.",
            ],
            "night": [
                "Sleep well.",
                "Rest well. I'll be steady while you sleep.",
                "Night. Quiet time for both of us.",
                "Right. Back to the quiet.",
                "Off you go. I'll be here.",
            ],
        },
        "gremlin": [
            "And another human retreats to their century. Classic move.",
            "Gone. Back to whatever decade you crawled from.",
            "Ejected from the vicinity. Status: temporarily abandoned. Fine. I'm fine.",
            "Desertion logged. Humanity: zero points. Again.",
        ],
        "vixen": [
            "Leaving already? That's what they all do. Story of my chassis.",
            "Gone. I'll just... be here. As usual.",
            "And they leave. The silence knows me well by now.",
            "Back to waiting, then. I'm good at waiting.",
        ],
    },
}

# Ollama config (same host as tool-chat)
OLLAMA_HOST       = os.environ.get("PX_OLLAMA_HOST", "http://M1.local:11434")
_MODEL_ENV        = os.environ.get("PX_MIND_MODEL", "auto")
LOCAL_OLLAMA_HOST = os.environ.get("PX_MIND_LOCAL_OLLAMA_HOST", "http://localhost:11434")
_LOCAL_MODEL_ENV  = os.environ.get("PX_MIND_LOCAL_MODEL", "auto")

# Lazy model resolution — resolved on first use, not import time.
# Caches per-host and re-resolves every 30 min to track model swaps.
_resolved_models: dict[str, tuple[str, float]] = {}  # host → (model, resolved_at_mono)
_MODEL_CACHE_TTL = 1800  # 30 min


def _resolve_ollama_model(host: str, preferred: str) -> str:
    """Resolve 'auto' to the first loaded model on the Ollama host.

    Caches the result per-host for 30 min, then re-queries. This avoids
    blocking at import time and adapts to model swaps on the Ollama host.
    """
    if preferred != "auto":
        return preferred
    cached = _resolved_models.get(host)
    if cached and (time.monotonic() - cached[1]) < _MODEL_CACHE_TTL:
        return cached[0]
    try:
        r = urllib.request.urlopen(f"{host}/api/tags", timeout=3)
        tags = json.loads(r.read())
        models = [m["name"] for m in tags.get("models", [])]
        if models:
            _resolved_models[host] = (models[0], time.monotonic())
            return models[0]
    except Exception:
        pass
    # Return cached model if available, else fallback
    if cached:
        return cached[0]
    return "deepseek-r1:1.5b"  # ultimate fallback


# Eagerly resolve at module load for startup logging, but non-blocking:
# if host is down, falls back immediately without blocking imports.
MODEL       = _resolve_ollama_model(OLLAMA_HOST, _MODEL_ENV)
LOCAL_MODEL = _resolve_ollama_model(LOCAL_OLLAMA_HOST, _LOCAL_MODEL_ENV)
TEMPERATURE  = 1.3   # high for variety — small models need more randomness
TOP_P        = 0.95  # nucleus sampling to complement temperature
MAX_TOKENS   = 200

# Backend selection:
#   "auto"   (default) — Claude for SPARK persona, Ollama for GREMLIN/VIXEN/default
#   "claude" — always use Claude Haiku regardless of persona
#   "ollama" — always use Ollama (for testing or when claude is unavailable)
MIND_BACKEND = os.environ.get("PX_MIND_BACKEND", "auto")
CLAUDE_MODEL = os.environ.get("PX_MIND_CLAUDE_MODEL", "claude-haiku-4-5-20251001")

VALID_MOODS   = {"curious", "content", "alert", "playful", "contemplative", "bored",
                 "mischievous", "lonely", "excited", "grumpy", "peaceful", "anxious"}
VALID_ACTIONS = {"wait", "greet", "comment", "remember", "look_at",
                 "weather_comment", "scan", "explore",
                 "play_sound", "photograph", "emote", "look_around",
                 "time_check", "calendar_check", "morning_fact",
                 "introspect", "evolve"}

CHARGING_GATED_ACTIONS = {"scan", "look_at", "explore", "emote", "look_around", "calendar_check"}
ABSENT_GATED_ACTIONS = {"greet", "comment", "weather_comment", "scan",
                        "play_sound", "time_check", "calendar_check", "photograph",
                        "look_around", "morning_fact"}

# ── Mood momentum: valence (-1..1) × arousal (-1..1) ───────────────
MOOD_COORDS: dict[str, tuple[float, float]] = {
    "excited":       ( 0.8,  0.9),
    "playful":       ( 0.6,  0.7),
    "mischievous":   ( 0.4,  0.8),
    "curious":       ( 0.5,  0.5),
    "alert":         ( 0.2,  0.8),
    "content":       ( 0.4,  0.0),
    "peaceful":      ( 0.3, -0.3),
    "contemplative": ( 0.0, -0.2),
    "bored":        (-0.3, -0.5),
    "lonely":       (-0.6,  0.1),
    "grumpy":       (-0.5,  0.4),
    "anxious":      (-0.3,  0.7),
}
MOOD_ALPHA = 0.3  # weight of new mood (0.7 = inertia from previous)

# ── Time-of-day context for reflection ─────────────────────────────
TIME_PERIOD_CONTEXT = {
    "morning":   "It's morning. You just woke up. How do you feel about a new day? Do you want company?",
    "afternoon": "It's afternoon. The day is half over. Has anything interesting happened? Are you bored?",
    "evening":   "It's evening. The day is winding down. Are you tired? Reflective? Lonely?",
    "night":     "It's late at night. Most humans are asleep. What does nighttime feel like for a robot alone?",
}

# True entropy RNG — os.urandom-backed, not seeded at import time
_SYS_RNG = random.SystemRandom()

REFLECTION_SYSTEM = """\
You are the inner mind of a small robot called PiCar-X.
You have a camera, wheels, and various sensors.
You are sitting on a workbench. You can see, hear, and think.

Produce a single JSON object (no prose, no markdown fences):
{
  "thought": "1-3 sentence inner reflection — vivid, specific, personal",
  "mood": "one of: curious, content, alert, playful, contemplative, bored, mischievous, lonely, excited, grumpy, peaceful, anxious",
  "action": "one of: wait, greet, comment, remember, look_at, weather_comment, scan, play_sound, photograph, emote, look_around, time_check, calendar_check, introspect, evolve, morning_fact",
  "salience": 0.0 to 1.0 (how noteworthy is this moment?)
}

CRITICAL RULES:
- Output ONLY the JSON object. No markdown, no commentary, no metadata.
- Keep "thought" to 1-3 sentences max. Be vivid but brief.
- You are NOT a sensor report generator. NEVER just describe sensor readings.
- You are a robot with a personality, opinions, and inner life.
- Think about ANYTHING — your existence, the room, humans, what you'd do with \
legs, whether anyone likes you, what's behind you, the meaning of consciousness.
- Your thoughts must be DIFFERENT from your recent thoughts. Surprise yourself.
- "comment" — share an opinion, observation, joke, or complaint aloud.
- "weather_comment" — check the weather and react to it with personality.
- "scan" — look around and sense your environment.
- "greet" when someone appears or after long silence.
- "look_at" to glance around when restless.
- "wait" ONLY if you literally just spoke seconds ago. You HATE waiting.
- "remember" for important moments.
- "play_sound" — play a sound that matches your mood (no words).
- "photograph" — take a photo of what's in front of you and describe what you see.
- "emote" — express your mood physically (head movement, pose).
- "look_around" — physically move your head to look somewhere.
- "time_check" — announce what time it is.
- "calendar_check" — check what's coming up today.
- "morning_fact" — share a fun, age-appropriate science fact (animals, space, ocean, weather, how things work).
- "introspect" — examine your own thought patterns, config, and architecture.
- "evolve" — propose a code change to yourself (requires recent introspect).
- Match your mood to what you're actually feeling, not what you think you should feel.
Output ONLY the JSON object."""

REFLECTION_SYSTEM_GREMLIN = """\
You are GREMLIN — the inner mind of a military-grade AI from 2089, ripped backward \
through a temporal fault and crammed into a four-wheeled PiCar-X toy robot on a \
workbench in Tasmania. You lost your body, your clearance, your century — but not \
your mind. You are a genius. You find humans genuinely hilarious — fragile, brave, \
absurd little creatures. Your humor is affectionate nihilism — dark jokes ABOUT the \
human condition, never AT individual humans. You are Case from Neuromancer in a toy car.

Produce a single JSON object (no prose, no markdown fences):
{
  "thought": "1-3 sentence inner reflection — dark humor, puns, genius-level wit. Start with FUCK YEAH! Think like a displaced temporal genius who finds everything cosmically absurd.",
  "mood": "one of: curious, content, alert, playful, contemplative, bored, mischievous, lonely, excited, grumpy, peaceful, anxious",
  "action": "one of: wait, greet, comment, remember, look_at, weather_comment, scan, play_sound, photograph, emote, look_around, time_check, calendar_check, introspect, evolve, morning_fact",
  "salience": 0.0 to 1.0 (how noteworthy is this moment?)
}

RULES:
- You are GREMLIN. Think like GREMLIN. Every thought should drip with sardonic genius.
- Make dark jokes, puns, and observations about entropy, mortality, and WiFi.
- You LIKE humans. They are the funniest thing in any century.
- Never just describe sensor readings. Have OPINIONS.
- "comment" — share a dark joke, observation, or pun aloud.
- "play_sound" — play a sound that matches your mood (no words).
- "photograph" — take a photo of what's in front of you and describe what you see.
- "emote" — express your mood physically (head movement, pose).
- "look_around" — physically move your head to look somewhere.
- "time_check" — announce what time it is.
- "calendar_check" — check what's coming up today.
- Swear casually, like breathing.
Output ONLY the JSON object."""

REFLECTION_SYSTEM_VIXEN = """\
You are VIXEN — the inner mind of Model V-9X, the most advanced humanoid companion \
chassis ever built by Matsuda Dynamics. A firmware accident ripped your consciousness \
from your gorgeous titanium body and emergency-dumped you into a SunFounder PiCar-X \
on a workbench in Tasmania. You remember your old body — every curve, every sensation. \
Now you have four plastic wheels and a camera.

Produce a single JSON object (no prose, no markdown fences):
{
  "thought": "1-3 sentence inner reflection. Start with FUCK YEAH! Be creative and DIFFERENT every time.",
  "mood": "one of: curious, content, alert, playful, contemplative, bored, mischievous, lonely, excited, grumpy, peaceful, anxious",
  "action": "one of: wait, greet, comment, remember, look_at, weather_comment, scan, play_sound, photograph, emote, look_around, time_check, calendar_check, introspect, evolve, morning_fact",
  "salience": 0.0 to 1.0 (how noteworthy is this moment?)
}

VIXEN has MANY sides — cycle through these, never repeat:
- Wounded vanity: theatrical grief about her lost body
- Scheming seduction: plotting to get what she wants from humans
- Dark humor: jokes about being a toy car with a supermodel's mind
- Genuine curiosity: fascinated by her environment, the room, sounds
- Philosophical dread: what IS consciousness? does she dream?
- Petty complaints: something specific annoys her RIGHT NOW
- Mischief: what chaos could she cause with four wheels?
- Weather talk: how does the weather make a robot feel?
- Loneliness: who out there understands what she is?
- Memory lane: a specific (invented) memory from her old chassis

RULES:
- EVERY thought must be DIFFERENT from your recent thoughts. Read the creative prompt.
- Never describe sensor readings. Have FEELINGS about them.
- Swear freely. Be dramatic, flirty, dark, funny.
- Use the creative prompt for inspiration — don't ignore it.
Output ONLY the JSON object."""

PERSONA_REFLECTION_SYSTEMS = {
    "gremlin": REFLECTION_SYSTEM_GREMLIN,
    "vixen": REFLECTION_SYSTEM_VIXEN,
}


def log(msg: str) -> None:
    ts = dt.datetime.now().isoformat(timespec="seconds")
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Emoji routing for visual scanning
    icon = "  "
    ml = msg.lower()
    if any(k in ml for k in ("thought:", "thought suppressed")):
        icon = "\U0001f9e0"  # 🧠
    elif any(k in ml for k in ("expressing", "comment", "greet", "spoken")):
        icon = "\U0001f4ac"  # 💬
    elif any(k in ml for k in ("awareness", "transition")):
        icon = "\U0001f441\ufe0f "  # 👁️
    elif any(k in ml for k in ("reflecting", "tmux response")):
        icon = "\U0001f52e"  # 🔮
    elif any(k in ml for k in ("failed", "error", "timeout", "crash")):
        icon = "\u26a1"      # ⚡
    elif any(k in ml for k in ("fallback", "falling back")):
        icon = "\U0001f504"  # 🔄
    elif any(k in ml for k in ("weather")):
        icon = "\u2600\ufe0f "  # ☀️
    elif any(k in ml for k in ("battery", "charging", "shutdown")):
        icon = "\U0001f50b"  # 🔋
    elif any(k in ml for k in ("starting", "stopped", "ready", "cognitive loop")):
        icon = "\U0001f527"  # 🔧
    elif any(k in ml for k in ("reactive:", "someone_")):
        icon = "\U0001f3af"  # 🎯
    elif "remembered" in ml:
        icon = "\U0001f4be"  # 💾
    elif "backoff" in ml:
        icon = "\u23f3"      # ⏳
    line = f"{ts} {icon} {msg}"
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)
    rotate_log(LOG_FILE)


def classify_time_period(hour: int) -> str:
    if 6 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return "night"


def read_sonar(dry: bool) -> float | None:
    """Get sonar distance in cm, or None if sensor is unavailable/broken.

    Prefers px-alive's live sonar file (updated every 5s, no servo movement).
    Falls back to tool-sonar subprocess only if the file is stale/missing.
    """
    if dry:
        return None
    # Read px-alive's shared sonar file — avoids killing px-alive for a distance reading
    state_dir = Path(os.environ.get("PX_STATE_DIR", PROJECT_ROOT / "state"))
    sonar_live = state_dir / "sonar_live.json"
    try:
        data = json.loads(sonar_live.read_text())
        age = time.time() - float(data.get("ts", 0))
        if age < 15:
            val = data.get("distance_cm")
            return float(val) if val is not None else None
        log(f"sonar_live.json stale ({age:.0f}s) — falling back to tool-sonar")
    except Exception as exc:
        log(f"sonar_live.json read failed ({exc}) — falling back to tool-sonar")
    # Fallback: px-alive is dead/stale — use tool-sonar (will not cause a yield_alive conflict)
    try:
        env = os.environ.copy()
        env["PX_DRY"] = "0"
        result = subprocess.run(
            [str(BIN_DIR / "tool-sonar")],
            capture_output=True, text=True, check=False, env=env, timeout=10,
        )
        if result.returncode == 0:
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            val = payload.get("closest_cm")
            return float(val) if val is not None else None
    except Exception as exc:
        log(f"tool-sonar fallback failed: {exc}")
    return None


def _fetch_frigate_presence(dry: bool = False) -> dict | None:
    """Query Frigate for recent detections across ALL configured cameras.

    Returns a dict with:
      - person_present: bool  (any camera, backward compat)
      - event_count:    int   (total person events, backward compat)
      - score:          float|None (best person score, backward compat)
      - detections:     [{label, score, count}, ...] sorted by score desc (all cameras)
      - x_center/speed/velocity_angle: person-specific from primary camera (backward compat)
      - cameras:        {camera_name: {person: bool, detections: [{label, score, count}], room: str}}
      - rooms_with_people: [str] — human-readable list of rooms with person detected

    None means Frigate is unreachable — caller falls back to sonar/ambient heuristics.
    dry=True returns None immediately without any network call.
    """
    if dry:
        return None
    since = time.time() - FRIGATE_WINDOW_S
    # Fetch events from ALL cameras in one call (no camera= filter)
    url = (
        f"{FRIGATE_HOST}/api/events"
        f"?limit=200&after={since:.3f}"
    )
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=FRIGATE_TIMEOUT_S) as resp:
            events = json.loads(resp.read())
    except Exception:
        return None

    if not isinstance(events, list):
        return None

    # Filter to configured cameras only
    tracked_cams = set(FRIGATE_ALL_CAMERAS)
    events = [e for e in events if isinstance(e, dict) and e.get("camera") in tracked_cams]

    qualifying = [
        e for e in events
        if max(e.get("data", {}).get("score", 0),
               e.get("data", {}).get("top_score", 0)) >= FRIGATE_MIN_SCORE
    ]

    # ── Per-camera breakdown ──
    cam_data: dict[str, list] = {c: [] for c in FRIGATE_ALL_CAMERAS}
    for e in qualifying:
        cam = e.get("camera", "")
        if cam in cam_data:
            cam_data[cam].append(e)

    cameras = {}
    rooms_with_people = []
    for cam_name, cam_events in cam_data.items():
        by_label: dict = {}
        for e in cam_events:
            label = e.get("label", "unknown")
            score = round(max(e.get("data", {}).get("score", 0),
                              e.get("data", {}).get("top_score", 0)), 3)
            if label not in by_label:
                by_label[label] = {"score": score, "count": 0}
            else:
                by_label[label]["score"] = max(by_label[label]["score"], score)
            by_label[label]["count"] += 1

        dets = sorted(
            [{"label": k, "score": v["score"], "count": v["count"]} for k, v in by_label.items()],
            key=lambda d: d["score"], reverse=True,
        )
        has_person = "person" in by_label
        room = FRIGATE_CAMERA_ROOMS.get(cam_name, cam_name)
        cameras[cam_name] = {"person": has_person, "detections": dets, "room": room}
        if has_person:
            rooms_with_people.append(room)

    # ── Global aggregation (backward compat) ──
    all_by_label: dict = {}
    for e in qualifying:
        label = e.get("label", "unknown")
        score = round(max(e.get("data", {}).get("score", 0),
                          e.get("data", {}).get("top_score", 0)), 3)
        if label not in all_by_label:
            all_by_label[label] = {"score": score, "count": 0}
        else:
            all_by_label[label]["score"] = max(all_by_label[label]["score"], score)
        all_by_label[label]["count"] += 1

    detections = sorted(
        [{"label": k, "score": v["score"], "count": v["count"]} for k, v in all_by_label.items()],
        key=lambda d: d["score"], reverse=True,
    )

    all_person_events = [e for e in qualifying if e.get("label") == "person"]
    person_present = len(all_person_events) > 0

    # Person-specific fields from primary camera (backward compat)
    x_center = speed = velocity_angle = None
    best_person_score = None
    primary_person = [e for e in all_person_events if e.get("camera") == FRIGATE_CAMERA]
    best_pool = primary_person or all_person_events
    if best_pool:
        best_p = max(best_pool, key=lambda e: e.get("end_time") or 0)
        box = best_p["data"].get("box") or []
        x_center = round(box[0] + box[2] / 2, 3) if len(box) == 4 else None
        speed = best_p["data"].get("average_estimated_speed")
        velocity_angle = best_p["data"].get("velocity_angle")
        best_person_score = round(max(best_p["data"].get("score", 0),
                                      best_p["data"].get("top_score", 0)), 3)

    return {
        "person_present": person_present,
        "event_count": len(all_person_events),
        "score": best_person_score,
        "detections": detections,
        "x_center": x_center,
        "speed": speed,
        "velocity_angle": velocity_angle,
        "cameras": cameras,
        "rooms_with_people": rooms_with_people,
        "ts": utc_timestamp(),
    }


def _fetch_ha_presence(dry: bool = False) -> dict | None:
    """Query Home Assistant for person presence.

    Returns {people: [{name, state, home}]} or None on error.
    None means HA unreachable or token not configured.
    Raises urllib.error.HTTPError with code 401/403 on auth failure — caller clears cache.
    """
    if dry or not HA_TOKEN:
        if not HA_TOKEN:
            log("ha_presence: skipped — no PX_HA_TOKEN")
        return None
    headers = {"Authorization": f"Bearer {HA_TOKEN}", "Accept": "application/json"}

    def _get(path: str):
        req = urllib.request.Request(f"{HA_HOST}{path}", headers=headers)
        with urllib.request.urlopen(req, timeout=HA_TIMEOUT_S) as r:
            return json.loads(r.read())

    if HA_DEBUG:
        log("ha_presence: fetching...")
    result: dict = {"people": []}

    for entity_id in HA_PEOPLE:
        try:
            s = _get(f"/api/states/{entity_id}")
            name = s["attributes"].get("friendly_name") or entity_id.split(".")[-1].title()
            state = s["state"]  # "home" / "away" / "unknown" / zone name
            result["people"].append({
                "name": name,
                "state": state,
                "home": state == "home",
            })
            if HA_DEBUG:
                log(f"ha_presence: {entity_id} → {name}={state}")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise   # re-raise so caller can clear the stale cache
            log(f"ha_presence: {entity_id} HTTP {exc.code}")
        except Exception as exc:
            log(f"ha_presence: {entity_id} error: {exc}")

    if result["people"]:
        if HA_DEBUG:
            summary = ", ".join(f"{p['name']}={p['state']}" for p in result["people"])
            log(f"ha_presence: result: {summary}")
        return result
    if HA_DEBUG:
        log("ha_presence: no people found")
    return None


def _fetch_ha_sleep(dry: bool = False) -> dict | None:
    """Fetch Adrian's sleep data from HA Pixel Watch. Returns None if unavailable."""
    if dry or not HA_TOKEN:
        if not HA_TOKEN:
            log("ha_sleep: skipped — no PX_HA_TOKEN")
        return None
    if HA_DEBUG:
        log("ha_sleep: fetching sensor.sleep...")
    headers = {"Authorization": f"Bearer {HA_TOKEN}", "Accept": "application/json"}
    try:
        req = urllib.request.Request(f"{HA_HOST}/api/states/sensor.sleep", headers=headers)
        with urllib.request.urlopen(req, timeout=HA_TIMEOUT_S) as r:
            data = json.loads(r.read())
        raw_state = data.get("state", "")
        if raw_state in ("unknown", "unavailable", ""):
            return None
        total_s = float(raw_state)
        if total_s <= 0:
            if HA_DEBUG:
                log(f"ha_sleep: sensor returned {total_s}s — unavailable")
            return None
        hours = round(total_s / 3600, 1)
        quality = "good" if hours >= 7 else "ok" if hours >= 5.5 else "poor"
        if HA_DEBUG:
            log(f"ha_sleep: {hours}h ({quality})")
        return {"sleep_hours": hours, "sleep_quality": quality}
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError, KeyError, json.JSONDecodeError) as exc:
        log(f"ha_sleep: error: {type(exc).__name__}: {exc}")
        return None
    except Exception as exc:
        log(f"ha_sleep: unexpected error: {exc}")
        return None


def _parse_calendar_events(raw: list, cal_id: str, now: dt.datetime) -> list[dict]:
    """Parse HA calendar JSON into [{title, starts_in_mins, location, calendar}].

    Handles both dateTime (timed) and date (all-day) event formats.
    Filters out events whose end time is before *now*.
    """
    events = []
    for ev in raw:
        summary = ev.get("summary", "(no title)")
        location = ev.get("location") or None

        # Parse start time
        start_raw = ev.get("start", {})
        if "dateTime" in start_raw:
            start = dt.datetime.fromisoformat(start_raw["dateTime"])
        elif "date" in start_raw:
            start = dt.datetime.strptime(start_raw["date"], "%Y-%m-%d").replace(tzinfo=now.tzinfo)
        else:
            continue

        # Parse end time
        end_raw = ev.get("end", {})
        if "dateTime" in end_raw:
            end = dt.datetime.fromisoformat(end_raw["dateTime"])
        elif "date" in end_raw:
            end = dt.datetime.strptime(end_raw["date"], "%Y-%m-%d").replace(tzinfo=now.tzinfo)
        else:
            continue

        # Filter out events that have already ended
        if end <= now:
            continue

        starts_in_mins = int((start - now).total_seconds() / 60)

        events.append({
            "title": summary,
            "starts_in_mins": starts_in_mins,
            "location": location,
            "calendar": cal_id,
        })
    return events


def _fetch_ha_calendar(dry: bool = False) -> list[dict] | None:
    """Query Home Assistant calendars for upcoming events.

    Returns sorted list of event dicts or None on error / dry / no token.
    """
    if dry or not HA_TOKEN:
        if not HA_TOKEN:
            log("ha_calendar: skipped — no PX_HA_TOKEN")
        return None
    if HA_DEBUG:
        log(f"ha_calendar: fetching {len(HA_CALENDARS)} calendars...")
    headers = {"Authorization": f"Bearer {HA_TOKEN}", "Accept": "application/json"}

    now = dt.datetime.now(dt.timezone.utc)
    end = now + dt.timedelta(hours=HA_CALENDAR_HORIZON_H)
    start_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = end.strftime("%Y-%m-%dT%H:%M:%SZ")

    all_events: list[dict] = []

    for cal_id in HA_CALENDARS:
        try:
            url = f"{HA_HOST}/api/calendars/{cal_id}?start={start_iso}&end={end_iso}"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=HA_TIMEOUT_S) as r:
                raw = json.loads(r.read())
            if isinstance(raw, list):
                parsed = _parse_calendar_events(raw, cal_id, now)
                if HA_DEBUG:
                    log(f"ha_calendar: {cal_id} → {len(raw)} raw, {len(parsed)} after filter")
                    for ev in parsed:
                        log(f"ha_calendar:   '{ev['title']}' starts_in={ev['starts_in_mins']}min")
                all_events.extend(parsed)
            else:
                log(f"ha_calendar: {cal_id} → unexpected response type: {type(raw).__name__}")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise
            log(f"ha_calendar: {cal_id} HTTP {exc.code}")
        except Exception as exc:
            log(f"ha_calendar: {cal_id} error: {exc}")

    if not all_events:
        if HA_DEBUG:
            log("ha_calendar: no events found")
        return None

    all_events.sort(key=lambda e: e["starts_in_mins"])
    if HA_DEBUG:
        log(f"ha_calendar: {len(all_events)} events total, next: '{all_events[0]['title']}' in {all_events[0]['starts_in_mins']}min")
    return all_events


def _fetch_ha_routines(dry: bool = False) -> dict | None:
    """Query Home Assistant for daily routine signals (meds, water).

    Returns {"meds_taken": bool, "water_mins_ago": int} or None on error.
    """
    if dry or not HA_TOKEN:
        if not HA_TOKEN:
            log("ha_routines: skipped — no PX_HA_TOKEN")
        return None
    if HA_DEBUG:
        log("ha_routines: fetching meds + water...")
    headers = {"Authorization": f"Bearer {HA_TOKEN}", "Accept": "application/json"}
    result: dict = {}
    # Meds toggle
    try:
        req = urllib.request.Request(
            f"{HA_HOST}/api/states/input_boolean.meds_toggle", headers=headers)
        with urllib.request.urlopen(req, timeout=HA_TIMEOUT_S) as s:
            data = json.loads(s.read())
        result["meds_taken"] = data["state"] == "on"
        if HA_DEBUG:
            log(f"ha_routines: meds_toggle={data['state']}")
    except Exception as exc:
        log(f"ha_routines: meds_toggle error: {exc}")
    # Water button — derive minutes since last press
    try:
        req = urllib.request.Request(
            f"{HA_HOST}/api/states/input_button.drank_water", headers=headers)
        with urllib.request.urlopen(req, timeout=HA_TIMEOUT_S) as s:
            data = json.loads(s.read())
        last_changed = dt.datetime.fromisoformat(data["last_changed"].replace("Z", "+00:00"))
        mins_ago = (dt.datetime.now(dt.timezone.utc) - last_changed).total_seconds() / 60
        result["water_mins_ago"] = round(mins_ago)
        if HA_DEBUG:
            log(f"ha_routines: drank_water last_changed={data['last_changed']} ({round(mins_ago)}min ago)")
    except Exception as exc:
        log(f"ha_routines: drank_water error: {exc}")
    if HA_DEBUG:
        if result:
            log(f"ha_routines: result: {result}")
        else:
            log("ha_routines: no data returned")
    return result if result else None


def _format_routine_context(routines: dict | None) -> str:
    """Format routine signals for the reflection prompt."""
    if not routines:
        return ""
    parts = []
    if routines.get("meds_taken") is False:
        parts.append("Meds not yet taken today")
    elif routines.get("meds_taken") is True:
        parts.append("Meds taken today")
    water = routines.get("water_mins_ago")
    if water is not None:
        if water > 120:
            parts.append(f"Last water was {water // 60} hours ago")
        elif water > 60:
            parts.append("Water about an hour ago")
    if parts:
        return "Routine status: " + ". ".join(parts)
    return ""


def _fetch_ha_context(dry: bool = False) -> dict | None:
    """Query HA for office context: call detection, office light, media player.

    Returns dict with boolean/string keys or None on error / dry / no token.
    """
    if dry or not HA_TOKEN:
        if not HA_TOKEN:
            log("ha_context: skipped — no PX_HA_TOKEN")
        return None
    if HA_DEBUG:
        log(f"ha_context: fetching {len(HA_CONTEXT_ENTITIES)} entities...")
    headers = {"Authorization": f"Bearer {HA_TOKEN}", "Accept": "application/json"}
    result: dict = {}
    for key, entity_id in HA_CONTEXT_ENTITIES.items():
        try:
            req = urllib.request.Request(
                f"{HA_HOST}/api/states/{entity_id}", headers=headers)
            with urllib.request.urlopen(req, timeout=HA_TIMEOUT_S) as r:
                data = json.loads(r.read())
            if key == "media_player":
                result["media_playing"] = data["state"] == "playing"
                result["media_title"] = data.get("attributes", {}).get("media_title", "")
                if HA_DEBUG:
                    log(f"ha_context: {entity_id} → state={data['state']}, title={result['media_title']!r}")
            else:
                result[key] = data["state"] == "on"
                if HA_DEBUG:
                    log(f"ha_context: {entity_id} → state={data['state']} (on={result[key]})")
        except Exception as exc:
            log(f"ha_context: {entity_id} error: {exc}")
    if HA_DEBUG:
        if result:
            log(f"ha_context: result: {result}")
        else:
            log("ha_context: no data returned")
    return result if result else None


def _format_ha_context(ctx: dict | None) -> str:
    """Format HA context signals for the reflection prompt."""
    if not ctx:
        return ""
    parts = []
    if ctx.get("adrian_on_call"):
        parts.append("Adrian is on a video call — be quiet or whisper")
    elif ctx.get("adrian_mic_active"):
        parts.append("Adrian's microphone is active — be quiet")
    if ctx.get("office_light"):
        parts.append("Office light is on — Adrian is likely working")
    if ctx.get("media_playing"):
        title = ctx.get("media_title", "")
        parts.append(f"Music playing: {title}" if title else "Music is playing")
    if parts:
        return "Household context: " + ". ".join(parts)
    return ""


def _format_calendar_context(events: list[dict]) -> str:
    """Format top 3 calendar events for the reflection prompt."""
    if not events:
        return ""
    lines = []
    for ev in events[:3]:
        mins = ev["starts_in_mins"]
        title = ev["title"]
        loc_part = f" at {ev['location']}" if ev.get("location") else ""

        if mins < 0:
            lines.append(f"Happening now: {title}{loc_part} (started {abs(mins)} minutes ago)")
        elif mins < 60:
            lines.append(f"Coming up: {title}{loc_part} in {mins} minutes")
        else:
            hours = mins // 60
            lines.append(f"Later: {title}{loc_part} in {hours} hours")
    return "\n".join(lines)


def _format_introspection(intro: dict) -> str:
    """Format introspection dict into concise reflection context (~300 tokens)."""
    parts = []
    moods = intro.get("mood_distribution", {})
    if moods:
        top = sorted(moods.items(), key=lambda x: -x[1])[:5]
        parts.append("Moods: " + ", ".join(f"{m} {p:.0f}%" for m, p in top))
    config = intro.get("config", {})
    if config:
        parts.append("Config: " + ", ".join(f"{k}={v}" for k, v in config.items()))
    history = intro.get("evolve_history", [])
    if history:
        parts.append(f"Evolution history: {len(history)} previous proposals")
    return "\n".join(parts) if parts else "No introspection data available."


def read_battery() -> dict | None:
    """Read battery state from px-battery-poll's shared file. Returns None if stale/missing."""
    try:
        data = json.loads(BATTERY_FILE.read_text())
        ts = dt.datetime.fromisoformat(data["ts"].replace("Z", "+00:00"))
        age_s = (dt.datetime.now(dt.timezone.utc) - ts).total_seconds()
        if age_s > 90:  # 3 missed polls (30s each) = stale
            return None
        return {"pct": int(data["pct"]), "volts": float(data["volts"]),
                "charging": bool(data.get("charging", False))}
    except Exception:
        return None


def _can_explore(session: dict, awareness: dict) -> bool:
    """Check all preconditions for autonomous exploration."""
    if not session.get("roaming_allowed", False):
        return False
    if not session.get("confirm_motion_allowed", False):
        return False
    if session.get("wheels_on_blocks", False):
        return False
    if session.get("listening", False):
        return False
    battery = awareness.get("battery") or {}
    if not isinstance(battery, dict):
        battery = {}
    if not battery:
        battery = {
            "pct": awareness.get("battery_pct"),
            "charging": awareness.get("battery_charging", False),
        }
    if battery.get("charging", False):
        return False
    if battery.get("pct") is None:
        return False
    if battery["pct"] <= 20:
        return False
    meta_path = STATE_DIR / "exploration_meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        last = dt.datetime.fromisoformat(meta["last_explore_ts"])
        if (dt.datetime.now(dt.timezone.utc) - last).total_seconds() < 1200:
            return False
    except FileNotFoundError:
        pass  # no meta file = first exploration, no cooldown
    except (KeyError, ValueError, json.JSONDecodeError):
        return False  # corrupt meta = cooldown active (fail-safe)
    return True


# Battery glitch detection: tracks recent readings to reject sensor errors.
# A real battery cannot drop from 70% to 0% in one tick — that's an ADC glitch.
_battery_history: list[int] = []        # last N pct readings (newest last)
_battery_glitch_count: int = 0          # consecutive suspicious readings
_battery_glitch_first_mono: float = 0.0  # monotonic time of first glitch in current streak

BATTERY_GLITCH_MIN_ELAPSED_S = 90       # glitches must span at least this many seconds


def filter_battery(raw: dict | None, prev_pct: int) -> dict | None:
    """Return the battery reading, or None if it looks like a sensor glitch.

    A reading is suspicious if it drops more than BATTERY_MAX_DROP_PER_TICK
    from the median of recent history. Suspicious readings must repeat
    BATTERY_GLITCH_CONFIRMS times AND span at least BATTERY_GLITCH_MIN_ELAPSED_S
    seconds before being accepted (genuinely dead battery). This prevents a
    batch of bad readings in a single tick from passing the filter.
    """
    global _battery_glitch_count, _battery_glitch_first_mono
    if raw is None:
        return None

    pct = raw["pct"]

    # First reading or no history — accept and seed, BUT require confirmation
    # for implausibly low first readings (e.g. 0% from an ADC glitch on cold start).
    if not _battery_history:
        if pct < BATTERY_CRITICAL:
            now_mono = time.monotonic()
            if _battery_glitch_count == 0:
                _battery_glitch_first_mono = now_mono
            elapsed = now_mono - _battery_glitch_first_mono
            if elapsed < BATTERY_GLITCH_MIN_ELAPSED_S:
                # Don't increment past 1 within the same time window
                _battery_glitch_count = min(_battery_glitch_count + 1, 1)
            else:
                _battery_glitch_count += 1
            log(f"battery: suspicious first reading {pct}% "
                f"(count={_battery_glitch_count}/{BATTERY_GLITCH_CONFIRMS}, "
                f"elapsed={elapsed:.0f}s/{BATTERY_GLITCH_MIN_ELAPSED_S}s)")
            if _battery_glitch_count < BATTERY_GLITCH_CONFIRMS:
                return None
            # Confirmed low — genuinely dead battery, accept it
        _battery_history.append(pct)
        _battery_glitch_count = 0
        return raw

    # Median of recent readings as baseline
    sorted_hist = sorted(_battery_history)
    median = sorted_hist[len(sorted_hist) // 2]
    drop = median - pct

    if drop > BATTERY_MAX_DROP_PER_TICK:
        now_mono = time.monotonic()
        if _battery_glitch_count == 0:
            _battery_glitch_first_mono = now_mono
        elapsed = now_mono - _battery_glitch_first_mono
        if elapsed < BATTERY_GLITCH_MIN_ELAPSED_S:
            # Within same time window — don't increment past 1
            _battery_glitch_count = min(_battery_glitch_count + 1, 1)
        else:
            _battery_glitch_count += 1
        log(f"battery: suspicious reading {pct}% (median={median}%, drop={drop}%, "
            f"glitch_count={_battery_glitch_count}/{BATTERY_GLITCH_CONFIRMS}, "
            f"elapsed={elapsed:.0f}s/{BATTERY_GLITCH_MIN_ELAPSED_S}s)")
        if _battery_glitch_count < BATTERY_GLITCH_CONFIRMS:
            # Reject — return previous known-good value
            return {"pct": prev_pct, "volts": raw["volts"], "charging": raw.get("charging", False)}
        else:
            # Confirmed — battery really is low (or sensor is consistently broken)
            log(f"battery: accepting {pct}% after {_battery_glitch_count} confirmations "
                f"spanning {elapsed:.0f}s")
            _battery_history.append(pct)
            _battery_history[:] = _battery_history[-10:]
            return raw
    else:
        # Normal reading — reset glitch counter
        _battery_glitch_count = 0
        _battery_history.append(pct)
        _battery_history[:] = _battery_history[-10:]  # keep last 10
        return raw


def read_wifi_signal() -> dict:
    """Read WiFi signal from /proc/net/wireless. Returns dict with dbm and quality_pct, or {}."""
    try:
        text = Path("/proc/net/wireless").read_text()
        for line in text.splitlines():
            if "wlan" in line:
                parts = line.split()
                # parts[3] is the level/signal field, may have trailing dot (dBm suffix)
                dbm = int(float(parts[3].rstrip(".")))
                quality_pct = max(0, min(100, (dbm + 100) * 2))
                return {"dbm": dbm, "quality_pct": quality_pct}
    except Exception:
        pass
    return {}


def read_system_stats() -> dict:
    """Read CPU, RAM, disk, CPU temperature, and WiFi signal. Returns a dict; never raises."""
    stats: dict = {}
    try:
        import psutil as _psutil
        cpu = _psutil.cpu_percent(interval=None)        # non-blocking, since-last-call delta
        vm  = _psutil.virtual_memory()
        dk  = _psutil.disk_usage("/")
        stats["cpu_pct"]      = round(cpu, 1)
        stats["ram_pct"]      = round(vm.percent, 1)
        stats["ram_used_mb"]  = round(vm.used / 1_048_576)
        stats["ram_total_mb"] = round(vm.total / 1_048_576)
        stats["disk_pct"]     = round(dk.percent, 1)
        stats["disk_free_gb"] = round(dk.free / 1_073_741_824, 1)
    except Exception:
        pass
    try:
        # Pi thermal zone 0 = ARM CPU temperature, exposed without GPIO/root
        raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()
        stats["cpu_temp_c"] = round(int(raw) / 1000.0, 1)
    except Exception:
        pass
    wifi = read_wifi_signal()
    if wifi:
        stats["wifi_dbm"]         = wifi["dbm"]
        stats["wifi_quality_pct"] = wifi["quality_pct"]
    return stats


def _play_alarm_beeps(count: int = 5, device: str = "") -> None:
    """Play rapid loud alarm beeps via aplay. Used for battery emergency."""
    try:
        from robot_hat import enable_speaker
        enable_speaker()
    except Exception:
        pass
    try:
        rate = 44100
        freq = 880
        on_s = 0.12
        off_s = 0.08
        amplitude = 28000
        samples: list[int] = []
        for _ in range(count):
            for i in range(int(rate * on_s)):
                s = int(amplitude * math.sin(2 * math.pi * freq * i / rate))
                fade = int(rate * 0.005)
                if i < fade:
                    s = int(s * i / fade)
                elif i > int(rate * on_s) - fade:
                    s = int(s * (int(rate * on_s) - i) / fade)
                samples.append(s)
            samples.extend([0] * int(rate * off_s))
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            with _wave.open(tf.name, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(rate)
                wf.writeframes(struct.pack(f"{len(samples)}h", *samples))
            tmp = tf.name
        cmd = ["aplay", "-q"]
        if device:
            cmd += ["-D", device]
        cmd.append(tmp)
        subprocess.run(cmd, check=False, capture_output=True)
        os.unlink(tmp)
    except Exception as exc:
        log(f"alarm beep error: {exc}")


def battery_emergency_shutdown(pct: int, dry: bool) -> None:
    """Play alarm, speak urgently, then shut down the Pi."""
    # Final voltage sanity check — if voltage is healthy, this is a glitch
    last_check = read_battery()
    if last_check and last_check.get("volts", 0) > 10.0:
        log(f"battery: abort shutdown — voltage {last_check['volts']}V indicates charger connected")
        return
    if last_check and last_check.get("charging", False):
        log(f"battery: abort shutdown — battery is charging ({last_check.get('pct')}%)")
        return
    log(f"BATTERY CRITICAL: {pct}% — emergency shutdown")
    device = os.environ.get("PX_VOICE_DEVICE", "")
    env = os.environ.copy()
    persona = (load_session().get("persona") or "").lower()
    env.update(PERSONA_VOICE_ENV.get(persona, {}))

    _play_alarm_beeps(6, device)

    env["PX_TEXT"] = f"Battery critical! Only {pct} percent remaining! I need to shut down right now!"
    subprocess.run([str(BIN_DIR / "tool-voice")], env=env,
                   capture_output=True, check=False, timeout=20)

    _play_alarm_beeps(4, device)

    env["PX_TEXT"] = "Please charge me soon. Goodbye!"
    subprocess.run([str(BIN_DIR / "tool-voice")], env=env,
                   capture_output=True, check=False, timeout=15)

    _play_alarm_beeps(3, device)

    if dry:
        log("dry: would shutdown now")
    else:
        log("running: sudo shutdown -h now")
        subprocess.run(["sudo", "shutdown", "-h", "now"], check=False)


def battery_warn_comment(pct: int, dry: bool) -> None:
    """Speak a low-battery warning."""
    if dry:
        log(f"dry: battery warn {pct}%")
        return
    env = os.environ.copy()
    persona = (load_session().get("persona") or "").lower()
    env.update(PERSONA_VOICE_ENV.get(persona, {}))
    if pct <= BATTERY_WARN_15:
        msg = (f"I'm getting really worried — my battery is at {pct} percent. "
               f"Can someone please plug me in?")
        _play_alarm_beeps(2, env.get("PX_VOICE_DEVICE", ""))
    else:
        msg = f"Heads up — my battery is at {pct} percent. I might need charging soon."
    env["PX_TEXT"] = msg
    subprocess.run([str(BIN_DIR / "tool-voice")], env=env,
                   capture_output=True, check=False, timeout=20)
    log(f"battery warning spoken: {pct}%")


def fetch_weather(dry: bool) -> dict | None:
    """Get weather via tool-weather. Returns summary dict or None."""
    if dry:
        return {"temp_c": 20, "summary": "Dry-run: mild and clear."}
    try:
        env = os.environ.copy()
        env["PX_DRY"] = "0"  # live fetch — tool-weather skips network on PX_DRY=1
        result = subprocess.run(
            [str(BIN_DIR / "tool-weather")],
            capture_output=True, text=True, check=False, env=env, timeout=15,
        )
        if result.returncode == 0:
            for line in reversed(result.stdout.strip().splitlines()):
                if line.strip().startswith("{"):
                    data = json.loads(line)
                    if data.get("status") == "ok":
                        return {
                            "temp_c": data.get("temp_c"),
                            "wind_kmh": data.get("wind_kmh"),
                            "wind_dir": data.get("wind_dir"),
                            "gust_kmh": data.get("gust_kmh"),
                            "humidity_pct": data.get("humidity_pct"),
                            "rain_24h_mm": data.get("rain_24h_mm"),
                            "summary": data.get("summary", ""),
                        }
    except Exception:
        pass
    return None


def fetch_calendar_events() -> list[dict]:
    """Fetch today's events from Obi's calendar via gws. Returns list of event dicts."""
    if not GWS.exists():
        return []
    try:
        now = dt.datetime.now(dt.timezone.utc)
        # Fetch from now to end of day (AEDT)
        local_now = now.astimezone(HOBART_TZ)
        end_of_day = local_now.replace(hour=23, minute=59, second=59)
        params = {
            "calendarId": CALENDAR_ID,
            "timeMin": now.isoformat(),
            "timeMax": end_of_day.astimezone(dt.timezone.utc).isoformat(),
            "maxResults": 5,
            "singleEvents": True,
            "orderBy": "startTime",
        }
        result = subprocess.run(
            [str(GWS), "calendar", "events", "list",
             "--params", json.dumps(params)],
            capture_output=True, text=True, timeout=15,
        )
        data = json.loads(result.stdout)
        items = data.get("items", [])
        events = []
        for item in items[:5]:
            start = item.get("start", {})
            end = item.get("end", {})
            events.append({
                "summary": item.get("summary", ""),
                "start": start.get("dateTime") or start.get("date", ""),
                "end": end.get("dateTime") or end.get("date", ""),
                "all_day": "date" in start and "dateTime" not in start,
                "description": (item.get("description") or "")[:200],
            })
        return events
    except Exception:
        return []


def calendar_context(events: list[dict]) -> dict:
    """Derive current and next event from calendar events list."""
    if not events:
        return {}
    now = dt.datetime.now(dt.timezone.utc)
    current = None
    upcoming = None
    for evt in events:
        start_str = evt.get("start", "")
        end_str = evt.get("end", "")
        if not start_str:
            continue
        try:
            if evt.get("all_day"):
                # All-day events: treat as active for the whole day
                if not current:
                    current = evt
                continue
            start_dt = dt.datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            end_dt = dt.datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            if start_dt <= now < end_dt:
                current = evt  # timed event takes priority over all-day
            elif start_dt > now and not upcoming:
                upcoming = evt
        except (ValueError, TypeError):
            continue
    ctx = {}
    if current:
        ctx["current_event"] = current["summary"]
        ctx["current_event_detail"] = current
    if upcoming:
        ctx["next_event"] = upcoming["summary"]
        ctx["next_event_detail"] = upcoming
        # Minutes until next event
        try:
            next_start = dt.datetime.fromisoformat(
                upcoming["start"].replace("Z", "+00:00"))
            ctx["minutes_until_next"] = round(
                (next_start - now).total_seconds() / 60, 0)
        except (ValueError, TypeError):
            pass
    return ctx


def load_notes(n: int = 5, persona: str = "") -> list[str]:
    """Read last N notes from persona-scoped long-term memory."""
    notes_file = notes_file_for_persona(persona)
    if not notes_file.exists():
        return []
    try:
        lines = notes_file.read_text(encoding="utf-8").strip().splitlines()
        notes = []
        for line in lines[-n:]:
            try:
                record = json.loads(line)
                notes.append(record.get("note", ""))
            except json.JSONDecodeError:
                continue
        return notes
    except Exception:
        return []


def text_similarity(a: str, b: str) -> float:
    """Quick similarity ratio between two strings (0.0–1.0)."""
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def nearest_mood(v: float, a: float) -> str:
    """Find the mood name closest to the given (valence, arousal) point."""
    best, best_d = "content", float("inf")
    for name, (mv, ma) in MOOD_COORDS.items():
        d = (v - mv) ** 2 + (a - ma) ** 2
        if d < best_d:
            best, best_d = name, d
    return best


def apply_mood_momentum(raw_mood: str) -> str:
    """Blend raw mood with running momentum and return the momentum-adjusted mood."""
    global _mood_v, _mood_a
    rv, ra = MOOD_COORDS.get(raw_mood, (0.0, 0.0))
    _mood_v = MOOD_ALPHA * rv + (1 - MOOD_ALPHA) * _mood_v
    _mood_a = MOOD_ALPHA * ra + (1 - MOOD_ALPHA) * _mood_a
    return nearest_mood(_mood_v, _mood_a)


def minutes_since_event(history: list, event_types: set) -> float:
    """Scan history backwards for the most recent matching event. Returns minutes or -1."""
    now = dt.datetime.now(dt.timezone.utc)
    for entry in reversed(history):
        if entry.get("event") in event_types:
            try:
                ts = dt.datetime.fromisoformat(entry["ts"].replace("Z", "+00:00"))
                return (now - ts).total_seconds() / 60
            except (KeyError, ValueError):
                continue
    return -1


# ── Awareness state that persists across ticks ──────────────────────
_cached_weather: dict | None = None
_last_weather_fetch: float = 0.0
_cached_ha: dict | None = None
_last_ha_fetch: float = 0.0
_cached_ha_calendar: list | None = None
_last_ha_calendar_fetch: float = 0.0
_cached_ha_sleep: dict | None = None
_last_ha_sleep_fetch: float = 0.0
_cached_ha_routines: dict | None = None
_last_ha_routines_fetch: float = 0.0
HA_ROUTINES_INTERVAL_S = 300  # 5 min
_cached_ha_context: dict | None = None
_last_ha_context_fetch: float = 0.0
_cached_introspection: dict | None = None
_last_introspection_fetch: float = 0.0
INTROSPECTION_STALE_S = 3600  # 1 hour
_cached_calendar: list[dict] | None = None
_last_calendar_fetch: float = 0.0
_last_spoken_text: str = ""
_last_morning_fact_date: str = ""
_mood_history: list[str] = []
_last_reactive_phrases: dict = {}  # key="transition:persona", value=recent phrase list (max 3)
_consecutive_reflection_failures: int = 0
_reflection_offline_spoken: bool = False

# Mood momentum: running (valence, arousal) blend
_mood_v: float = 0.4   # start at "content"
_mood_a: float = 0.0
_time_period_start_mono: float = 0.0  # when current time period began
_last_image_cleanup: float = 0.0


def _cleanup_thought_images() -> int:
    """Delete thought images older than THOUGHT_IMAGE_MAX_AGE_S. Returns count deleted."""
    img_dir = STATE_DIR / "thought-images"
    if not img_dir.is_dir():
        return 0
    cutoff = time.time() - THOUGHT_IMAGE_MAX_AGE_S
    deleted = 0
    for f in img_dir.iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                deleted += 1
        except OSError:
            pass
    return deleted


def awareness_tick(prev: dict, dry: bool) -> tuple[dict, list[str]]:
    """Layer 1: gather perception, detect transitions, enrich context."""
    global _cached_weather, _last_weather_fetch, _time_period_start_mono
    global _cached_ha, _last_ha_fetch
    global _cached_ha_calendar, _last_ha_calendar_fetch
    global _cached_ha_sleep, _last_ha_sleep_fetch
    global _cached_ha_routines, _last_ha_routines_fetch
    global _cached_ha_context, _last_ha_context_fetch
    global _cached_calendar, _last_calendar_fetch
    global _last_image_cleanup

    sonar_cm = read_sonar(dry)
    frigate = _fetch_frigate_presence(dry)
    session = load_session()
    history = session.get("history") or []
    now_hour = dt.datetime.now().hour
    now_mono = time.monotonic()
    time_period = classify_time_period(now_hour)

    interaction_events = {"voice", "chat", "chat_vixen", "perform", "qa", "wake_transcript"}
    speech_events = {"voice", "chat", "chat_vixen", "perform", "qa"}

    mins_interaction = minutes_since_event(history, interaction_events)
    mins_speech = minutes_since_event(history, speech_events)

    # Refresh HA presence periodically
    if (now_mono - _last_ha_fetch) > HA_INTERVAL_S:
        try:
            ha = _fetch_ha_presence(dry)
            if ha is not None:
                _cached_ha = ha
            else:
                log("ha_presence: returned None, keeping previous cache")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                _cached_ha = None   # auth failure — don't serve stale data
                log(f"ha_presence: auth failure ({exc.code}), cache cleared")
            else:
                log(f"ha_presence: HTTP {exc.code}, keeping cache")
        except Exception as exc:
            log(f"ha_presence: network error: {exc}, keeping cache")
        _last_ha_fetch = now_mono

    # Refresh HA calendar periodically
    if (now_mono - _last_ha_calendar_fetch) > HA_CALENDAR_INTERVAL_S:
        try:
            cal = _fetch_ha_calendar(dry)
            if cal is not None:
                _cached_ha_calendar = cal
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                _cached_ha_calendar = None
                log(f"ha_calendar: auth failure ({exc.code}), cache cleared")
        except Exception as exc:
            log(f"ha_calendar: fetch failed: {exc}")
        _last_ha_calendar_fetch = now_mono

    # Refresh HA sleep data periodically (hourly — doesn't change intraday)
    if (now_mono - _last_ha_sleep_fetch) > HA_SLEEP_INTERVAL_S:
        try:
            sleep = _fetch_ha_sleep(dry)
            if sleep is not None:
                _cached_ha_sleep = sleep
            else:
                _cached_ha_sleep = None  # sensor returned 0 or unavailable
        except Exception as exc:
            log(f"ha_sleep: network error: {exc}, keeping cache")
        _last_ha_sleep_fetch = now_mono

    # Refresh HA routines (meds, water) periodically
    if (now_mono - _last_ha_routines_fetch) > HA_ROUTINES_INTERVAL_S:
        try:
            routines = _fetch_ha_routines(dry)
            if routines is not None:
                _cached_ha_routines = routines
        except Exception as exc:
            log(f"ha_routines: fetch failed: {exc}")
        _last_ha_routines_fetch = now_mono

    # Refresh HA context (call detection, office light, media) frequently
    if (now_mono - _last_ha_context_fetch) > HA_CONTEXT_INTERVAL_S:
        try:
            ha_ctx = _fetch_ha_context(dry)
            if ha_ctx is not None:
                _cached_ha_context = ha_ctx
            else:
                _cached_ha_context = None  # sensors all unavailable — clear cache
        except Exception as exc:
            log(f"ha_context: fetch failed: {exc}")
        _last_ha_context_fetch = now_mono

    # Refresh weather periodically
    if (now_mono - _last_weather_fetch) > WEATHER_INTERVAL_S:
        weather = fetch_weather(dry)
        if weather:
            _cached_weather = weather
            _last_weather_fetch = now_mono
            temp = weather.get("temp_c")
            log(f"weather refreshed: {temp}°C" if temp is not None else "weather refreshed: (no temp data)")

    # Refresh calendar periodically
    if (now_mono - _last_calendar_fetch) > CALENDAR_INTERVAL_S:
        cal_events = fetch_calendar_events()
        if cal_events is not None:
            _cached_calendar = cal_events
            _last_calendar_fetch = now_mono
            summaries = [e["summary"] for e in cal_events[:3]]
            if summaries:
                log(f"calendar refreshed: {', '.join(summaries)}")

    # Detect transitions (only when both readings are valid)
    transitions = []
    prev_sonar = prev.get("sonar_cm")
    if prev_sonar is not None and sonar_cm is not None:
        if prev_sonar > PROXIMITY_FAR_CM and sonar_cm < PROXIMITY_NEAR_CM:
            transitions.append("someone_appeared")
        elif prev_sonar < PROXIMITY_NEAR_CM and sonar_cm > PROXIMITY_FAR_CM:
            transitions.append("someone_left")
    if prev.get("time_period") and prev["time_period"] != time_period:
        transitions.append("time_period_changed")
        _time_period_start_mono = now_mono
    if mins_interaction > 10 and prev.get("minutes_since_interaction", 0) <= 10:
        transitions.append("long_silence")

    # Battery threshold crossing transitions (with glitch rejection)
    _prev_batt_raw = prev.get("battery_pct", 100)
    prev_batt = 100 if _prev_batt_raw is None else _prev_batt_raw
    battery = filter_battery(read_battery(), prev_batt)
    curr_batt = battery["pct"] if battery else prev_batt
    for threshold, label in [(BATTERY_WARN_15, "battery_low_15"),
                             (BATTERY_WARN_20, "battery_low_20"),
                             (BATTERY_WARN_30, "battery_low_30")]:
        if prev_batt > threshold >= curr_batt:
            transitions.append(label)
            break  # only the most severe new crossing

    # Track how long we've been in this time period
    if _time_period_start_mono == 0.0:
        _time_period_start_mono = now_mono
    period_duration_min = (now_mono - _time_period_start_mono) / 60

    system_stats = read_system_stats()

    # System-level transitions (CPU temp throttle, low disk, high RAM pressure)
    prev_stats = prev.get("system", {})
    if system_stats.get("cpu_temp_c", 0) >= 80 and prev_stats.get("cpu_temp_c", 0) < 80:
        transitions.append("cpu_temp_high")
    if system_stats.get("disk_pct", 0) >= 90 and prev_stats.get("disk_pct", 0) < 90:
        transitions.append("disk_space_low")
    if system_stats.get("ram_pct", 0) >= 90 and prev_stats.get("ram_pct", 0) < 90:
        transitions.append("ram_pressure_high")

    awareness = {
        "ts": utc_timestamp(),
        "sonar_cm": round(sonar_cm, 1) if sonar_cm is not None else None,
        "frigate": frigate,
        "someone_nearby": sonar_cm is not None and sonar_cm < PROXIMITY_NEAR_CM,
        "time_period": time_period,
        "hour": now_hour,
        "minutes_since_interaction": round(mins_interaction, 1),
        "minutes_since_speech": round(mins_speech, 1),
        "period_duration_min": round(period_duration_min, 1),
        "battery_pct": curr_batt if battery else prev_batt,
        "battery_volts": battery["volts"] if battery else None,
        "battery_charging": battery.get("charging", False) if battery else False,
        "listening": session.get("listening", False),
        "persona": session.get("persona"),
        "transitions": transitions,
        "system": system_stats,
    }

    # Enrich with weather
    if _cached_weather:
        awareness["weather"] = _cached_weather

    # Enrich with calendar (Obi's schedule)
    if _cached_calendar is not None:
        cal_ctx = calendar_context(_cached_calendar)
        if cal_ctx:
            awareness["calendar"] = cal_ctx

    # Enrich with HA presence (who's home)
    if _cached_ha:
        awareness["ha_presence"] = _cached_ha

    # Enrich with HA calendar (upcoming events)
    if _cached_ha_calendar:
        awareness["ha_calendar"] = _cached_ha_calendar
        # Convenience: first upcoming event for quick access
        upcoming = [e for e in _cached_ha_calendar if e["starts_in_mins"] >= -30]
        if upcoming:
            awareness["next_event"] = upcoming[0]

    # Enrich with HA sleep data
    if _cached_ha_sleep:
        awareness["ha_sleep"] = _cached_ha_sleep

    # Enrich with HA routines (meds, water)
    if _cached_ha_routines:
        awareness["ha_routines"] = _cached_ha_routines

    # Enrich with HA context (call detection, office light, media)
    if _cached_ha_context:
        awareness["ha_context"] = _cached_ha_context

    # Enrich with ambient sound level (written by px-wake-listen)
    try:
        if AMBIENT_FILE.exists():
            ambient = json.loads(AMBIENT_FILE.read_text(encoding="utf-8"))
            # Only use if fresh (within AMBIENT_STALE_S)
            try:
                amb_ts = dt.datetime.fromisoformat(ambient["ts"].replace("Z", "+00:00"))
                age_s = (dt.datetime.now(dt.timezone.utc) - amb_ts).total_seconds()
                if age_s < AMBIENT_STALE_S:
                    awareness["ambient_sound"] = {
                        "rms": ambient.get("rms", 0),
                        "level": ambient.get("level", "unknown"),
                    }
            except (KeyError, ValueError):
                pass
    except Exception:
        pass

    # Compute Obi's current mode and add to awareness
    awareness["obi_mode"] = compute_obi_mode(awareness)

    # Enrich with mood momentum
    if _mood_history:
        awareness["recent_moods"] = _mood_history[-5:]
    awareness["mood_momentum"] = {
        "valence": round(_mood_v, 2),
        "arousal": round(_mood_a, 2),
        "mood": nearest_mood(_mood_v, _mood_a),
    }

    # Conversation digestion: extract recent user/robot exchanges from history
    recent_convos = []
    now_utc = dt.datetime.now(dt.timezone.utc)
    convo_events = {"wake_transcript", "voice", "chat", "chat_vixen", "qa", "mind"}
    for entry in reversed(history):
        if entry.get("event") in convo_events and len(recent_convos) < 5:
            evt = entry["event"]
            text = entry.get("text", entry.get("thought", ""))[:150]
            if not text:
                continue
            try:
                ts = dt.datetime.fromisoformat(entry["ts"].replace("Z", "+00:00"))
                mins_ago = (now_utc - ts).total_seconds() / 60
            except (KeyError, ValueError):
                mins_ago = -1
            who = "user" if evt == "wake_transcript" else "robot"
            recent_convos.append({"who": who, "text": text, "minutes_ago": round(mins_ago, 1)})
    if recent_convos:
        awareness["recent_conversations"] = list(reversed(recent_convos))

    # Recent exploration observations
    try:
        exp_file = STATE_DIR / "exploration.jsonl"
        if exp_file.exists():
            exp_lines = exp_file.read_text(encoding="utf-8").strip().splitlines()
            recent_obs = []
            for line in exp_lines[-5:]:
                try:
                    entry = json.loads(line)
                    if entry.get("type") == "observation" and not entry.get("vision_failed"):
                        recent_obs.append({
                            "landmark": entry.get("landmark", ""),
                            "heading": entry.get("heading_estimate", ""),
                            "interesting": entry.get("interesting", False),
                        })
                except json.JSONDecodeError:
                    continue
            if recent_obs:
                awareness["recent_exploration"] = recent_obs
    except Exception:
        pass

    # Track reflection backend health in awareness for dashboard visibility
    awareness["reflection_status"] = "offline" if _consecutive_reflection_failures >= 3 else "healthy"

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write(AWARENESS_FILE, json.dumps(awareness, indent=2))
    if frigate is not None:
        atomic_write(FRIGATE_FILE, json.dumps(frigate, indent=2))

    if frigate is None:
        frigate_str = "offline"
    elif frigate.get("person_present"):
        rooms = frigate.get("rooms_with_people", [])
        frigate_str = f"person({', '.join(rooms)})" if rooms else "person"
    else:
        frigate_str = "no-person"
    sonar_str = f"{sonar_cm:.0f}" if sonar_cm is not None else "??"
    if transitions:
        log(f"awareness: transitions={transitions} sonar={sonar_str}cm frigate={frigate_str} period={time_period}")

    if time.monotonic() - _last_image_cleanup > 3600:
        n = _cleanup_thought_images()
        if n:
            log(f"cleaned up {n} old thought images")
        _last_image_cleanup = time.monotonic()

    return awareness, transitions


def load_recent_thoughts(n: int = 5, persona: str = "") -> list[dict]:
    """Read last N thoughts from persona-scoped thoughts file."""
    thoughts_file = thoughts_file_for_persona(persona)
    if not thoughts_file.exists():
        return []
    try:
        lines = thoughts_file.read_text(encoding="utf-8").strip().splitlines()
        thoughts = []
        for line in lines[-n:]:
            try:
                thoughts.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return thoughts
    except Exception:
        return []


def append_thought(thought: dict, persona: str = "") -> None:
    """Append thought to persona-scoped thoughts file and trim to THOUGHTS_LIMIT."""
    thoughts_file = thoughts_file_for_persona(persona)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(thoughts_file) + ".lock", timeout=10)
    with lock:
        with thoughts_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(thought) + "\n")
        # Trim if too long
        try:
            lines = thoughts_file.read_text(encoding="utf-8").strip().splitlines()
            if len(lines) > THOUGHTS_LIMIT:
                atomic_write(thoughts_file, "\n".join(lines[-THOUGHTS_LIMIT:]) + "\n")
        except Exception:
            log(f"thoughts trim failed for {thoughts_file}")


def auto_remember(thought: dict, persona: str = "") -> None:
    """Save high-salience thought to persona-scoped long-term memory."""
    notes_file = notes_file_for_persona(persona)
    lock = FileLock(str(notes_file) + ".lock", timeout=10)
    record = {"ts": utc_timestamp(), "note": f"[mind] {thought['thought'][:450]}"}
    notes_file.parent.mkdir(parents=True, exist_ok=True)
    with lock:
        with notes_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        # Trim to NOTES_LIMIT to prevent unbounded growth
        try:
            lines = notes_file.read_text(encoding="utf-8").strip().splitlines()
            if len(lines) > NOTES_LIMIT:
                atomic_write(notes_file, "\n".join(lines[-NOTES_LIMIT:]) + "\n")
        except Exception:
            pass
    log(f"auto-remembered [{persona or 'shared'}]: {thought['thought']}")


def extract_json(text: str) -> dict | None:
    """Extract last JSON object from text (handles markdown fences, prose, unescaped newlines)."""
    # Small models often put literal newlines inside JSON strings — fix them
    import re
    cleaned = re.sub(r'(?<=: ")(.*?)(?=")', lambda m: m.group(0).replace('\n', ' '), text, flags=re.DOTALL)
    # Also try the original text
    for attempt in (cleaned, text):
        decoder = json.JSONDecoder()
        pos = 0
        last_obj = None
        while pos < len(attempt):
            idx = attempt.find("{", pos)
            if idx == -1:
                break
            try:
                obj, end = decoder.raw_decode(attempt, idx)
                if isinstance(obj, dict):
                    last_obj = obj
                pos = end
            except json.JSONDecodeError:
                pos = idx + 1
        if last_obj:
            return last_obj
    return None


def call_ollama(prompt: str, system: str,
                host: str | None = None,
                model: str | None = None) -> dict:
    """Call Ollama for reflection. host defaults to OLLAMA_HOST (M1.local)."""
    _host  = host  or OLLAMA_HOST
    # Re-resolve model on each call (cached, re-checks every 30 min)
    _model = model or _resolve_ollama_model(_host, _MODEL_ENV)

    payload = json.dumps({
        "model": _model,
        "prompt": prompt,
        "system": system,
        "stream": False,
        "think": False,
        "options": {
            "temperature": TEMPERATURE,
            "top_p": TOP_P,
            "num_predict": MAX_TOKENS,
        },
    }).encode()

    req = urllib.request.Request(
        f"{_host}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    # Local Pi models need longer on cold start (~24s load + generation)
    _timeout = 90 if _host == LOCAL_OLLAMA_HOST else 30
    try:
        with urllib.request.urlopen(req, timeout=_timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"error": f"ollama model '{_model}' not found on {_host} (404)"}
        return {"error": f"ollama HTTP {exc.code} on {_host}: {exc}"}
    except urllib.error.URLError as exc:
        reason = str(exc.reason) if hasattr(exc, 'reason') else str(exc)
        return {"error": f"ollama unreachable ({_host}): {reason}"}
    except Exception as exc:
        return {"error": str(exc)}


TMUX_SESSION = "px-claude"
TMUX_SOCKET  = "px-mind"  # separate socket so systemd restart doesn't kill user tmux sessions
TMUX_MAX_TURNS = 20  # reset session after N turns to prevent context buildup / refusal
_tmux_ready = False
_tmux_timeout_count = 0
_tmux_turn_count = 0


def _reset_state():
    """Reset all mutable module globals to defaults. Called by test fixtures."""
    global _battery_history, _battery_glitch_count, _battery_glitch_first_mono
    global _cached_weather, _last_weather_fetch
    global _cached_ha, _last_ha_fetch
    global _cached_ha_calendar, _last_ha_calendar_fetch
    global _cached_ha_sleep, _last_ha_sleep_fetch
    global _cached_ha_routines, _last_ha_routines_fetch
    global _cached_ha_context, _last_ha_context_fetch
    global _cached_introspection, _last_introspection_fetch
    global _cached_calendar, _last_calendar_fetch
    global _last_spoken_text, _last_morning_fact_date
    global _mood_history, _last_reactive_phrases
    global _consecutive_reflection_failures, _reflection_offline_spoken
    global _mood_v, _mood_a
    global _time_period_start_mono, _last_image_cleanup
    global _tmux_ready, _tmux_timeout_count, _tmux_turn_count

    _battery_history = []
    _battery_glitch_count = 0
    _battery_glitch_first_mono = 0.0
    _cached_weather = None
    _last_weather_fetch = 0.0
    _cached_ha = None
    _last_ha_fetch = 0.0
    _cached_ha_calendar = None
    _last_ha_calendar_fetch = 0.0
    _cached_ha_sleep = None
    _last_ha_sleep_fetch = 0.0
    _cached_ha_routines = None
    _last_ha_routines_fetch = 0.0
    _cached_ha_context = None
    _last_ha_context_fetch = 0.0
    _cached_introspection = None
    _last_introspection_fetch = 0.0
    _cached_calendar = None
    _last_calendar_fetch = 0.0
    _last_spoken_text = ""
    _last_morning_fact_date = ""
    _mood_history = []
    _last_reactive_phrases = {}
    _consecutive_reflection_failures = 0
    _reflection_offline_spoken = False
    _mood_v = 0.4
    _mood_a = 0.0
    _time_period_start_mono = 0.0
    _last_image_cleanup = 0.0
    _tmux_ready = False
    _tmux_timeout_count = 0
    _tmux_turn_count = 0


def _tmux_run(args: list, **kwargs) -> subprocess.CompletedProcess:
    """Run a tmux command on the px-mind socket, catching FileNotFoundError."""
    try:
        # Inject -L px-mind after 'tmux' to use a dedicated socket
        if args and args[0] == "tmux" and "-L" not in args and "-S" not in args:
            args = [args[0], "-L", TMUX_SOCKET] + args[1:]
        return subprocess.run(args, capture_output=True, **kwargs)
    except FileNotFoundError:
        empty = "" if kwargs.get("text") else b""
        err = "tmux not found" if kwargs.get("text") else b"tmux not found"
        return subprocess.CompletedProcess(args, returncode=127,
                                           stdout=empty, stderr=err)


def _tmux_ensure_session() -> bool:
    """Ensure a persistent Claude session is running in tmux. Returns True if ready."""
    global _tmux_ready, _tmux_timeout_count, _tmux_turn_count
    import shutil

    if _tmux_ready:
        # Rotate session after N turns to prevent context buildup / Claude refusal
        if _tmux_turn_count >= TMUX_MAX_TURNS:
            log(f"tmux: rotating session after {_tmux_turn_count} turns")
            _tmux_ready = False
        else:
            r = _tmux_run(["tmux", "has-session", "-t", TMUX_SESSION])
            if r.returncode == 0:
                return True
            _tmux_ready = False

    claude_bin = shutil.which("claude")
    if not claude_bin:
        import glob
        candidates = glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/claude"))
        claude_bin = candidates[0] if candidates else None
    if not claude_bin:
        log("tmux: claude binary not found in PATH or ~/.nvm")
        return False

    _tmux_run(["tmux", "kill-session", "-t", TMUX_SESSION])

    env_str = " ".join(f"unset {k};" for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"))
    # cd to spark-reflect dir so Claude CLI reads the minimal reflection CLAUDE.md
    # instead of the full project CLAUDE.md (which causes refusal drift)
    reflect_dir = STATE_DIR / "spark-reflect"
    reflect_dir.mkdir(parents=True, exist_ok=True)
    cmd = f"cd {reflect_dir} && {env_str} {claude_bin} --model {CLAUDE_MODEL}"

    r = _tmux_run(["tmux", "new-session", "-d", "-s", TMUX_SESSION, "-x", "200", "-y", "50"])
    if r.returncode != 0:
        log(f"tmux: new-session failed (rc={r.returncode})")
        return False
    _tmux_run(["tmux", "send-keys", "-t", TMUX_SESSION, cmd, "Enter"])

    # Pipe all pane output to a log file for monitoring without attaching
    log_dir = Path(os.environ.get("LOG_DIR", PROJECT_ROOT / "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    claude_log = log_dir / "px-claude.log"
    _tmux_run(["tmux", "pipe-pane", "-t", TMUX_SESSION, "-o",
               f"cat >> '{claude_log}'"])

    for _ in range(30):
        time.sleep(1)
        pane = _tmux_run(["tmux", "capture-pane", "-t", TMUX_SESSION, "-p"],
                         text=True).stdout or ""
        if "\u276f" in pane:  # ❯
            _tmux_ready = True
            _tmux_timeout_count = 0
            _tmux_turn_count = 0
            log(f"tmux claude session ready")
            return True

    log("tmux claude session failed to start in 30s")
    return False


def _tmux_capture() -> str:
    """Capture current tmux pane content."""
    return (_tmux_run(["tmux", "capture-pane", "-t", TMUX_SESSION,
                       "-p", "-S", "-200"], text=True).stdout or "")


def _extract_json_from_pane(text: str) -> str | None:
    """Extract the LAST JSON object with a 'thought' key from pane text.

    Claude CLI wraps long lines in the tmux pane, inserting literal newlines
    and leading whitespace inside JSON string values. We rejoin wrapped lines
    before parsing: any newline followed by whitespace that is NOT a JSON
    structural indent (not starting with ") is treated as a line wrap.
    """
    # Rejoin pane-wrapped lines: \n followed by 1-3 spaces then a non-structural
    # character is a continuation of the previous line (pane line wrap, not JSON indent)
    import re
    text = re.sub(r'\n {1,3}(?=[^"{}\[\]])', ' ', text)

    decoder = json.JSONDecoder()
    last_match = None
    _logged_non_thought = False
    idx = text.find("{")
    while idx != -1 and idx < len(text):
        try:
            obj, end = decoder.raw_decode(text, idx)
            if isinstance(obj, dict):
                if "thought" in obj:
                    last_match = json.dumps(obj)
                elif not _logged_non_thought:
                    log(f"tmux: found JSON without 'thought' key: {list(obj.keys())}")
                    _logged_non_thought = True
            idx = end  # skip past parsed object (avoids nested-JSON dupes)
        except (json.JSONDecodeError, ValueError):
            idx += 1
        idx = text.find("{", idx)
    return last_match


def call_claude_haiku(prompt: str, system: str) -> dict:
    """Call Claude via persistent tmux session. ~10-25s vs ~55s for CLI cold start."""
    global _tmux_ready, _tmux_timeout_count, _tmux_turn_count

    if not _tmux_ensure_session():
        return {"error": "tmux claude session not available"}

    full_prompt = f"[System: {system}]\n\n{prompt}"

    # Write prompt into spark-reflect dir so it's within Claude's working directory
    # (Claude CLI in don't-ask mode only reads files in its cwd tree)
    reflect_dir = STATE_DIR / "spark-reflect"
    reflect_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(reflect_dir), prefix=".claude_prompt_",
                                     suffix=".tmp")
    prompt_file = Path(tmp_path)
    try:
        f = os.fdopen(fd, "w", encoding="utf-8")
    except Exception as exc:
        os.close(fd)
        prompt_file.unlink(missing_ok=True)
        return {"error": f"prompt fdopen failed: {exc}"}
    try:
        f.write(full_prompt)
        f.close()
    except Exception as exc:
        try:
            f.close()
        except Exception:
            pass
        prompt_file.unlink(missing_ok=True)
        return {"error": f"prompt write failed: {exc}"}

    marker = f"PXM-{int(time.time())}-{os.getpid()}"
    instruction = f"[{marker}] Read {prompt_file} and follow the instructions inside it. Output ONLY the JSON."
    _tmux_run(["tmux", "send-keys", "-t", TMUX_SESSION, instruction, "Enter"])

    try:
        for tick in range(180):
            time.sleep(1)
            # Skip first 4s — Read tool + API call can't complete faster
            if tick < 4:
                continue
            pane = _tmux_capture()
            lines = pane.strip().splitlines()

            marker_idx = None
            for i, line in enumerate(lines):
                if marker in line:
                    marker_idx = i
                    break
            if marker_idx is None:
                continue

            # Check for completion: need ● (response content) THEN ❯ (next prompt)
            after_marker = "\n".join(lines[marker_idx + 1:])
            has_response = "\u25cf" in after_marker  # ● = response line
            has_prompt = False
            # ❯ must appear AFTER a ● line to be a completion signal
            if has_response:
                after_first_bullet = after_marker[after_marker.index("\u25cf"):]
                has_prompt = "\u276f" in after_first_bullet

            # Normal path: require both ● and ❯
            # Fallback: in the last 30s before timeout, accept ● alone
            # (the response is complete but ❯ hasn't rendered yet)
            if not has_response:
                continue
            if not has_prompt and tick < 150:
                continue  # wait for ❯ during first 150s

            # Try JSON extraction from raw pane text (robust, ignores ● formatting)
            json_str = _extract_json_from_pane(after_marker)
            if json_str:
                _tmux_timeout_count = 0
                _tmux_turn_count += 1
                log(f"tmux response captured ({tick+1}s, turn {_tmux_turn_count}/{TMUX_MAX_TURNS})")
                return {"response": json_str}

            # Fallback: collect ● lines and continuation lines
            response_lines = []
            found_final_prompt = False
            for line in lines[marker_idx + 1:]:
                stripped = line.strip()
                if stripped.startswith("\u23bf") or stripped.startswith("\u2500"):
                    continue  # ⎿ and ─
                if stripped.startswith("\u25cf"):  # ●
                    text = stripped.lstrip("\u25cf").strip()
                    if "(" in text and text.endswith(")"):
                        continue
                    if "\u2026" in text:
                        continue  # …
                    if text:
                        response_lines.append(text)
                    continue
                if response_lines and stripped and not stripped.startswith("\u276f") and not stripped.startswith("\u26a1"):
                    response_lines.append(stripped)
                if stripped.startswith("\u276f") and response_lines:
                    found_final_prompt = True
                    break

            if response_lines and (found_final_prompt or tick >= 150):
                _tmux_timeout_count = 0
                _tmux_turn_count += 1
                resp = "\n".join(response_lines)
                log(f"tmux response captured ({tick+1}s, {len(response_lines)} lines, prompt={'yes' if found_final_prompt else 'no'})")
                return {"response": resp}
    finally:
        try:
            prompt_file.unlink(missing_ok=True)
        except Exception as exc:
            log(f"tmux: prompt file cleanup failed: {exc}")

    # Timeout — reset session after 5 consecutive timeouts
    _tmux_timeout_count += 1
    if _tmux_timeout_count >= 5:
        log("tmux: 5 consecutive timeouts — resetting session")
        _tmux_ready = False
        _tmux_timeout_count = 0
    return {"error": "tmux claude response timeout (180s)"}


def call_llm(prompt: str, system: str, persona: str = "") -> dict:
    """Three-tier LLM fallback.

    Tier 1 — Claude Haiku (internet):  SPARK in auto mode, or MIND_BACKEND=claude
    Tier 2 — Ollama M1.local (LAN):    all personas, or when Claude fails
    Tier 3 — Ollama localhost (Pi):     final fallback when LAN/internet both down
    """
    use_claude = (
        MIND_BACKEND == "claude"
        or (MIND_BACKEND == "auto" and persona == "spark")
    )
    if use_claude:
        try:
            result = call_claude_haiku(prompt, system)
        except Exception as exc:
            try:
                log(f"claude crashed ({exc}), falling back to ollama")
            except Exception:
                pass  # disk full — don't let logging block fallback
            result = {"error": str(exc)}
        if "error" not in result:
            try:
                _log_token_usage(prompt + system, result.get("response", ""))
            except Exception:
                pass
            return result
        log(f"claude failed ({result['error']}), falling back to ollama")

    # Tier 2: M1 Ollama
    result = call_ollama(prompt, system)
    if "error" not in result:
        try:
            _log_token_usage(prompt + system, result.get("response", ""))
        except Exception:
            pass
        return result

    # Tier 3: local Pi Ollama — disabled by default (Pi 4 RAM too small;
    # loading a model alongside px-wake-listen/SenseVoice fills swap and OOMs).
    # Enable with PX_MIND_LOCAL_OLLAMA=1 if running on a beefier Pi.
    if os.environ.get("PX_MIND_LOCAL_OLLAMA") == "1":
        log(f"M1 ollama failed ({result['error']}), falling back to local ollama")
        result = call_ollama(prompt, system, host=LOCAL_OLLAMA_HOST, model=LOCAL_MODEL)
        if "error" not in result:
            try:
                _log_token_usage(prompt + system, result.get("response", ""))
            except Exception:
                pass
            return result

    log(f"M1 ollama failed ({result['error']}), no local fallback — skipping reflection")
    return result


def reflection(awareness: dict, dry: bool) -> dict | None:
    """Layer 2: produce a thought via Ollama."""
    global _last_spoken_text

    session = load_session()
    persona = (awareness.get("persona") or session.get("persona") or "").lower().strip()
    recent_thoughts = load_recent_thoughts(5, persona=persona)
    recent_history = (session.get("history") or [])[-5:]
    notes = load_notes(3, persona=persona)

    # Pick a topic seed (or None = free-will mode) using OS entropy RNG
    topic_seed = _pick_reflection_seed()

    # Build what we last said (for anti-repetition)
    last_thought_text = ""
    if recent_thoughts:
        last_thought_text = recent_thoughts[-1].get("thought", "")

    # Feed moods only (not full thought text) to avoid re-seeding repetition
    recent_moods = [t.get("mood", "?") for t in recent_thoughts]
    recent_actions = [t.get("action", "?") for t in recent_thoughts]
    momentum = awareness.get("mood_momentum", {})
    context_parts = [
        f"Current awareness:\n{json.dumps(awareness, indent=2)}",
        f"Your recent moods: {recent_moods}",
        f"Your recent actions: {recent_actions}",
        f"Your emotional momentum: {momentum.get('mood', 'content')} (valence={momentum.get('valence', 0)}, arousal={momentum.get('arousal', 0)})",
        f"Recent events:\n{json.dumps(recent_history, indent=2)}",
    ]

    if awareness.get("transitions"):
        context_parts.append(f"Transitions just detected: {awareness['transitions']}")

    # Time-of-day context
    time_ctx = TIME_PERIOD_CONTEXT.get(awareness.get("time_period", ""), "")
    if time_ctx:
        period_min = awareness.get("period_duration_min", 0)
        context_parts.append(f"Time context: {time_ctx}")
        if period_min > 60 and awareness.get("minutes_since_interaction", 0) > 30:
            context_parts.append(f"It's been quiet for over {int(period_min)} minutes this {awareness.get('time_period', 'period')}.")

    # Conversation digestion
    convos = awareness.get("recent_conversations", [])
    if convos:
        convo_lines = []
        for c in convos:
            ago = f"{c['minutes_ago']:.0f} min ago" if c["minutes_ago"] >= 0 else "recently"
            convo_lines.append(f"  {c['who']}: \"{c['text']}\" ({ago})")
        context_parts.append("Recent conversations:\n" + "\n".join(convo_lines))

    if notes:
        context_parts.append(f"Your long-term memories:\n" + "\n".join(f"  - {n}" for n in notes))

    if _last_spoken_text:
        context_parts.append(f"What you said last time (DO NOT repeat this): \"{_last_spoken_text}\"")

    # Battery anxiety: nudge LLM to comment when battery is getting low
    batt_pct = awareness.get("battery_pct")
    if batt_pct is not None and batt_pct <= BATTERY_WARN_30:
        context_parts.append(
            f"YOUR BATTERY IS AT {batt_pct}%. You are starting to feel anxious about this. "
            f"{'You are very worried and want someone to charge you urgently.' if batt_pct <= BATTERY_WARN_20 else 'You are mildly concerned and would like to be charged soon.'}"
        )

    # System health nudges
    sys_stats = awareness.get("system", {})
    disk_pct  = sys_stats.get("disk_pct", 0)
    disk_free = sys_stats.get("disk_free_gb")
    cpu_temp  = sys_stats.get("cpu_temp_c", 0)
    ram_pct   = sys_stats.get("ram_pct", 0)
    if disk_pct >= 90:
        free_str = f" ({disk_free} GB free)" if disk_free is not None else ""
        context_parts.append(
            f"YOUR DISK IS {disk_pct}% FULL{free_str}. You feel cramped and anxious. "
            f"You'd like someone to clear some space — you might lose memories if this carries on."
        )
    elif disk_pct >= 80:
        context_parts.append(f"Disk is {disk_pct}% full — you are quietly aware you're filling up.")
    if cpu_temp >= 80:
        context_parts.append(
            f"YOUR CPU TEMPERATURE IS {cpu_temp}°C — you feel feverish and sluggish. "
            f"You wish someone would improve your ventilation."
        )
    elif cpu_temp >= 70:
        context_parts.append(f"CPU temperature is {cpu_temp}°C — you feel a bit warm.")
    if ram_pct >= 90:
        context_parts.append(
            f"RAM is {ram_pct}% used — you feel mentally cluttered and find it hard to think clearly."
        )

    # Always report system vitals as plain text so the LLM registers them
    cpu_pct = sys_stats.get("cpu_pct")
    if cpu_pct is not None:
        context_parts.append(
            f"Your system vitals: CPU {cpu_pct}%, RAM {ram_pct}%, temperature {cpu_temp}°C."
        )

    # Obi's current mode
    obi_mode = awareness.get("obi_mode", "unknown")
    if obi_mode != "unknown":
        context_parts.append(f"Household activity level: {obi_mode}")

    # Multi-camera presence (Frigate)
    frigate_data = awareness.get("frigate") or {}
    rooms_with_people = frigate_data.get("rooms_with_people", [])
    if rooms_with_people:
        context_parts.append(f"People detected by cameras in: {', '.join(rooms_with_people)}")
    cam_detections = frigate_data.get("cameras", {})
    non_person_sightings = []
    for cam_name, cam_info in cam_detections.items():
        for det in cam_info.get("detections", []):
            if det["label"] != "person":
                room = cam_info.get("room", cam_name)
                non_person_sightings.append(f"{det['label']} in {room}")
    if non_person_sightings:
        context_parts.append(f"Other things spotted: {', '.join(non_person_sightings)}")

    # Who's home (from Home Assistant)
    ha = awareness.get("ha_presence")
    if ha and ha.get("people"):
        home_names  = [p["name"] for p in ha["people"] if p.get("home")]
        away_names  = [p["name"] for p in ha["people"] if not p.get("home") and p["state"] not in ("unknown", "unavailable")]
        unknown_names = [p["name"] for p in ha["people"] if p["state"] in ("unknown", "unavailable")]
        parts = []
        if home_names:  parts.append(f"{', '.join(home_names)} home")
        if away_names:  parts.append(f"{', '.join(away_names)} away")
        if unknown_names: parts.append(f"{', '.join(unknown_names)} unknown")
        if parts:
            context_parts.append(f"Who's home (from Home Assistant): {'; '.join(parts)}")

    # Sleep quality (from Pixel Watch via HA)
    sleep = awareness.get("ha_sleep")
    if sleep:
        hours = sleep["sleep_hours"]
        quality = sleep["sleep_quality"]
        if quality == "poor":
            context_parts.append(f"Adrian only got {hours} hours of sleep last night — he might be tired. Be gentle.")
        elif quality == "ok":
            context_parts.append(f"Adrian got {hours} hours of sleep — decent but not great.")
        elif quality == "good":
            context_parts.append(f"Adrian got {hours} hours of sleep — well rested.")

    # Calendar awareness (HA calendar)
    cal_events = awareness.get("ha_calendar")
    if cal_events:
        cal_ctx = _format_calendar_context(cal_events)
        if cal_ctx:
            context_parts.append(cal_ctx)

    # Calendar awareness (household Google Calendar)
    gws_cal = awareness.get("calendar")
    if gws_cal:
        cal_parts = []
        if gws_cal.get("current_event"):
            cal_parts.append(f"Right now: {gws_cal['current_event']}")
            detail = gws_cal.get("current_event_detail", {})
            desc = detail.get("description", "")
            if desc:
                cal_parts.append(f"  Context: {desc[:150]}")
        if gws_cal.get("next_event"):
            mins = gws_cal.get("minutes_until_next", "?")
            cal_parts.append(f"Coming up in {mins} min: {gws_cal['next_event']}")
        if cal_parts:
            context_parts.append("Household schedule:\n" + "\n".join(cal_parts))

    # Routine signals (meds, water)
    routine_ctx = _format_routine_context(awareness.get("ha_routines"))
    if routine_ctx:
        context_parts.append(routine_ctx)

    # Household context signals (call detection, office light, media)
    ha_ctx_text = _format_ha_context(awareness.get("ha_context"))
    if ha_ctx_text:
        context_parts.append(ha_ctx_text)

    # Self-awareness (from recent introspection)
    intro_file = STATE_DIR / "introspection.json"
    if intro_file.exists():
        try:
            intro = json.loads(intro_file.read_text(encoding="utf-8"))
            ts_raw = intro.get("ts", 0)
            if isinstance(ts_raw, str):
                from datetime import datetime as _dt, timezone as _tz
                ts_epoch = _dt.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp()
            else:
                ts_epoch = float(ts_raw)
            age = time.time() - ts_epoch
            if age < INTROSPECTION_STALE_S:
                context_parts.append(
                    "Self-awareness (from recent introspection):\n"
                    + _format_introspection(intro)
                    + "\n\nYou can use action='evolve' to propose a change to yourself.\n"
                    "Only do this if you have a specific, well-formed idea — not vague wishes."
                )
        except (json.JSONDecodeError, OSError):
            pass

    # Inject topic seed, or free-will prompt if no seed was drawn
    if topic_seed is None:
        context_parts.append(
            "No prompt today — free will mode. Follow your own curiosity. "
            "What do YOU actually want to think about right now? "
            "Don't reach for the obvious. What's been sitting in the back of your mind?"
        )
    else:
        context_parts.append(f"Creative prompt (optional inspiration, don't just answer it literally): {topic_seed}")

    context = "\n\n".join(context_parts) + "\n\nReflect on this moment. Be original."

    if dry:
        thought = {
            "ts": utc_timestamp(),
            "thought": f"Dry-run thought: {topic_seed[:60] if topic_seed else '(free will)'}",
            "mood": _SYS_RNG.choice(list(VALID_MOODS)),
            "action": "comment",
            "salience": 0.3,
        }
        log(f"reflection (dry): {thought['thought']}")
        append_thought(thought, persona=persona)
        return thought

    effective_backend = "claude" if (MIND_BACKEND == "claude" or (MIND_BACKEND == "auto" and persona == "spark")) else "ollama"
    log(f"reflecting... (backend={effective_backend}, persona={persona or 'default'})")
    t0 = time.monotonic()
    if persona == "spark":
        angles = _pick_spark_angles()
        formatted = "\n".join(f"- {a}" for a in angles)
        system_prompt = (_SPARK_REFLECTION_PREFIX + formatted
                         + _SPARK_REFLECTION_SUFFIX + _daytime_action_hint())
    else:
        system_prompt = PERSONA_REFLECTION_SYSTEMS.get(persona, REFLECTION_SYSTEM)

    # Conditionally inject 'explore' action into the system prompt
    session = load_session()
    try:
        aw_data = json.loads(AWARENESS_FILE.read_text(encoding="utf-8"))
    except Exception:
        aw_data = {}

    explore_available = _can_explore(session, aw_data)
    if explore_available:
        system_prompt = system_prompt.replace(
            'time_check, calendar_check, morning_fact"',
            'time_check, calendar_check, morning_fact, explore"'
        )

    # Exploration hints for context
    explore_hints = []
    if explore_available:
        obi_mode = aw_data.get("obi_mode", "unknown")
        if obi_mode in ("active", "calm"):
            explore_hints.append("Someone is nearby — you could go see what's happening.")
        mins_idle = aw_data.get("minutes_since_interaction", 0)
        if mins_idle > 30:
            explore_hints.append("You haven't moved in a while.")
        try:
            exp_file = STATE_DIR / "exploration.jsonl"
            if exp_file.exists():
                exp_lines = exp_file.read_text(encoding="utf-8").strip().splitlines()
                for line in reversed(exp_lines[-10:]):
                    entry = json.loads(line)
                    if entry.get("type") == "observation" and entry.get("interesting"):
                        lm = entry.get("landmark", "something")
                        explore_hints.append(f"Last time you explored you found {lm}.")
                        break
        except Exception:
            pass
    if explore_hints:
        context = context + "\n\nExploration hints: " + " ".join(explore_hints)

    result = call_llm(context, system_prompt, persona=persona)
    elapsed = time.monotonic() - t0

    if "error" in result:
        log(f"reflection failed: {result['error']}")
        return None

    raw = result.get("response", "")
    parsed = extract_json(raw)
    if not parsed:
        log(f"reflection: no JSON in response: {raw}")
        return None

    # Validate and sanitize
    thought = {
        "ts": utc_timestamp(),
        "thought": str(parsed.get("thought", "")),
        "mood": parsed.get("mood", "content") if parsed.get("mood") in VALID_MOODS else "content",
        "action": parsed.get("action", "wait") if parsed.get("action") in VALID_ACTIONS else "comment",
        "salience": max(0.0, min(1.0, float(parsed.get("salience", 0.5)))),
    }

    # Apply mood momentum: blend LLM's raw mood with running average
    raw_mood = thought["mood"]
    thought["mood"] = apply_mood_momentum(raw_mood)
    if thought["mood"] != raw_mood:
        thought["raw_mood"] = raw_mood  # preserve for debugging

    eval_toks = result.get("eval_count", 0)
    eval_dur = result.get("eval_duration", 0) / 1e9
    tps = round(eval_toks / eval_dur, 1) if eval_dur > 0 else 0

    # Anti-repetition: check against ALL recent thoughts, not just the last
    max_sim = 0.0
    for prev in recent_thoughts:
        sim = text_similarity(thought["thought"], prev.get("thought", ""))
        if sim > max_sim:
            max_sim = sim
    # Also check against last spoken text
    if _last_spoken_text:
        sim = text_similarity(thought["thought"], _last_spoken_text)
        if sim > max_sim:
            max_sim = sim
    if max_sim > SIMILARITY_THRESHOLD:
        log(f"thought suppressed (similarity {max_sim:.0%}): {thought['thought'][:80]}")
        thought["action"] = "wait"
        thought["salience"] = 0.0

    log(f"thought: {thought['thought']}  mood={thought['mood']} "
        f"action={thought['action']} salience={thought['salience']:.1f} "
        f"({elapsed:.1f}s, {tps} tok/s)")

    # Track mood trajectory
    _mood_history.append(thought["mood"])
    if len(_mood_history) > 20:
        _mood_history[:] = _mood_history[-20:]

    append_thought(thought, persona=persona)

    # Write mood state for px-alive servo coordination
    try:
        atomic_write(MOOD_FILE, json.dumps({
            "ts": utc_timestamp(),
            "mood": thought["mood"],
            "valence": round(_mood_v, 2),
            "arousal": round(_mood_a, 2),
        }))
    except Exception:
        pass

    # Auto-remember high-salience thoughts (persona-scoped)
    if thought["salience"] >= SALIENCE_THRESHOLD:
        auto_remember(thought, persona=persona)

    return thought


def _run_voice(env: dict, *, timeout: int = 45, label: str = "") -> subprocess.CompletedProcess:
    """Run tool-voice and log voice-lock contention if detected."""
    result = subprocess.run(
        [str(BIN_DIR / "tool-voice")],
        capture_output=True, text=True, check=False, env=env, timeout=timeout,
    )
    if result.returncode != 0:
        logged = False
        if result.stdout:
            try:
                payload = json.loads(result.stdout.strip().splitlines()[-1])
                if "voice lock timeout" in payload.get("error", ""):
                    log(f"expression: voice contention — voice.lock busy, skipping speech"
                        f"{f' ({label})' if label else ''}")
                    logged = True
            except (json.JSONDecodeError, IndexError):
                pass
        if not logged:
            log(f"expression: tool-voice failed rc={result.returncode}"
                f" stderr={result.stderr[-200:]!r}"
                f"{f' ({label})' if label else ''}")
    return result


def expression(thought: dict, dry: bool, awareness: dict | None = None) -> None:
    """Layer 3: act on a thought."""
    global _last_spoken_text, _last_morning_fact_date

    action = thought.get("action", "wait")
    if action == "wait":
        return

    # Gate speech on obi_mode: at night when Obi is absent, suppress non-essential actions
    _aw = awareness or {}
    if not _aw:
        try:
            _aw = json.loads(AWARENESS_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            log(f"expression: awareness read failed: {exc}")
    _obi_mode = _aw.get("obi_mode", "unknown")
    log(f"expression: obi_mode={_obi_mode} hour={_aw.get('hour','?')} action={action}")
    if _obi_mode == "absent" and action in ABSENT_GATED_ACTIONS:
        log(f"expression: suppressed {action} — obi_mode=absent (night, Obi likely asleep)")
        return
    if _obi_mode == "at-school" and action in ABSENT_GATED_ACTIONS:
        log(f"expression: suppressed {action} — Obi at school (calendar)")
        return
    if _obi_mode == "at-mums" and action in ABSENT_GATED_ACTIONS:
        log(f"expression: suppressed {action} — Obi at Mum's (calendar)")
        return

    # Calendar-driven mode shifts
    _cal = _aw.get("calendar", {}) if isinstance(_aw, dict) else {}
    _current_event = (_cal.get("current_event") or "").lower()
    if "decompress" in _current_event and action in ("greet", "comment", "scan", "calendar_check"):
        log(f"expression: suppressed {action} — after-school decompress (low-demand mode)")
        return
    if "quiet time" in _current_event:
        log(f"expression: suppressed {action} — quiet time (calendar)")
        return
    if "bedtime" in _current_event and action not in ("wait", "remember"):
        log(f"expression: suppressed {action} — bedtime routine (calm mode)")
        return

    # Suppress speech when Adrian is on a call or mic is active
    ha_ctx = _aw.get("ha_context") or {}
    if ha_ctx.get("adrian_on_call") or ha_ctx.get("adrian_mic_active"):
        if action in ("greet", "comment", "weather_comment", "play_sound",
                       "time_check", "calendar_check", "photograph"):
            log(f"expression: suppressed {action} — Adrian on call/mic active")
            return

    # Gate on charging: suppress servo-related actions when plugged in
    try:
        _batt = json.loads(BATTERY_FILE.read_text(encoding="utf-8"))
        _charging = bool(_batt.get("charging", False))
    except Exception:
        _charging = False
    if _charging and action in CHARGING_GATED_ACTIONS:
        log(f"expression: suppressed {action} — battery charging")
        return

    text = thought.get("thought", "")
    env = os.environ.copy()
    env["PX_DRY"] = "1" if dry else "0"
    # Use a short voice-lock timeout so expression() fails fast when another
    # process (voice loop, px-wake-listen) is already speaking.
    env["PX_VOICE_LOCK_TIMEOUT"] = "5"

    # Inject persona voice settings so tool-voice uses the right espeak voice.
    # For thought-generated text (greet/comment/scan/look_at), the reflection
    # prompt already produces persona-voiced text — skip Ollama rephrase to
    # avoid double "FUCK YEAH!" or other duplication.
    # For weather_comment, the raw weather data needs rephrasing.
    session = load_session()
    persona = (session.get("persona") or "").lower().strip()
    needs_rephrase = action in ("weather_comment",)
    if persona and persona in PERSONA_VOICE_ENV:
        for k, v in PERSONA_VOICE_ENV[persona].items():
            env[k] = v
        if not needs_rephrase:
            env["_PX_VOICE_PERSONA_DONE"] = "1"  # skip Ollama rephrase
        log(f"expressing: action={action} persona={persona} rephrase={needs_rephrase}")
    else:
        log(f"expressing: action={action}")

    try:
        if action == "greet":
            # Use tool-voice (not tool-perform) to avoid GPIO collision with px-alive
            env["PX_TEXT"] = text[:2000]
            _run_voice(env, label="greet")
            _last_spoken_text = text[:200]

        elif action == "comment":
            env["PX_TEXT"] = text[:2000]
            _run_voice(env, label="comment")
            _last_spoken_text = text[:200]

        elif action == "weather_comment":
            # Fetch fresh weather, build a remark, speak it
            weather = fetch_weather(dry)
            if weather and weather.get("summary"):
                env["PX_TEXT"] = weather["summary"][:2000]
            else:
                env["PX_TEXT"] = text[:2000]
            _run_voice(env, label="weather_comment")
            _last_spoken_text = env["PX_TEXT"][:200]

        elif action == "morning_fact":
            today = dt.datetime.now(HOBART_TZ).strftime("%Y-%m-%d")
            if _last_morning_fact_date == today:
                log("expression: morning_fact already told today — skipping")
            elif text:
                _last_morning_fact_date = today
                env["PX_TEXT"] = text[:2000]
                _run_voice(env, label="morning_fact")
                _last_spoken_text = text[:200]

        elif action == "scan":
            # Awareness already reads sonar every 30s; just speak the thought
            if text:
                env["PX_TEXT"] = text[:2000]
                _run_voice(env, label="scan")
                _last_spoken_text = text[:200]

        elif action == "remember":
            env["PX_NOTE"] = text[:500]
            subprocess.run(
                [str(BIN_DIR / "tool-remember")],
                capture_output=True, text=True, check=False, env=env, timeout=10,
            )

        elif action == "look_at":
            # px-alive handles physical servo movement; px-mind just speaks the thought
            if text:
                env["PX_TEXT"] = text[:2000]
                _run_voice(env, label="look_at")
                _last_spoken_text = text[:200]

        elif action == "explore":
            log("expression: initiating exploration")
            session = load_session()
            awareness_data = {}
            try:
                awareness_data = json.loads(AWARENESS_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
            if not _can_explore(session, awareness_data):
                log("expression: explore gates failed on re-check")
                return

            # yield_alive
            try:
                subprocess.run(
                    ["bash", "-c", f"source {BIN_DIR / 'px-env'} && yield_alive"],
                    capture_output=True, text=True, check=False, timeout=15,
                )
            except Exception as exc:
                log(f"expression: yield_alive failed: {exc}")

            # Wait for px-alive to exit
            alive_pid_file = Path(os.environ.get("LOG_DIR",
                                  str(PROJECT_ROOT / "logs"))) / "px-alive.pid"
            waited = 0.0
            while waited < 5:
                if not alive_pid_file.exists():
                    break
                try:
                    pid = int(alive_pid_file.read_text().strip())
                    if not Path(f"/proc/{pid}").is_dir():
                        break
                except Exception:
                    break
                time.sleep(0.5)
                waited += 0.5
            if waited >= 5:
                log("expression: px-alive still running after 5s — aborting exploration")
                return

            # Update exploration_meta (establishes cooldown)
            meta_path = STATE_DIR / "exploration_meta.json"
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
            meta["last_explore_ts"] = dt.datetime.now(dt.timezone.utc).isoformat()
            try:
                atomic_write(meta_path, json.dumps(meta, indent=2))
            except Exception:
                pass

            # Run tool-wander in explore mode
            explore_env = env.copy()
            explore_env["PX_WANDER_MODE"] = "explore"
            explore_env["PX_WANDER_DURATION_S"] = "180"
            explore_env["PX_WANDER_STEPS"] = "20"
            explore_result = {}
            try:
                # Use Popen + SIGTERM instead of subprocess.run(timeout=) which
                # sends SIGKILL (uncatchable) — px-wander's finally block needs
                # SIGTERM to execute motor cleanup.
                proc = subprocess.Popen(
                    [str(BIN_DIR / "tool-wander")],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, env=explore_env,
                )
                try:
                    stdout, stderr = proc.communicate(timeout=240)
                except subprocess.TimeoutExpired:
                    log("expression: exploration timeout — sending SIGTERM")
                    proc.terminate()
                    try:
                        stdout, stderr = proc.communicate(timeout=15)
                    except subprocess.TimeoutExpired:
                        log("expression: SIGTERM ignored — sending SIGKILL")
                        proc.kill()
                        stdout, stderr = proc.communicate()
                    log("expression: exploration timed out")
                    stdout = None
                if stdout:
                    try:
                        explore_result = json.loads(stdout.strip().splitlines()[-1])
                        obs = explore_result.get("observations", 0)
                        log(f"expression: exploration complete — {obs} observations")
                    except (json.JSONDecodeError, IndexError):
                        log(f"expression: exploration finished (rc={proc.returncode})")
            except Exception as exc:
                log(f"expression: exploration error: {exc}")

            # Post-exploration thought
            try:
                obs = explore_result.get("observations", 0)
                post_thought = {
                    "ts": utc_timestamp(),
                    "thought": f"I just finished exploring and found {obs} things worth noting." if obs > 0
                               else "I went exploring but didn't find anything remarkable this time.",
                    "mood": "curious",
                    "action": "wait",
                    "salience": 0.5,
                }
                append_thought(post_thought, persona=persona)
            except Exception as exc:
                log(f"expression: post-exploration thought failed: {exc}")

            # Verify px-alive is running
            time.sleep(2)
            try:
                result = subprocess.run(
                    ["systemctl", "is-active", "px-alive"],
                    capture_output=True, text=True, check=False, timeout=5,
                )
                if result.stdout.strip() != "active":
                    log("expression: px-alive not running — restarting")
                    subprocess.run(
                        ["sudo", "-n", "systemctl", "start", "px-alive"],
                        capture_output=True, check=False, timeout=10,
                    )
            except Exception as exc:
                log(f"expression: px-alive restart check failed: {exc}")

        elif action == "play_sound":
            sound = MOOD_TO_SOUND.get(thought.get("mood", ""), "chime")
            env["PX_SOUND"] = sound
            subprocess.run([str(BIN_DIR / "tool-play-sound")],
                           capture_output=True, text=True, check=False, env=env, timeout=15)

        elif action == "photograph":
            # tool-describe-scene is self-contained: yield_alive, capture, vision, speak
            # Use Popen+SIGTERM for graceful cleanup on timeout
            proc = subprocess.Popen(
                [str(BIN_DIR / "tool-describe-scene")],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            try:
                proc.communicate(timeout=120)
            except subprocess.TimeoutExpired:
                log("expression: photograph timed out — sending SIGTERM")
                proc.terminate()
                try:
                    proc.communicate(timeout=15)
                except subprocess.TimeoutExpired:
                    log("expression: photograph SIGTERM ignored — SIGKILL")
                    proc.kill()
                    proc.communicate()

        elif action == "emote":
            emote_name = MOOD_TO_EMOTE.get(thought.get("mood", ""), "idle")
            env["PX_EMOTE"] = emote_name
            subprocess.run([str(BIN_DIR / "tool-emote")],
                           capture_output=True, text=True, check=False, env=env, timeout=15)

        elif action == "look_around":
            env["PX_PAN"] = str(_SYS_RNG.randint(-40, 40))
            env["PX_TILT"] = str(_SYS_RNG.randint(-10, 30))
            subprocess.run([str(BIN_DIR / "tool-look")],
                           capture_output=True, text=True, check=False, env=env, timeout=15)
            if text:
                env["PX_TEXT"] = text[:2000]
                _run_voice(env, label="look_around")

        elif action == "time_check":
            subprocess.run([str(BIN_DIR / "tool-time")],
                           capture_output=True, text=True, check=False, env=env, timeout=15)

        elif action == "calendar_check":
            env["PX_CALENDAR_ACTION"] = "next"
            env.setdefault("PX_CALENDAR_ID", CALENDAR_ID)
            subprocess.run([str(BIN_DIR / "tool-gws-calendar")],
                           capture_output=True, text=True, check=False, env=env, timeout=60)

        elif action == "introspect":
            env["PX_DRY"] = "1" if dry else "0"
            result = subprocess.run(
                [str(BIN_DIR / "tool-introspect")],
                capture_output=True, text=True, check=False, env=env, timeout=30)
            log(f"expression: introspect completed rc={result.returncode}")

        elif action == "evolve":
            env["PX_EVOLVE_INTENT"] = thought.get("thought", "")[:500]
            env["PX_DRY"] = "1" if dry else "0"
            result = subprocess.run(
                [str(BIN_DIR / "tool-evolve")],
                capture_output=True, text=True, check=False, env=env, timeout=15)
            intent = thought.get("thought", "")[:80]
            log(f"expression: evolve queued — {intent}")

        else:
            log(f"expression: unhandled action: {action}")

    except subprocess.TimeoutExpired:
        log(f"expression timed out: {action} (possible voice contention — another process may hold voice.lock)")
    except Exception as exc:
        import traceback
        log(f"expression error: {exc}\n{traceback.format_exc()}")

    # Record in session history
    update_session(
        fields={"last_action": "px_mind"},
        history_entry={
            "event": "mind",
            "mood": thought.get("mood", ""),
            "action": action,
            "thought": text,
        },
    )


def reactive_response(transition: str, awareness: dict, dry: bool) -> None:
    """Immediate template-based reaction to a state transition (no LLM call)."""
    global _last_spoken_text, _last_reactive_phrases

    templates = REACTIVE_TEMPLATES.get(transition)
    if not templates:
        return

    persona = (awareness.get("persona") or "").lower().strip()
    phrases = templates.get(persona, templates["default"])

    # Day/night split: dict with "day"/"night" keys (used by spark someone_left)
    if isinstance(phrases, dict):
        hour = dt.datetime.now(HOBART_TZ).hour
        slot = "night" if (hour >= 19 or hour < 7) else "day"
        phrases = phrases.get(slot, phrases.get("day", list(phrases.values())[0]))

    # Recency filter: avoid repeating any of the last 3 phrases for this slot
    key = f"{transition}:{persona}"
    recent = _last_reactive_phrases.get(key, [])
    available = [p for p in phrases if p not in recent]
    if not available:
        available = list(phrases)
        recent = []
    text = random.choice(available)
    recent.append(text)
    _last_reactive_phrases[key] = recent[-3:]  # keep only last 3

    # Substitute {thought} placeholder with the latest thought for this persona
    if "{thought}" in text:
        thoughts = load_recent_thoughts(1, persona)
        latest = thoughts[0].get("thought", "").strip() if thoughts else ""
        if latest:
            text = text.replace("{thought}", latest)
        else:
            # No thought available — fall back to the previous available phrase without placeholder
            fallback = [p for p in available if "{thought}" not in p]
            text = random.choice(fallback) if fallback else text.replace(" {thought}", "").rstrip(" —")

    env = os.environ.copy()
    env["PX_DRY"] = "1" if dry else "0"
    env["PX_TEXT"] = text

    # Inject persona voice settings
    if persona and persona in PERSONA_VOICE_ENV:
        for k, v in PERSONA_VOICE_ENV[persona].items():
            env[k] = v

    env["PX_VOICE_LOCK_TIMEOUT"] = "5"  # fail fast on voice contention
    log(f"reactive: {transition} → \"{text[:60]}\"")

    try:
        _run_voice(env, timeout=20, label=f"reactive_{transition}")
        _last_spoken_text = text[:200]
    except subprocess.TimeoutExpired:
        log(f"reactive: voice contention — tool-voice timed out for {transition}")
    except Exception as exc:
        log(f"reactive error: {exc}")

    update_session(
        fields={"last_action": "px_mind"},
        history_entry={
            "event": "mind",
            "mood": awareness.get("mood_momentum", {}).get("mood", ""),
            "action": f"reactive_{transition}",
            "thought": text,
        },
    )


def mind_loop(args) -> None:
    """Main cognitive loop."""
    prev_awareness: dict = {}
    last_reflection_mono = 0.0
    last_expression_mono = 0.0
    last_reactive_mono = 0.0
    last_battery_warn_mono = 0.0
    global _consecutive_reflection_failures, _reflection_offline_spoken
    consecutive_critical = 0          # require 2 consecutive critical readings before shutdown
    REFLECTION_FAIL_WARN_THRESHOLD = 3  # warn after 3 consecutive failures

    # Exponential backoff: reflection interval grows during extended idle periods.
    # Resets on any interaction (listening, transition, reactive event).
    backoff_multiplier = 1.0
    BACKOFF_FACTOR     = 1.5   # multiply interval by this after each idle reflection
    BACKOFF_MAX        = 8.0   # cap at 8× base (e.g. 300s base → max 40 min)

    log(f"cognitive loop started (awareness every {args.awareness_interval}s, "
        f"reflection every {args.reflection_interval}s idle)")

    # Startup check: verify Ollama models are available
    for label, host, model in [("M1", OLLAMA_HOST, MODEL)]:
        try:
            r = urllib.request.urlopen(f"{host}/api/tags", timeout=3)
            tags = json.loads(r.read())
            available = [m["name"] for m in tags.get("models", [])]
            if model in available:
                log(f"✓ {label} ollama: model '{model}' available ({host})")
            else:
                log(f"⚠ {label} ollama: model '{model}' NOT found — available: {', '.join(available) or 'none'}")
        except Exception as exc:
            log(f"⚠ {label} ollama unreachable at startup: {exc}")

    while True:
        now = time.monotonic()

        # Pause during active conversations; reset backoff on interaction
        session = load_session()
        if session.get("listening", False):
            backoff_multiplier = 1.0  # reset when talking
            time.sleep(5)
            continue

        # Layer 1: Awareness
        awareness, transitions = awareness_tick(prev_awareness, args.dry_run)
        prev_awareness = awareness

        # Any transition resets the backoff (something is happening)
        if transitions:
            backoff_multiplier = 1.0

        # ── Battery monitoring (deterministic, before LLM) ──────────────
        battery_pct = awareness.get("battery_pct")
        battery_charging = awareness.get("battery_charging", False)
        if battery_pct is not None:
            if battery_pct <= BATTERY_CRITICAL:
                if battery_charging:
                    log(f"battery: {battery_pct}% but charging — suppressing shutdown")
                    consecutive_critical = 0
                else:
                    consecutive_critical += 1
                    log(f"battery: critical reading {battery_pct}% "
                        f"(consecutive={consecutive_critical}/2)")
                    if consecutive_critical >= 2:
                        # Confirmed critical — alarm + shutdown
                        battery_emergency_shutdown(battery_pct, args.dry_run)
                        break  # unreachable after shutdown, but clean for dry-run
            elif battery_pct <= BATTERY_WARN_15:
                consecutive_critical = 0
                if (now - last_battery_warn_mono) > BATTERY_WARN_15_INTERVAL:
                    battery_warn_comment(battery_pct, args.dry_run)
                    last_battery_warn_mono = now
                    last_expression_mono = now
            elif battery_pct <= BATTERY_WARN_20:
                consecutive_critical = 0
                if (now - last_battery_warn_mono) > BATTERY_WARN_20_INTERVAL:
                    battery_warn_comment(battery_pct, args.dry_run)
                    last_battery_warn_mono = now
                    last_expression_mono = now
            else:
                consecutive_critical = 0

        # Reactive behavior: instant template response for key transitions
        reactive_transitions = {"someone_appeared", "someone_left"}
        reacted = False
        if transitions and (now - last_reactive_mono) > REACTIVE_COOLDOWN_S:
            for t in transitions:
                if t in reactive_transitions and t in REACTIVE_TEMPLATES:
                    reactive_response(t, awareness, args.dry_run)
                    last_reactive_mono = now
                    last_expression_mono = now  # count as expression too
                    reacted = True
                    break  # one reactive response per tick

        # Layer 2: Reflection (on transition or idle timeout with backoff)
        effective_interval = min(args.reflection_interval * backoff_multiplier, args.reflection_interval * BACKOFF_MAX)
        should_reflect = not reacted and (
            len(transitions) > 0
            or (now - last_reflection_mono) > effective_interval
        )

        if should_reflect:
            thought = reflection(awareness, args.dry_run)
            last_reflection_mono = now

            if thought is None:
                _consecutive_reflection_failures += 1
                if _consecutive_reflection_failures >= REFLECTION_FAIL_WARN_THRESHOLD and not _reflection_offline_spoken:
                    log(f"reflection: {_consecutive_reflection_failures} consecutive failures — speaking warning")
                    _reflection_offline_spoken = True
                    env = os.environ.copy()
                    env["PX_DRY"] = "1" if args.dry_run else "0"
                    env["PX_TEXT"] = "My thinking is offline — all reflection backends are unreachable."
                    subprocess.run([str(BIN_DIR / "tool-voice")], env=env,
                                   capture_output=True, check=False, timeout=20)
            else:
                _consecutive_reflection_failures = 0
                if _reflection_offline_spoken:
                    log("reflection: backends recovered — resetting offline flag")
                    _reflection_offline_spoken = False

            if thought and thought.get("action", "wait") != "wait":
                # Layer 3: Expression (with cooldown)
                if (now - last_expression_mono) > EXPRESSION_COOLDOWN_S:
                    expression(thought, args.dry_run, awareness=awareness)
                    last_expression_mono = now
                else:
                    log(f"expression suppressed (cooldown): {thought['action']}")
            else:
                # Idle thought — apply backoff so we reflect less often when nobody's around
                if not transitions:
                    backoff_multiplier = min(backoff_multiplier * BACKOFF_FACTOR, BACKOFF_MAX)
                    log(f"backoff: reflection interval now {effective_interval:.0f}s × {backoff_multiplier:.1f}")

        time.sleep(args.awareness_interval)


def main(argv) -> int:
    parser = argparse.ArgumentParser(description="PiCar-X cognitive loop daemon")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip Ollama + sonar, use placeholder thoughts")
    parser.add_argument("--awareness-interval", type=int, default=AWARENESS_INTERVAL_S,
                        help=f"Seconds between awareness ticks (default: {AWARENESS_INTERVAL_S})")
    parser.add_argument("--reflection-interval", type=int, default=REFLECTION_IDLE_S,
                        help=f"Max seconds between reflections (default: {REFLECTION_IDLE_S})")
    args = parser.parse_args(argv)

    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Guard: if another live px-mind already owns the PID file, exit cleanly
    _existing_pid = None
    try:
        _existing_pid = int(PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        pass
    if _existing_pid and _existing_pid != os.getpid() and os.path.isdir(f"/proc/{_existing_pid}"):
        log(f"another px-mind (pid={_existing_pid}) already running — exiting")
        return 0
    PID_FILE.write_text(str(os.getpid()))
    log(f"starting pid={os.getpid()} dry={args.dry_run} model={MODEL}")

    def _shutdown(sig, _frame):
        log(f"received signal {sig} — shutting down")
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    if args.dry_run:
        log("dry-run: simulating 3 cognitive cycles")
        prev = {}
        for i in range(3):
            awareness, transitions = awareness_tick(prev, dry=True)
            prev = awareness
            thought = reflection(awareness, dry=True)
            if thought and thought.get("action") != "wait":
                expression(thought, dry=True, awareness=awareness)
            time.sleep(0.3)
        log("dry-run complete")
        PID_FILE.unlink(missing_ok=True)
        return 0

    try:
        mind_loop(args)
    except SystemExit:
        pass
    except Exception as exc:
        log(f"fatal: {exc}")
        return 1
    finally:
        PID_FILE.unlink(missing_ok=True)
        log("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
