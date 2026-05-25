# HANDOFF — 2026-05-20

## This session

Mostly health checks — SPARK is running well. Two commits landed before this session:

- `119e51e` — network-outage resilience: host failure cache + HA offline flag in `mind.py`
- `d70403e` — README test count correction

## Changes committed this session

`a9e997b` — two decisions made and committed clean:

- **LLM tier reorder** (`mind.py`): M5 Ollama is now primary for all personas including SPARK. Claude Haiku is the fallback when M5 is unreachable. Use `PX_MIND_BACKEND=claude` to force Claude primary.
- **Expression cooldown** (`spark_config.py`): raised from 120s → 1800s. One spontaneous utterance per 30 min max.

## System status

| Check | Value |
|---|---|
| Health | OK |
| Mood | content (salience 0.85–0.95) |
| Feed posts | 100 (posting actively) |
| Battery | 96%, not charging |
| CPU temp | 53–58°C |
| Disk | 78% — worth watching |
| Claude budget | 3 used / 8 today |

## Open issues updated this session

- **#36** (MCP server): Phase 1 done. Layers 2–3 not started.
- **#25** (SPARK persona): Cognitive loop solid. Obi-facing interaction layer is the remaining gap.

## Next session candidates

### High value / low effort
- **Commit the pending changes** (or make the decisions above and commit clean)
- **Disk cleanup** — 78% and climbing; `state/thought-images/` has 30-day TTL but check if anything else is accumulating
- **Expression cooldown tuning** — live with 30 min for a day and see how it feels

### Medium effort
- **Issue #36 Layer 2** — stateful conversation buffer. Rolling 10-turn window injected into `build_model_prompt()`. Would fix web UI chat statefulness too. ~2–3h.
- **Issue #25 Obi layer** — proximity greet routine, one-question-at-a-time interaction mode, named persona. Needs design before code.

### Bigger bets
- **Issue #31** (face follow) — prerequisite for gaze-toward-Obi on approach
- **Issue #36 Layer 3** (autonomous agent) — blocked on Layer 2
