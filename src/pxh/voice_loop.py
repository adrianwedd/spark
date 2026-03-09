from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .logging import log_event
from .state import load_session, update_session, ensure_session
from .time import utc_timestamp

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BIN_DIR = PROJECT_ROOT / "bin"

ALLOWED_TOOLS = {
    "tool_status",
    "tool_circle",
    "tool_figure8",
    "tool_stop",
    "tool_voice",
    "tool_weather",
    "tool_look",
    "tool_emote",
    "tool_sonar",
    "tool_perform",
    "tool_drive",
    "tool_time",
    "tool_remember",
    "tool_recall",
    "tool_photograph",
    "tool_qa",
    "tool_play_sound",
    "tool_face",
    "tool_describe_scene",
    "tool_wander",
    "tool_timer",
    "tool_api_start",
    "tool_api_stop",
    "tool_chat",
    "tool_chat_vixen",
}

TOOL_COMMANDS = {
    "tool_status":         BIN_DIR / "tool-status",
    "tool_circle":         BIN_DIR / "tool-circle",
    "tool_figure8":        BIN_DIR / "tool-figure8",
    "tool_stop":           BIN_DIR / "tool-stop",
    "tool_voice":          BIN_DIR / "tool-voice",
    "tool_weather":        BIN_DIR / "tool-weather",
    "tool_look":           BIN_DIR / "tool-look",
    "tool_emote":          BIN_DIR / "tool-emote",
    "tool_sonar":          BIN_DIR / "tool-sonar",
    "tool_perform":        BIN_DIR / "tool-perform",
    "tool_drive":          BIN_DIR / "tool-drive",
    "tool_time":           BIN_DIR / "tool-time",
    "tool_remember":       BIN_DIR / "tool-remember",
    "tool_recall":         BIN_DIR / "tool-recall",
    "tool_photograph":     BIN_DIR / "tool-photograph",
    "tool_qa":             BIN_DIR / "tool-qa",
    "tool_play_sound":     BIN_DIR / "tool-play-sound",
    "tool_face":           BIN_DIR / "tool-face",
    "tool_describe_scene": BIN_DIR / "tool-describe-scene",
    "tool_wander":         BIN_DIR / "tool-wander",
    "tool_timer":          BIN_DIR / "tool-timer",
    "tool_api_start":     BIN_DIR / "tool-api-start",
    "tool_api_stop":      BIN_DIR / "tool-api-stop",
    "tool_chat":          BIN_DIR / "tool-chat",
    "tool_chat_vixen":    BIN_DIR / "tool-chat-vixen",
}


# Persona voice settings — injected into tool env when persona is active
# Persona prompt files — used instead of default system prompt when persona active
PERSONA_PROMPTS = {
    "vixen": PROJECT_ROOT / "docs" / "prompts" / "persona-vixen.md",
    "gremlin": PROJECT_ROOT / "docs" / "prompts" / "persona-gremlin.md",
    "spark": PROJECT_ROOT / "docs" / "prompts" / "spark-voice-system.md",
}

PERSONA_VOICE_ENV = {
    "vixen": {
        "PX_PERSONA": "vixen",
        "PX_VOICE_VARIANT": "en+f4",
        "PX_VOICE_PITCH": "72",
        "PX_VOICE_RATE": "135",
    },
    "gremlin": {
        "PX_PERSONA": "gremlin",
        "PX_VOICE_VARIANT": "en+croak",
        "PX_VOICE_PITCH": "20",
        "PX_VOICE_RATE": "180",
    },
    "spark": {
        "PX_PERSONA": "spark",
        "PX_VOICE_VARIANT": "en+m3",
        "PX_VOICE_PITCH": "58",
        "PX_VOICE_RATE": "120",
    },
}


class VoiceLoopError(Exception):
    """Domain-specific errors."""


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def watchdog_thread_func(heartbeat_q: queue.Queue, timeout: float) -> None:
    """Monitors a queue for heartbeats and exits if they become stale."""
    last_heartbeat = time.monotonic()
    while True:
        try:
            last_heartbeat = heartbeat_q.get_nowait()
        except queue.Empty:
            pass

        stale_time = time.monotonic() - last_heartbeat
        if stale_time > timeout:
            log_event(
                "voice-watchdog",
                {
                    "status": "stale",
                    "age_seconds": stale_time,
                    "threshold_seconds": timeout,
                    "message": "Watchdog timeout exceeded. Forcing process exit.",
                },
            )
            # Use os._exit for an immediate, hard exit that bypasses finally blocks.
            os._exit(1)
        time.sleep(timeout / 4)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Codex-driven PiCar-X voice assistant loop")
    parser.add_argument(
        "--prompt",
        default=str(PROJECT_ROOT / "docs/prompts/codex-voice-system.md"),
        help="Path to the system prompt file",
    )
    parser.add_argument(
        "--input-mode",
        choices=["text", "voice"],
        default="text",
        help="How to capture user input (default: text)",
    )
    parser.add_argument(
        "--transcriber-cmd",
        help="Command used to transcribe microphone input when --input-mode=voice",
    )
    parser.add_argument(
        "--codex-cmd",
        default=os.environ.get("CODEX_CHAT_CMD", "codex exec --model gpt-5-codex --full-auto -"),
        help="Command used to invoke the Codex CLI",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=50,
        help="Maximum conversation turns before exiting",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Force PX_DRY=1 for all tool executions",
    )
    parser.add_argument(
        "--auto-log",
        action="store_true",
        help="Append full Codex responses to logs/voice-loop.log for auditing",
    )
    parser.add_argument(
        "--exit-on-stop",
        action="store_true",
        help="Exit loop immediately after a successful tool_stop call",
    )
    parser.add_argument(
        "--watchdog-timeout",
        type=float,
        default=float(os.environ.get("PX_WATCHDOG_STALE_SECONDS", "30.0")),
        help="Seconds of inactivity before the watchdog forces an exit.",
    )
    return parser.parse_args(argv)


def read_prompt(path: Path) -> str:
    if not path.exists():
        raise VoiceLoopError(f"prompt file missing: {path}")
    return path.read_text(encoding="utf-8").strip()


def capture_text_input() -> Optional[str]:
    try:
        line = input("You> ").strip()
    except EOFError:
        # Stdin exhausted (piped single utterance). Return a continuation sentinel
        # so the model can follow up on its last tool call (e.g. speak weather result).
        return "(continue)"
    if not line:
        return None
    return line


def capture_voice_input(cmd_spec: str) -> Optional[str]:
    if not cmd_spec:
        raise VoiceLoopError("voice mode requested but --transcriber-cmd not provided")

    if any(token in cmd_spec for token in ("|", ";", "&&", "||", ">", "<")):
        raise VoiceLoopError(
            "shell pipelines are not allowed in --transcriber-cmd for security reasons. "
            "Please create a wrapper script in the bin/ directory."
        )

    command = shlex.split(cmd_spec)
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        errors="ignore",
    )
    if result.returncode != 0:
        raise VoiceLoopError(
            f"transcription failed (rc={result.returncode}): {result.stderr.strip()}"
        )
    text_result = result.stdout.strip()
    return text_result or None


def build_model_prompt(system_prompt: str, state: Dict[str, Any], user_text: str) -> str:
    state_copy = {k: v for k, v in state.items() if k != "history"}

    highlights: Dict[str, Any] = {}
    for key in (
        "mode",
        "confirm_motion_allowed",
        "wheels_on_blocks",
        "battery_pct",
        "battery_ok",
        "last_motion",
        "last_action",
    ):
        value = state_copy.get(key)
        if value is not None:
            highlights[key] = value

    last_weather = state_copy.get("last_weather") or {}
    if isinstance(last_weather, dict):
        summary = last_weather.get("summary")
        if summary:
            highlights["last_weather_summary"] = summary

    recent_history = state.get("history") or []
    recent_events = recent_history[-3:]

    context_sections = [
        "Current highlights:",
        json.dumps(highlights, indent=2),
    ]
    if recent_events:
        context_sections.append("Recent events:")
        context_sections.append(json.dumps(recent_events, indent=2))

    # Inject inner thoughts from px-mind (if running)
    thoughts_file = Path(os.environ.get("PX_STATE_DIR", str(PROJECT_ROOT / "state"))) / "thoughts.jsonl"
    if thoughts_file.exists():
        try:
            lines = thoughts_file.read_text(encoding="utf-8").strip().splitlines()
            recent_thoughts = []
            for line in lines[-3:]:
                try:
                    t = json.loads(line)
                    recent_thoughts.append({
                        "thought": t.get("thought", ""),
                        "mood": t.get("mood", ""),
                    })
                except json.JSONDecodeError:
                    continue
            if recent_thoughts:
                context_sections.append("Robot's recent inner thoughts:")
                context_sections.append(json.dumps(recent_thoughts, indent=2))
                last_mood = recent_thoughts[-1].get("mood")
                if last_mood:
                    context_sections.append(f"Current mood: {last_mood}")
        except Exception:
            pass

    context_block = "\n".join(context_sections)

    return (
        f"{system_prompt}\n\n"
        f"{context_block}\n"
        f"User transcript: {user_text}\n"
        f"Respond with a single JSON object as instructed."
    )


def run_codex(command_spec: str, prompt: str) -> Tuple[int, str, str]:
    command = shlex.split(command_spec)
    result = subprocess.run(
        command,
        input=prompt,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stdout, result.stderr


def extract_action(text: str) -> Optional[Dict[str, Any]]:
    # Fast path: scan lines in reverse for a single-line JSON object
    for line in reversed(text.strip().splitlines()):
        candidate = line.strip()
        if candidate.startswith("{") and candidate.endswith("}"):
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    # Fallback: find the last valid JSON object that may span multiple lines
    decoder = json.JSONDecoder()
    last_obj: Optional[Dict[str, Any]] = None
    pos = 0
    while pos < len(text):
        idx = text.find("{", pos)
        if idx == -1:
            break
        try:
            obj, end = decoder.raw_decode(text, idx)
            if isinstance(obj, dict):
                last_obj = obj
            pos = end
        except json.JSONDecodeError:
            pos = idx + 1
    return last_obj


def parse_tool_payload(raw: str) -> Optional[Dict[str, Any]]:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return json.loads(raw.splitlines()[-1])
    except json.JSONDecodeError:
        return None

def _num(value: Any, name: str) -> float:
    """Convert param to float, raising VoiceLoopError on bad input."""
    try:
        return float(value)
    except (ValueError, TypeError):
        raise VoiceLoopError(f"invalid numeric value for {name}: {value!r}")


def validate_action(action: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    tool = action.get("tool")
    if tool not in ALLOWED_TOOLS:
        raise VoiceLoopError(f"unsupported tool requested: {tool}")

    params = action.get("params", {})
    sanitized: Dict[str, Any] = {}

    if tool in ("tool_status", "tool_stop", "tool_weather"):
        pass  # no params required
    elif tool == "tool_circle":
        speed = int(_num(params.get("speed", 30), "speed"))
        duration = _num(params.get("duration", 6), "duration")
        if not (0 <= speed <= 60):
            raise VoiceLoopError("tool_circle speed out of range")
        if not (1 <= duration <= 12):
            raise VoiceLoopError("tool_circle duration out of range")
        sanitized["PX_SPEED"] = str(speed)
        sanitized["PX_DURATION"] = f"{duration:.2f}"
    elif tool == "tool_figure8":
        speed = int(_num(params.get("speed", 30), "speed"))
        duration = _num(params.get("duration", 6), "duration")
        rest = _num(params.get("rest", 1.5), "rest")
        if not (0 <= speed <= 60):
            raise VoiceLoopError("tool_figure8 speed out of range")
        if not (1 <= duration <= 12):
            raise VoiceLoopError("tool_figure8 duration out of range")
        if not (0 <= rest <= 5):
            raise VoiceLoopError("tool_figure8 rest out of range")
        sanitized["PX_SPEED"] = str(speed)
        sanitized["PX_DURATION"] = f"{duration:.2f}"
        sanitized["PX_REST"] = f"{rest:.2f}"
    elif tool == "tool_voice":
        text = params.get("text")
        if not isinstance(text, str) or not text.strip():
            raise VoiceLoopError("tool_voice requires a non-empty text parameter")
        if len(text) > 2000:
            text = text[:2000]
        sanitized["PX_TEXT"] = text
    elif tool == "tool_look":
        pan  = int(clamp(_num(params.get("pan",  0), "pan"), -90, 90))
        tilt = int(clamp(_num(params.get("tilt", 0), "tilt"), -35, 65))
        ease = clamp(_num(params.get("ease", 0.8), "ease"), 0.1, 5.0)
        sanitized["PX_PAN"]  = str(pan)
        sanitized["PX_TILT"] = str(tilt)
        sanitized["PX_EASE"] = f"{ease:.2f}"
    elif tool == "tool_emote":
        valid = {"idle", "curious", "thinking", "happy", "alert", "excited", "sad", "shy"}
        name = str(params.get("name", "idle")).lower()
        if name not in valid:
            raise VoiceLoopError(f"unknown emote '{name}'; valid: {sorted(valid)}")
        sanitized["PX_EMOTE"] = name
    elif tool == "tool_sonar":
        pass  # no params required
    elif tool == "tool_perform":
        steps = params.get("steps")
        if not isinstance(steps, list) or not steps:
            raise VoiceLoopError("tool_perform requires a non-empty 'steps' list")
        if len(steps) > 12:
            steps = steps[:12]
        for step in steps:
            if not isinstance(step, dict):
                raise VoiceLoopError("each perform step must be a JSON object")
            if "speak" in step and len(str(step["speak"])) > 200:
                step["speak"] = str(step["speak"])[:200]
        sanitized["PX_PERFORM_STEPS"] = json.dumps(steps)
    elif tool == "tool_drive":
        direction = str(params.get("direction", "forward")).lower()
        if direction not in ("forward", "backward"):
            raise VoiceLoopError(f"tool_drive direction must be 'forward' or 'backward'")
        speed    = int(clamp(_num(params.get("speed",    30), "speed"),  0,   60))
        duration = clamp(_num(params.get("duration", 1.0), "duration"),     0.1, 10.0)
        steer    = int(clamp(_num(params.get("steer",     0), "steer"), -35,  35))
        sanitized["PX_DIRECTION"] = direction
        sanitized["PX_SPEED"]     = str(speed)
        sanitized["PX_DURATION"]  = f"{duration:.2f}"
        sanitized["PX_STEER"]     = str(steer)
    elif tool == "tool_time":
        pass  # no params required
    elif tool == "tool_remember":
        note = params.get("text") or params.get("note", "")
        if not isinstance(note, str) or not note.strip():
            raise VoiceLoopError("tool_remember requires a non-empty 'text' parameter")
        sanitized["PX_NOTE"] = note.strip()[:500]
    elif tool == "tool_recall":
        limit = int(clamp(_num(params.get("limit", 5), "limit"), 1, 20))
        sanitized["PX_RECALL_LIMIT"] = str(limit)
    elif tool == "tool_photograph":
        pass  # no required params; PX_PHOTO_PATH is optional
    elif tool == "tool_qa":
        text = params.get("text")
        if not isinstance(text, str) or not text.strip():
            raise VoiceLoopError("tool_qa requires a non-empty text parameter")
        if len(text) > 2000:
            text = text[:2000]
        sanitized["PX_TEXT"] = text
    elif tool == "tool_play_sound":
        name = str(params.get("name", "")).lower().strip()
        allowed = {"chime", "beep", "tada", "alert"}
        if name not in allowed:
            raise VoiceLoopError(f"unknown sound '{name}'; allowed: {sorted(allowed)}")
        sanitized["PX_SOUND"] = name
    elif tool == "tool_face":
        pass  # no params required
    elif tool == "tool_describe_scene":
        pass  # no params required
    elif tool == "tool_wander":
        steps = int(clamp(_num(params.get("steps", 5), "steps"), 1, 20))
        sanitized["PX_WANDER_STEPS"] = str(steps)
    elif tool == "tool_timer":
        seconds = int(clamp(_num(params.get("seconds", 60), "seconds"), 5, 3600))
        label   = str(params.get("label", ""))[:100]
        sanitized["PX_TIMER_SECONDS"] = str(seconds)
        sanitized["PX_TIMER_LABEL"]   = label
    elif tool in ("tool_chat", "tool_chat_vixen"):
        text = params.get("text")
        if not isinstance(text, str) or not text.strip():
            raise VoiceLoopError(f"{tool} requires a non-empty text parameter")
        if len(text) > 2000:
            text = text[:2000]
        sanitized["PX_TEXT"] = text
    elif tool in ("tool_api_start", "tool_api_stop"):
        pass  # no params required
    else:
        if params:
            raise VoiceLoopError("unexpected parameters for tool")

    return tool, sanitized


def execute_tool(tool: str, env_overrides: Dict[str, str], dry_mode: bool) -> Tuple[int, str, str]:
    command_path = TOOL_COMMANDS[tool]
    if not command_path.exists():
        raise VoiceLoopError(f"tool command missing: {command_path}")

    env = os.environ.copy()
    env.setdefault("PROJECT_ROOT", str(PROJECT_ROOT))
    env_overrides = dict(env_overrides)
    env_overrides.pop("PX_DRY", None)  # never allow model to control PX_DRY
    if dry_mode:
        env["PX_DRY"] = "1"
    # else: leave PX_DRY as inherited from the operator's environment
    for key, value in env_overrides.items():
        env[key] = value
    # Inject persona voice settings if a persona is active in session
    session_persona = load_session().get("persona") or ""
    if session_persona and session_persona in PERSONA_VOICE_ENV:
        for k, v in PERSONA_VOICE_ENV[session_persona].items():
            env[k] = v

    result = subprocess.run(
        [str(command_path)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    return result.returncode, result.stdout, result.stderr


def supervisor_loop(args: argparse.Namespace) -> None:
    ensure_session()
    system_prompt = read_prompt(Path(args.prompt))

    heartbeat_q: queue.Queue = queue.Queue()
    if args.input_mode != "text":
        watchdog = threading.Thread(
            target=watchdog_thread_func, args=(heartbeat_q, args.watchdog_timeout), daemon=True
        )
        watchdog.start()

    turn = 0
    while turn < args.max_turns:
        heartbeat_q.put(time.monotonic())
        session = load_session()

        listening_enabled = session.get("listening", False)
        if args.input_mode == "voice" and not listening_enabled:
            time.sleep(float(os.environ.get("PX_LISTEN_IDLE_SLEEP", "0.5")))
            continue

        turn += 1
        heartbeat_q.put(time.monotonic())
        if args.input_mode == "text":
            user_text = capture_text_input()
        else:
            user_text = capture_voice_input(args.transcriber_cmd)

        if not user_text:
            print("[voice-loop] No input, exiting.")
            break

        heartbeat_q.put(time.monotonic())
        # Use persona prompt if one is active in session
        active_persona = (session.get("persona") or "").lower().strip()
        if active_persona and active_persona in PERSONA_PROMPTS:
            persona_prompt_path = PERSONA_PROMPTS[active_persona]
            if persona_prompt_path.exists():
                current_prompt = read_prompt(persona_prompt_path)
            else:
                current_prompt = system_prompt
        else:
            current_prompt = system_prompt
        prompt = build_model_prompt(current_prompt, session, user_text)
        prompt_excerpt = prompt[:800]

        heartbeat_q.put(time.monotonic())
        rc, stdout, stderr = run_codex(args.codex_cmd, prompt)
        heartbeat_q.put(time.monotonic())

        if args.auto_log:
            log_event(
                "voice-loop",
                {
                    "turn": turn,
                    "model_rc": rc,
                    "stdout": stdout[-4000:],
                    "stderr": stderr[-4000:],
                },
            )

        if rc != 0:
            print(f"[voice-loop] Codex CLI exited with {rc}: {stderr.strip()}")
            continue

        action = extract_action(stdout)
        if not action:
            print("[voice-loop] No JSON action detected; ignoring response.")
            continue

        try:
            tool, env_overrides = validate_action(action)
        except VoiceLoopError as exc:
            print(f"[voice-loop] Invalid action: {exc}")
            continue

        heartbeat_q.put(time.monotonic())
        try:
            rc_tool, tool_stdout, tool_stderr = execute_tool(tool, env_overrides, args.dry_run)
        except VoiceLoopError as exc:
            print(f"[voice-loop] Execution error: {exc}")
            continue
        heartbeat_q.put(time.monotonic())

        tool_payload = parse_tool_payload(tool_stdout)
        session_update = {
            "last_prompt_excerpt": prompt_excerpt,
            "last_model_action": action,
            "watchdog_heartbeat_ts": utc_timestamp(),
        }
        if isinstance(tool_payload, dict):
            session_update["last_tool_payload"] = tool_payload
        if args.input_mode == "voice":
            session_update.update({"listening": False, "listening_since": None})

        update_session(fields=session_update)

        transcript_entry = {
            "turn": turn,
            "prompt_excerpt": prompt_excerpt,
            "model_action": action,
            "tool": tool,
            "returncode": rc_tool,
            "dry": args.dry_run,
            "tool_stdout": tool_stdout[-1000:],
            "tool_stderr": tool_stderr[-1000:],
        }
        if isinstance(tool_payload, dict):
            transcript_entry["tool_payload"] = tool_payload

        log_event(
            "voice-loop",
            {
                "turn": turn,
                "tool": tool,
                "returncode": rc_tool,
                "dry": args.dry_run,
            },
        )

        if tool_stdout.strip():
            print(tool_stdout.strip())
        if tool_stderr.strip():
            print(tool_stderr.strip(), file=sys.stderr)

        voice_result = None
        if tool == "tool_weather":
            summary = tool_payload.get("summary") if isinstance(tool_payload, dict) else None
            if summary:
                heartbeat_q.put(time.monotonic())
                try:
                    rc_voice, voice_stdout, voice_stderr = execute_tool(
                        "tool_voice",
                        {"PX_TEXT": summary},
                        args.dry_run,
                    )
                except VoiceLoopError as exc:
                    print(f"[voice-loop] Voice execution error: {exc}")
                else:
                    log_event(
                        "voice-loop",
                        {"turn": turn, "tool": "tool_voice", "returncode": rc_voice, "dry": args.dry_run},
                    )
                    if voice_stdout.strip():
                        print(voice_stdout.strip())
                    if voice_stderr.strip():
                        print(voice_stderr.strip(), file=sys.stderr)
                    voice_result = {
                        "returncode": rc_voice,
                        "stdout": voice_stdout[-1000:],
                        "stderr": voice_stderr[-1000:],
                    }
                heartbeat_q.put(time.monotonic())

        if voice_result is not None:
            transcript_entry["voice_result"] = voice_result

        log_event("voice-transcript", transcript_entry)

        if args.exit_on_stop and tool == "tool_stop" and rc_tool == 0:
            print("[voice-loop] Stop command acknowledged. Exiting loop.")
            break


def main(argv: Optional[list[str]] = None) -> int:
    try:
        args = parse_args(argv)
        supervisor_loop(args)
        return 0
    except VoiceLoopError as exc:
        print(f"voice-loop error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())