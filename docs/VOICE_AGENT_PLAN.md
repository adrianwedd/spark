# Codex Voice Agent Enhancement Plan

## Objectives
- Ensure every interaction with Codex includes full safety instructions, current robot state, and recent tool outcomes.
- Maintain a durable conversation context via `state/session.json` so Codex can reason about prior actions.
- Capture structured logs for prompts, Codex responses, tool invocations, and hardware outcomes to support audits.
- Keep the implementation modular so additional tools (keyboard recorder, REST bridge, etc.) can plug into the same loop.

## Components & Responsibilities

### 1. Prompt Builder (voice_loop)
- Assemble a per-turn prompt containing:
  1. Static system contract (tool list, JSON-only response rule, safety guardrails).
  2. Snapshot of `state/session.json` minus bulky history.
  3. Optional recent tool summaries (e.g., `last_weather.summary`).
  4. User transcript captured from voice/text input.
- Ensure prompts end with “Respond with a single JSON object as instructed.”

### 2. State Persistence (`pxh.state`)
- `update_session` should be called by every tool wrapper.
- Track at minimum:
  - `mode` (`live`/`dry-run`).
  - `confirm_motion_allowed`, `wheels_on_blocks`.
  - `battery_pct`, `battery_ok`, `last_weather`, `last_motion`, `last_action`.
  - Short `history` entries with timestamps, tool name, key parameters, and status.
- (Optional) add `last_prompt` / `last_response` if we want to store full transcripts; for now logs may suffice.

### 3. Tool Wrappers
- Continue producing structured JSON for stdout, trimmed to essential fields.
- Provide human-friendly `summary` text for Codex consumption (weather already does this).
- Update session fields promptly (e.g., `last_motion`, `last_weather`).
- Append to JSONL logs under `logs/tool-*.log`.

### 4. Logging & Audit
- `--auto-log` mode in `codex-voice-loop` should stay on by default; stores raw Codex stdout/stderr for each turn (`logs/tool-voice-loop.log`).
- Consider adding separate `logs/voice-transcript.log` (JSON lines) capturing `prompt`, `model_action`, and `tool_result` per turn.
- Ensure log rotation strategy later if needed (currently manual).

### 5. Codex CLI Integration
- Environment variable `CODEX_CHAT_CMD` to be set to the full CLI command (e.g., `codex exec --full-auto -`). The supervisor already pipes prompts through stdin.
- Create helper script (`bin/run-codex`) to wrap the command with appropriate environment exports for easier tmux startup.

### 6. Future Enhancements
- Wake-word/VAD front-end: run before transcription, write `listening` flag into session.
- Multi-tool command sequences: allow Codex to request more than one tool per turn by iterating on JSON array format (future work).
- Remote control/REST bridge: reuse session state and logging so external clients stay in sync.

## Implementation Steps
1. **Prompt Template**
   - Extract the static instruction block from `voice_loop` into `docs/prompts/codex-voice-system.md` (already done) and load it each turn.
   - Extend `build_model_prompt` to include recent summaries (e.g., weather, last motion).
2. **Session Schema**
   - Add any missing fields (e.g., `last_prompt_checksum` if needed) with defaults in `state/session.template.json`.
3. **Loop Logging**
   - Introduce `logs/voice-transcript.log` with JSON entries: `{prompt_excerpt, model_action, tool_stdout, tool_stderr}`.
4. **CLI Convenience**
   - Provide `bin/run-voice-loop` that exports `CODEX_CHAT_CMD` and launches tmux session.
5. **Testing Matrix**
   - Dry-run mode to verify prompts/actions.
   - Live mode on blocks for full hardware test.
   - Manual CLI invocation (`codex chat ...`) to validate prompt formatting outside the loop.
6. **Documentation**
   - Update README and `docs/TOOLS.md` with instructions on setting `CODEX_CHAT_CMD`, log locations, and state expectations.

Once this plan is in place, we can iterate on actual changes (prompt enrichment, new logs, wake word) step by step.
