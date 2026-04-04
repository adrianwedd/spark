# Layer 3: Expression Architecture

The expression layer is the final stage of SPARK's three-layer cognitive loop. It receives a thought dict from Layer 2 (Reflection) and dispatches a concrete action -- speech, motion, memory write, or tool invocation. The entry point is `expression()` at line 2518 of `src/pxh/mind.py`.

## 1. Action Taxonomy

All valid actions are defined in `VALID_ACTIONS` (line 366 of `mind.py`). There are 21 actions grouped into 8 categories:

### Speech

| Action | Tool invoked | Behaviour |
|--------|-------------|-----------|
| `greet` | `tool-voice` | Speaks the thought text (up to 2000 chars). Updates `_last_spoken_text`. |
| `comment` | `tool-voice` | Same as greet -- speaks thought text. Most common speech action. |
| `weather_comment` | `tool-weather` then `tool-voice` | Fetches fresh weather via `fetch_weather()`, speaks the summary. Marked as needing Ollama persona rephrase (`needs_rephrase = True`). |
| `morning_fact` | `tool-voice` | Once-per-day guard via `_last_morning_fact_date`. Skips if already spoken today (Hobart date). |

### GPIO / Motion

| Action | Tool invoked | Behaviour |
|--------|-------------|-----------|
| `scan` | `tool-voice` | Speaks the thought text only -- Layer 1 awareness already reads sonar every 60s. No servo movement. |
| `look_at` | `tool-voice` | Speaks thought text. Physical servo movement delegated to px-alive, not px-mind. |
| `look_around` | `tool-look` then `tool-voice` | Moves to random pan (-40..40) and tilt (-10..30), then speaks if text present. |
| `explore` | `tool-wander` | Full autonomous exploration. Calls `yield_alive`, waits for px-alive exit, runs `tool-wander` in explore mode (180s, 20 steps) via `Popen` with SIGTERM-first timeout (240s hard cap). Posts a follow-up thought. Restarts px-alive afterward. |
| `emote` | `tool-emote` | Maps mood to emote name via `MOOD_TO_EMOTE` (from `spark_config.py`). |

### Audio

| Action | Tool invoked | Behaviour |
|--------|-------------|-----------|
| `play_sound` | `tool-play-sound` | Maps mood to sound name via `MOOD_TO_SOUND` (from `spark_config.py`). |

### Camera

| Action | Tool invoked | Behaviour |
|--------|-------------|-----------|
| `photograph` | `tool-describe-scene` | Runs via `Popen` with 120s timeout, SIGTERM-first cleanup. |

### Memory

| Action | Tool invoked | Behaviour |
|--------|-------------|-----------|
| `remember` | `tool-remember` | Writes thought text (up to 500 chars) to `notes.jsonl` via `PX_NOTE` env var. 10s timeout. |

### Clock

| Action | Tool invoked | Behaviour |
|--------|-------------|-----------|
| `time_check` | `tool-time` | Speaks current time. 15s timeout. |
| `calendar_check` | `tool-gws-calendar` | Fetches next calendar event. Uses `PX_CALENDAR_ID` (default: `obiwedd@gmail.com`). 60s timeout. |

### Autonomy (Claude-powered)

| Action | Tool invoked | Behaviour |
|--------|-------------|-----------|
| `introspect` | `tool-introspect` | Computes thought statistics. 30s timeout. |
| `evolve` | `tool-evolve` | Queues a self-evolution proposal. Passes thought text as `PX_EVOLVE_INTENT`. 15s timeout. |
| `research` | `tool-research` | Haiku-powered deep dive. Passes thought text as `PX_RESEARCH_QUERY`. 360s timeout. |
| `compose` | `tool-compose` | Haiku-powered creative writing. Passes thought text as `PX_COMPOSE_TOPIC`. 360s timeout. |
| `blog_essay` | `tool-blog` | Writes a blog post. Passes thought text as `PX_BLOG_TOPIC`. 360s timeout. |
| `self_debug` | `run_claude_session()` | Sonnet with read-only tools (`Read,Glob,Grep`). Triggered by consecutive reflection failures. Saves to `state/debug_reports.jsonl`. 600s timeout. Not a subprocess tool -- calls `claude_session.run_claude_session()` directly. |

### No-op

| Action | Behaviour |
|--------|-----------|
| `wait` | Returns immediately. No tool invoked. |


## 2. Gating Matrix

Expression is subject to five independent gate checks, evaluated in order at the top of `expression()` (lines 2526-2573). If any gate fires, the action is suppressed with a log message and the function returns early.

### Gate 1: Absent-gated actions (`ABSENT_GATED_ACTIONS`, line 374)

Suppressed when `obi_mode` is `absent`, `at-school`, or `at-mums`.

Actions gated: `greet`, `comment`, `weather_comment`, `scan`, `play_sound`, `time_check`, `calendar_check`, `photograph`, `look_around`, `morning_fact`, `explore`, `research`, `compose`, `blog_essay`.

### Gate 2: Calendar-driven modes (lines 2546-2556)

Checked against `awareness.calendar.current_event` (case-insensitive substring match):

| Calendar event | Actions suppressed |
|----------------|-------------------|
| Contains "decompress" | `greet`, `comment`, `scan`, `calendar_check` |
| Contains "quiet time" | **All** actions (blanket suppress) |
| Contains "bedtime" | Everything except `wait` and `remember` |

### Gate 3: Adrian context (lines 2559-2564)

Suppressed when `awareness.ha_context.adrian_on_call` or `adrian_mic_active` is true.

Actions gated: `greet`, `comment`, `weather_comment`, `play_sound`, `time_check`, `calendar_check`, `photograph`.

### Gate 4: Charging-gated actions (`CHARGING_GATED_ACTIONS`, line 373)

Suppressed when battery is charging (read from `state/battery.json`).

Actions gated: `scan`, `look_at`, `explore`, `emote`, `look_around`, `calendar_check`.

### Gate 5: Global expression cooldown (line 3124)

The main loop enforces `EXPRESSION_COOLDOWN_S = 120` seconds (2 minutes) between any two expression calls. This is checked in the main loop *before* calling `expression()`, not inside it.

### Summary table

| Action | Absent-gated | Decompress | Quiet time | Bedtime | Adrian call | Charging-gated |
|--------|:---:|:---:|:---:|:---:|:---:|:---:|
| `wait` | - | - | - | - | - | - |
| `greet` | Y | Y | Y | Y | Y | - |
| `comment` | Y | Y | Y | Y | Y | - |
| `weather_comment` | Y | - | Y | Y | Y | - |
| `morning_fact` | Y | - | Y | Y | - | - |
| `scan` | Y | Y | Y | Y | - | Y |
| `look_at` | - | - | Y | Y | - | Y |
| `look_around` | Y | - | Y | Y | - | Y |
| `explore` | Y | - | Y | Y | - | Y |
| `emote` | - | - | Y | Y | - | Y |
| `play_sound` | Y | - | Y | Y | Y | - |
| `photograph` | Y | - | Y | Y | Y | - |
| `remember` | - | - | Y | - | - | - |
| `time_check` | Y | - | Y | Y | Y | - |
| `calendar_check` | Y | Y | Y | Y | Y | Y |
| `introspect` | - | - | Y | Y | - | - |
| `evolve` | - | - | Y | Y | - | - |
| `research` | Y | - | Y | Y | - | - |
| `compose` | Y | - | Y | Y | - | - |
| `blog_essay` | Y | - | Y | Y | - | - |
| `self_debug` | - | - | Y | Y | - | - |


## 3. Cooldown Rules

### Global cooldown

`EXPRESSION_COOLDOWN_S = 120` (defined in `spark_config.py`, line 14). Enforced in the main loop at line 3124 of `mind.py`. Any action (except `wait`) resets the cooldown timer. If an action arrives within 120 seconds of the previous expression, it is logged as "expression suppressed (cooldown)" and discarded.

### Per-action secondary cooldowns

| Action | Cooldown | Mechanism |
|--------|----------|-----------|
| `morning_fact` | Once per calendar day (Hobart timezone) | `_last_morning_fact_date` compared to `YYYY-MM-DD` (line 2624). Reset on daemon restart. |
| `explore` | 1200 seconds (20 minutes) | `_can_explore()` at line 1064 reads `state/exploration_meta.json` `last_explore_ts`. Persists across restarts. |
| `introspect` | 1800 seconds (30 minutes) | Enforced inside `tool-introspect` via `state/introspection.json` timestamp. |
| `evolve` | 86400 seconds (24 hours) | Enforced inside `tool-evolve` via `state/evolve_queue.jsonl` timestamps. Additionally rate-limited by `claude_session.py` (1/day Opus quota). |
| `research` | 7200 seconds (2 hours) | Enforced by `claude_session.py` research session cooldown. |
| `compose` | 14400 seconds (4 hours) | Enforced by `claude_session.py` compose session cooldown. |
| `self_debug` | 21600 seconds (6 hours) | Enforced by `claude_session.py` self_debug session cooldown. |

### Explore preconditions (`_can_explore()`, line 1064)

Beyond the 20-minute cooldown, exploration requires all of:
- `session.roaming_allowed == True`
- `session.confirm_motion_allowed == True`
- `session.wheels_on_blocks == False`
- `session.listening == False` (not in active conversation)
- Battery not charging
- Battery percentage > 20%


## 4. Persona Routing

Voice settings are injected into the tool subprocess environment based on the active persona in session state. The canonical source is `PERSONA_VOICE_ENV` in `src/pxh/voice_loop.py` (line 144), imported by `mind.py` at line 46.

### Persona voice settings

| Persona | `PX_VOICE_VARIANT` | `PX_VOICE_PITCH` | `PX_VOICE_RATE` |
|---------|-------------------|------------------|-----------------|
| `spark` | `en-gb` | 95 | 100 |
| `gremlin` | `en+croak` | 20 | 180 |
| `vixen` | `en+f4` | 72 | 135 |

### Injection points in expression()

1. **Lines 2588-2598**: Loads session, reads `persona` field. If the persona exists in `PERSONA_VOICE_ENV`, all env vars are injected.
2. **Rephrase skip** (line 2595): For most actions, sets `_PX_VOICE_PERSONA_DONE=1` to skip Ollama persona rephrasing (the reflection prompt already produces persona-voiced text). Exception: `weather_comment` needs rephrasing because it receives raw weather data.

### voice_loop.py injection

`execute_tool()` (line 705) independently reads the session persona and injects `PERSONA_VOICE_ENV` vars at line 733. This covers voice-loop-initiated tool calls (user commands), separate from px-mind expression.


## 5. Failure Modes

### Tool subprocess timeout

Most tools have explicit timeouts passed to `subprocess.run()`:

| Tool | Timeout |
|------|---------|
| `tool-voice` (via `_run_voice`) | 45s (default) |
| `tool-remember` | 10s |
| `tool-look`, `tool-emote`, `tool-play-sound`, `tool-time` | 15s |
| `tool-gws-calendar` | 60s |
| `tool-introspect` | 30s |
| `tool-evolve` | 15s |
| `tool-describe-scene` (photograph) | 120s |
| `tool-wander` (explore) | 240s |
| `tool-research`, `tool-compose`, `tool-blog` | 360s |
| `run_claude_session` (self_debug) | 600s |

Long-running tools (`explore`, `photograph`) use `Popen` with SIGTERM-first graceful shutdown: on `TimeoutExpired`, send `SIGTERM`, wait 15s, then `SIGKILL` if still alive. This is necessary because `subprocess.run(timeout=)` sends `SIGKILL` directly, which prevents cleanup (motor stop, servo reset).

### Voice lock contention

`_run_voice()` (line 2494) sets `PX_VOICE_LOCK_TIMEOUT=5` in the environment, so `tool-voice` will fail fast (5s) if another process holds the voice FileLock (`logs/voice.lock`). On contention, the error is logged as "voice contention -- voice.lock busy" and speech is silently dropped.

### I2C / GPIO errors

`expression()` wraps the entire action dispatch in a try/except (the function body from line 2600 onward). I2C failures in motion tools (`tool-look`, `tool-emote`) surface as non-zero return codes but do not crash px-mind. The explore action additionally handles `OSError` from its subprocess chain.

### Reflection failure cascade

When `_consecutive_reflection_failures` reaches 3 (the `REFLECTION_FAIL_WARN_THRESHOLD` at line 3010), px-mind:
1. Speaks a warning: "My thinking is offline -- all reflection backends are unreachable."
2. Sets `awareness.reflection_status = "offline"` (line 1827).
3. On the next successful reflection, resets the counter and clears the offline flag (line 3117).

`self_debug` action is triggered by the reflection prompt when failures accumulate, not by a hardcoded threshold in expression().

### Budget exhaustion

Claude-powered actions (`self_debug`) catch `SessionBudgetExhausted` (line 2903) and log a message without crashing. Tool-based Claude actions (`research`, `compose`, `blog_essay`, `evolve`) handle budget checks inside their respective tool scripts.


## 6. DRY Mode Semantics

When `--dry-run` is passed to px-mind (or `PX_DRY=1` in environment):

1. `expression()` sets `env["PX_DRY"] = "1"` for all tool subprocesses (line 2578).
2. **Motion tools** (`tool-look`, `tool-emote`, `tool-wander`): skip servo/motor commands, log what would have happened.
3. **Audio tools** (`tool-voice`, `tool-play-sound`): skip espeak/aplay, return success JSON.
4. **Tool output**: all tools still emit their standard JSON object to stdout, with status and any computed data.
5. **Claude-powered tools**: `tool-introspect` and `tool-evolve` respect `PX_DRY` (evolve writes a queue entry with `dry: true`).
6. **weather_comment**: `fetch_weather(dry=True)` returns a synthetic response: `{"temp_c": 20, "summary": "Dry-run: mild and clear."}`.

The gating logic (absent, charging, cooldown) runs identically in dry mode -- gates still suppress actions. This allows accurate testing of the gating matrix without hardware.

Note: `execute_tool()` in `voice_loop.py` (line 725) strips `PX_DRY` from model-provided params to prevent the LLM from controlling dry mode. The operator's environment or `--dry-run` flag is authoritative.


## 7. Integration Points

### voice_loop.py tool dispatch

`expression()` in `mind.py` runs tools via direct `subprocess.run()` / `Popen()` calls to `bin/tool-*` paths. It does **not** go through `voice_loop.execute_tool()`. The voice loop's `validate_action()` and `execute_tool()` are for user-initiated commands via the voice/text interface, not for autonomous expression.

Both paths inject `PERSONA_VOICE_ENV` independently: mind.py at line 2591, voice_loop.py at line 733.

### px-alive yield

The `explore` action (lines 2665-2692) performs a yield_alive handshake:
1. Sources `px-env` and calls `yield_alive` (sends SIGUSR1 to px-alive).
2. Polls `logs/px-alive.pid` for up to 5 seconds, checking `/proc/{pid}` liveness.
3. If px-alive is still running after 5s, aborts the exploration.
4. After exploration completes, checks `systemctl is-active px-alive` and restarts it if needed (line 2761).

### exploring.json lock

Long-running tools (`tool-wander`, `tool-describe-scene`) write `state/exploring.json` with `active: true` while running. px-alive checks this file and skips restart if exploration is active. This prevents servo contention during multi-minute operations. The explore action in `expression()` does not write `exploring.json` directly -- that is handled by the tool scripts themselves.

### State file writes

Expression writes to:
- `state/exploration_meta.json` -- `last_explore_ts` timestamp (explore action, line 2700)
- `state/debug_reports.jsonl` -- self_debug diagnostic reports (line 2898)
- `state/session.json` -- indirectly via `load_session()` for persona reads

### Main loop integration (line 3122-3128)

The main loop calls `expression()` only when:
1. A thought was produced by Layer 2 (reflection).
2. The thought's action is not `wait`.
3. At least `EXPRESSION_COOLDOWN_S` (120s) has elapsed since the last expression.

If the cooldown has not elapsed, the thought is logged as suppressed and discarded. There is no queue -- suppressed actions are lost.
