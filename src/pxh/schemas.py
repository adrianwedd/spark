"""Declarative parameter schemas for SPARK tools, mirrored from validate_action.

Hand-maintained alongside voice_loop.validate_action — test_schemas.py guards
that every ALLOWED_TOOLS entry is covered.

Schema shape per tool:
    {"description": str, "params": {name: {"type", "required", and one of "range"/"enum"/"max"/"min"}}}

No-param tools get "params": {}.
"""

from pxh.spark_config import ANNOUNCE_MAX_CHARS

TOOL_SCHEMAS: dict = {
    # --- No-param tools ---
    "tool_status": {
        "description": "Report robot status",
        "params": {},
    },
    "tool_stop": {
        "description": "Stop all motion immediately",
        "params": {},
    },
    "tool_weather": {
        "description": "Fetch and report current weather",
        "params": {},
    },
    "tool_sonar": {
        "description": "Read ultrasonic distance sensor",
        "params": {},
    },
    "tool_time": {
        "description": "Report the current time",
        "params": {},
    },
    "tool_photograph": {
        "description": "Take a photograph with the camera",
        "params": {},
    },
    "tool_face": {
        "description": "Detect faces in the camera view",
        "params": {},
    },
    "tool_describe_scene": {
        "description": "Describe what the camera currently sees",
        "params": {},
    },
    "tool_api_start": {
        "description": "Start the REST API server",
        "params": {},
    },
    "tool_api_stop": {
        "description": "Stop the REST API server",
        "params": {},
    },

    # --- Motion tools ---
    "tool_circle": {
        "description": "Drive in a circle",
        "params": {
            "speed":    {"type": "int",   "range": [0, 60],  "required": False},
            "duration": {"type": "float", "range": [1, 12],  "required": False},
        },
    },
    "tool_figure8": {
        "description": "Drive in a figure-8 pattern",
        "params": {
            "speed":    {"type": "int",   "range": [0, 60],  "required": False},
            "duration": {"type": "float", "range": [1, 12],  "required": False},
            "rest":     {"type": "float", "range": [0, 5],   "required": False},
        },
    },
    "tool_drive": {
        "description": "Drive forward or backward",
        "params": {
            "direction": {"type": "str",   "enum": ["forward", "backward"], "required": False},
            "speed":     {"type": "int",   "range": [0, 60],                "required": False},
            "duration":  {"type": "float", "range": [0.1, 10.0],            "required": False},
            "steer":     {"type": "int",   "range": [-35, 35],              "required": False},
        },
    },
    "tool_wander": {
        "description": "Wander autonomously, avoiding obstacles or exploring",
        "params": {
            "steps":    {"type": "int", "range": [1, 20],                  "required": False},
            "mode":     {"type": "str", "enum": ["avoid", "explore"],      "required": False},
            "duration": {"type": "int", "range": [30, 300],                "required": False},
        },
    },

    # --- Sensor / perception tools ---
    "tool_look": {
        "description": "Pan and tilt the camera head to a given angle",
        "params": {
            "pan":  {"type": "int",   "range": [-90, 90], "required": False},
            "tilt": {"type": "int",   "range": [-35, 65], "required": False},
            "ease": {"type": "float", "range": [0.1, 5.0], "required": False},
        },
    },
    "tool_frigate_events": {
        "description": "Fetch recent Frigate camera events",
        "params": {
            "limit": {"type": "int", "range": [1, 20], "required": False},
        },
    },

    # --- Speech / audio tools ---
    "tool_voice": {
        "description": "Speak text aloud via espeak",
        "params": {
            "text": {"type": "str", "max": 2000, "required": True},
        },
    },
    "tool_emote": {
        "description": "Play an emotion animation on the display",
        "params": {
            "name": {
                "type": "str",
                "enum": ["idle", "curious", "thinking", "happy", "alert", "excited", "sad", "shy"],
                "required": False,
            },
        },
    },
    "tool_play_sound": {
        "description": "Play a named sound effect",
        "params": {
            "name": {"type": "str", "max": 40, "required": True},
        },
    },
    "tool_record_sound": {
        "description": "Record a named sound clip from the microphone",
        "params": {
            "name":    {"type": "str", "max": 60,       "required": True},
            "seconds": {"type": "int", "range": [1, 15], "required": False},
        },
    },
    "tool_announce": {
        "description": "Announce text via Nest speakers through the HA relay",
        "params": {
            "text":    {"type": "str",  "max": ANNOUNCE_MAX_CHARS,    "required": True},
            "targets": {"type": "list",                               "required": False},
        },
    },

    # --- Memory tools ---
    "tool_remember": {
        "description": "Store a note in persistent memory",
        "params": {
            "text": {"type": "str", "max": 500, "required": True},
        },
    },
    "tool_recall": {
        "description": "Retrieve recent memory notes",
        "params": {
            "limit": {"type": "int", "range": [1, 20], "required": False},
        },
    },

    # --- Performance / scripted action ---
    "tool_perform": {
        "description": "Execute a scripted sequence of motion+speech steps",
        "params": {
            "steps": {"type": "list", "required": True},
        },
    },

    # --- Question answering ---
    "tool_qa": {
        "description": "Answer a factual question using Claude",
        "params": {
            "text": {"type": "str", "max": 2000, "required": True},
        },
    },

    # --- Utility ---
    "tool_timer": {
        "description": "Set a countdown timer",
        "params": {
            "seconds": {"type": "int", "range": [5, 3600], "required": False},
            "label":   {"type": "str", "max": 100,         "required": False},
        },
    },

    # --- Persona chat tools ---
    "tool_chat": {
        "description": "Chat as the GREMLIN persona",
        "params": {
            "text": {"type": "str", "max": 2000, "required": True},
        },
    },
    "tool_chat_vixen": {
        "description": "Chat as the VIXEN persona",
        "params": {
            "text": {"type": "str", "max": 2000, "required": True},
        },
    },

    # --- Child-companion tools ---
    "tool_routine": {
        "description": "Load or step through a named daily routine",
        "params": {
            "action": {
                "type": "str",
                "enum": ["load", "next", "status", "complete"],
                "required": False,
            },
            "name": {"type": "str", "max": 40, "required": False},
        },
    },
    "tool_checkin": {
        "description": "Ask about or record Obi's current mood/check-in",
        "params": {
            "action": {"type": "str", "enum": ["ask", "record"], "required": False},
            "mood":   {"type": "str", "max": 40,                 "required": False},
        },
    },
    "tool_celebrate": {
        "description": "Celebrate an achievement with text or animation",
        "params": {
            "text": {"type": "str", "max": 300, "required": False},
        },
    },
    "tool_transition": {
        "description": "Warn, buffer, or confirm an activity transition",
        "params": {
            "action": {
                "type": "str",
                "enum": ["warn", "buffer", "arrived"],
                "required": False,
            },
            "minutes": {"type": "int", "range": [1, 60], "required": False},
            "label":   {"type": "str", "max": 80,        "required": False},
        },
    },
    "tool_quiet": {
        "description": "Start, check, or end a quiet-time session",
        "params": {
            "action": {
                "type": "str",
                "enum": ["start", "check", "end"],
                "required": False,
            },
        },
    },
    "tool_sleep": {
        "description": "Start, check, or end bedtime/sleep mode",
        "params": {
            "action": {
                "type": "str",
                "enum": ["start", "check", "end"],
                "required": False,
            },
        },
    },
    "tool_breathe": {
        "description": "Guide a breathing exercise",
        "params": {
            "type":   {"type": "str", "enum": ["box", "478", "simple"], "required": False},
            "rounds": {"type": "int", "range": [1, 4],                  "required": False},
        },
    },
    "tool_dopamine_menu": {
        "description": "Suggest or add a dopamine-menu activity",
        "params": {
            "action":  {"type": "str", "enum": ["suggest", "add"],              "required": False},
            "item":    {"type": "str", "max": 200,                              "required": False},
            "energy":  {"type": "str", "enum": ["high", "medium", "low"],       "required": False},
            "context": {"type": "str", "enum": ["free", "focus", "wind-down"],  "required": False},
        },
    },
    "tool_sensory_check": {
        "description": "Ask about or record a sensory issue",
        "params": {
            "action": {"type": "str", "enum": ["ask", "record"], "required": False},
            "issue":  {"type": "str", "max": 80,                 "required": False},
        },
    },
    "tool_repair": {
        "description": "Initiate a relationship-repair interaction",
        "params": {
            "context": {"type": "str", "max": 200, "required": False},
        },
    },

    # --- Google Workspace tools ---
    "tool_gws_calendar": {
        "description": "Read Google Calendar events (today / next / week)",
        "params": {
            "action": {
                "type": "str",
                "enum": ["today", "next", "week"],
                "required": False,
            },
            "calendar_id": {"type": "str", "max": 200, "required": False},
        },
    },
    "tool_gws_sheets_log": {
        "description": "Log an event to the Google Sheets activity log",
        "params": {
            "event_type": {"type": "str", "max": 40,  "required": False},
            "detail":     {"type": "str", "max": 200, "required": False},
            "mood":       {"type": "str", "max": 40,  "required": False},
            "notes":      {"type": "str", "max": 500, "required": False},
        },
    },

    # --- Cognitive tools ---
    "tool_research": {
        "description": "Research a topic using Claude and web search",
        "params": {
            "query": {"type": "str", "min": 5, "max": 500, "required": True},
        },
    },
    "tool_compose": {
        "description": "Compose a creative or factual piece on a topic",
        "params": {
            "topic": {"type": "str", "min": 3, "max": 500, "required": True},
        },
    },
    "tool_blog": {
        "description": "Write a blog post on a given topic",
        "params": {
            "topic": {"type": "str", "min": 5, "max": 500, "required": True},
        },
    },
    "tool_story": {
        "description": "Start, add to, read, or finish a collaborative story",
        "params": {
            "action": {
                "type": "str",
                "enum": ["start", "add", "read", "finish"],
                "required": False,
            },
            "text": {"type": "str", "max": 500, "required": False},
        },
    },
}
