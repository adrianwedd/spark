# Agent Operations Guide

## Voice Automation Flow
- Use `bin/run-voice-loop` to launch the Codex supervisor. By default it streams prompts through `codex exec --full-auto -`; override `CODEX_CHAT_CMD` before launch if you need a different model or options.
- The loop reads `state/session.json` for context. Flip `listening: true` with `bin/px-wake --set on` (or `--keyboard`) before speaking; the loop idles until that flag is raised.
- Audio feedback is produced through `tool-voice` (`espeak` fallback). Logs are appended to `logs/tool-voice-loop.log` and `logs/tool-voice-transcript.log`. Inspect quick stats with `bin/px-voice-report --json`.

## Diagnostics & Safety
- Run `bin/px-diagnostics` at the start of a session. It narrates every check (status, sensors, weather, circle motion unless `--no-motion`, camera capture, speaker/microphone tests) and records a JSON summary in `logs/tool-diagnostics.log`.
- Keep wheels on blocks for live runs; dry-run (`PX_DRY=1`) skips motion and still plays the spoken announcements so you can verify the speaker.
- `bin/px-stop` remains the emergency halt; it is safe to call repeatedly.

## Automation Toolbox
- `bin/px-dance` performs a narrated demo routine (voice intro → circle → figure-eight → finale). Use `PX_DRY=1` to rehearse without motion.
- `bin/px-frigate-stream` pushes an RTSP feed (`rpicam-vid` → `ffmpeg`) to Frigate/go2rtc (`pi5-hailo.local` by default). Test first with `--dry-run` to confirm command lines.
- `bin/px-session` bootstraps a tmux workspace (voice loop, wake console, log tail). `--plan` prints the layout before launching.

## Development Workflow
1. Activate the virtualenv: `source .venv/bin/activate`.
2. Implement helpers under `bin/` and keep logic in Python for easier testing.
3. Add or update pytest coverage in `tests/`; set `PX_BYPASS_SUDO=1` and `LOG_DIR=logs_test` (relative paths resolve under `PROJECT_ROOT`) in the test environment to avoid privileged operations.
4. Run `python -m pytest` before every commit (current suite covers voice tools, diagnostics, tmux plan, and streaming helpers).
5. Update documentation (`README.md`, `docs/TOOLS.md`, roadmap/strategy docs) alongside new features so operators have fresh instructions.

## Lessons Learned
- Treat every helper as a modular tool Codex can invoke; build consistent JSON outputs and summaries to keep transcripts clean.
- State persistence is critical: always update `state/session.json` when a tool runs so the next Codex turn has context.
- Keep audio pathways live even in dry-run; it surfaced a muted speaker regression immediately.
- Use structured logging (`logs/tool-voice-transcript.log`, `logs/tool-*.log`) to audit behaviour and drive reporting tools.
- Leverage tmux (`bin/px-session`) during development to survive SSH drops and keep wake/log panes visible.
