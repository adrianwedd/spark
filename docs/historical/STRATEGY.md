> [!WARNING]
> **FOSSIL RECORD**
> This file describes the October-2025 Codex-voice / explicit-confirmation-gate strategy and is preserved for historical context only. It does not reflect the current operational strategy.

# PiCar-X Voice Agent Strategy

## 1. Safety and Trustworthiness
- Maintain mechanical safety defaults: wheels on blocks unless explicitly cleared, `tool-stop` always available, and battery/ultrasonic sanity checks before motion. 
- Persist full context in `state/session.json` and log every Codex turn (`logs/tool-voice-transcript.log`, `logs/tool-voice-loop.log`) so actions can be audited or replayed. 
- Keep a clean dry-run path for every helper to rehearse commands, and ensure tests simulate workflows without hardware or sudo requirements.

## 2. Voice-Driven Operations
- Deliver a speech-first experience: Codex ingests spoken transcripts, reasons over state, and responds with human-like narration (`tool_voice`).
- Near-term enhancements: wake-word/VAD front-end to gate transcription, “listening” indicators, and quick safety prompts ("wheels on blocks?").

## 3. Composable Automation Layer
- Treat each helper (`tool-*`) as a modular capability Codex can chain: status checks, circle/figure-eight runs, weather reporting, scans.
- Expand state summaries (e.g., last weather, last motion, battery trend) so Codex can plan longer sequences without losing context.

## 4. Remote Visibility & Control
- Build toward remote dashboards or REST bridges using the existing logging/state infrastructure: easy to plug in a web UI or CLI wrappers.
- Keep documentation (`README`, `docs/TOOLS.md`, `docs/ROADMAP.md`) synchronized so operators know the exact safety checklist, helper usage, and telemetry locations.

## 5. Scalable Experimentation
- Preserve modularity and pytest coverage, enabling quick iteration on new ideas (keyboard recorder, tmux orchestrators, regression suites).
- When enabling new powers (web search, external APIs), always surface results in state/logs so they are auditable and reversible.

### Near-Term Focus
1. Wake-word detection & VAD integration.
2. Enhanced transcript analytics (summaries, alerting for notable events).
3. Remote/tmux orchestration to survive SSH disconnects.

### Longer-Term Vision
- Natural-language “mission plans” executed by Codex with explicit confirmation gates.
- REST/WebSocket bridge for telemetry streaming and remote stop controls.
- Autonomous behaviours layered atop the existing toolset with strong guardrails.
