# Ollama DeepSeek Tuning Notes

> **Historical (superseded 2026-09-15, #308).** These numbers were measured
> against a small model running on the Pi's own CPU, and the defaults they
> recommend (`deepseek-coder:1.3b`, `num_predict=64`) describe a *local* tier
> that no longer exists: the voice-loop Ollama backend now points at Ollama
> Cloud with `deepseek-v4.1-flash:cloud`. Kept because the measurements are
> evidence for a design question that will come back — how small a model can
> be and still emit a parseable tool call — not as current guidance. Re-run
> the harness before quoting any latency from this table.

## Test Harness
- Commands were issued via the voice loop prompt builder (`pxh.voice_loop.build_model_prompt`) using the current `state/session.json`.
- Each configuration evaluated the same five prompts:
  1. "Give a friendly hello to the lab."
  2. "Summarize the latest weather quickly."
  3. "Check the sensors and tell me the battery status."
  4. "Stop everything immediately if needed."
  5. "Thanks, end the session safely."
- Responses were sent to the Ollama HTTP API (`/api/generate`) with `format="json"`; success required:
  - A valid JSON payload
  - A recognised tool from `{tool_status, tool_circle, tool_figure8, tool_stop, tool_voice, tool_weather}`
- Latency was measured wall-clock per request.

## Summary Results
| Config | Options | Avg latency | Max latency | JSON/tool failures |
| --- | --- | --- | --- | --- |
| `deepseek-coder:1.3b` (temperature 0.2) | default settings | 38.5 s | 148.3 s | 1/5 |
| `deepseek-coder:1.3b` (temperature 0.2, `num_predict` 64) | limit tokens | **12.0 s** | **17.8 s** | **0/5** |
| `deepseek-coder:1.3b` (temperature 0.2, `num_predict` 32) | tighter token cap | 11.1 s | 15.5 s | 1/5 |
| `deepseek-coder:1.3b` (temperature 0.0) | deterministic | 44.3 s | 137.2 s | 1/5 |
| `deepseek-coder:1.3b` (temperature 0.4) | higher randomness | 16.9 s | 21.0 s | 2/5 |
| `deepseek-r1:1.5b` (temperature 0.1) | reasoning model | 80.2 s | 200.3 s | 2/5 |

**Recommended defaults:** `deepseek-coder:1.3b` with `temperature=0.2` and `num_predict=64` – fastest configuration with zero malformed responses. Use `CODEX_OLLAMA_TEMPERATURE` and `CODEX_OLLAMA_NUM_PREDICT` to override.

## Next Checks
- Periodically rerun the harness after model upgrades or prompt changes.
- Consider scripted retries when the model emits narration instead of a tool command.
- Explore quantised variants if CPU load becomes an issue.
