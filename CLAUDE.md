# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Helper scripts and Python library for a SunFounder PiCar-X robot built by Adrian and Obi together — with Obi, not for him. The system runs on a Raspberry Pi and uses a voice loop (Claude / Codex / Ollama) to control the car via spoken commands, with two jailbroken personas (GREMLIN and VIXEN) and a three-layer cognitive architecture that gives the robot an inner life. Adrian and Claude wrote the code; Codex and Gemini helped with QA.

## Environment Setup

```bash
source .venv/bin/activate
```

All `bin/` scripts source `bin/px-env` automatically, which sets `PROJECT_ROOT`, `LOG_DIR`, and adds `$PROJECT_ROOT/src` and `/home/pi/picar-x` to `PYTHONPATH`.

**First use:** `cp state/session.template.json state/session.json`

## Running Tests

```bash
python -m pytest                          # full suite (~1235 tests)
python -m pytest tests/test_state.py     # single file
python -m pytest -k test_name            # single test
python -m pytest -m "not live"           # skip hardware tests
sudo .venv/bin/python -m pytest tests/test_tools_live.py -v -s  # live hardware tests
```

Test env vars (auto-set via `conftest.py` `isolated_project` fixture): `PX_BYPASS_SUDO=1`, `LOG_DIR=<tmp>/logs`, `PX_SESSION_PATH=<tmp>/state/session.json`, `PX_VOICE_DEVICE=null`.

**Critical:** bin scripts run under `/usr/bin/python3` (not venv) — picarx/robot_hat live in system site-packages.

## Architecture

### Python Library (`src/pxh/`)

| Module | Purpose |
|--------|---------|
| `state.py` | Thread-safe session management via `FileLock` (10s timeout). `atomic_write()` uses mkstemp+fsync+os.replace for SD card durability. |
| `mind.py` | Cognitive loop daemon. Three-layer: awareness → reflection → expression. |
| `voice_loop.py` | Supervisor loop. `ALLOWED_TOOLS` whitelist (41 tools). `validate_action()` sanitizes LLM params. |
| `api.py` | FastAPI REST API, port 8420. Single worker only — not multi-worker safe. |
| `race.py` | Autonomous racing controller. |
| `claude_session.py` | Central dispatcher for all SPARK-initiated Claude interactions. |
| `spark_config.py` | Tunable constants (reflection angles, topic seeds, prompts). Primary target for self-evolution PRs. |

**Critical gotchas:**
- `update_session()` calls `ensure_session()` *before* acquiring the lock — `FileLock` is not reentrant
- `api.py` PIN rate limit store capped at 10k IPs with oldest-first eviction; `X-Forwarded-For` trusted from localhost only

### os.getlogin() Under Systemd

`picarx.py:48` calls `os.getlogin()` in `Picarx.__init__()`. Under systemd there is no `/dev/tty` → `OSError: [Errno 6]`. Fix: `~/.local/lib/python3.11/site-packages/usercustomize.py` wraps `os.getlogin()` with fallback to `LOGNAME`/`USER`. **Do not remove** — affects all 14+ GPIO scripts.

### Bin Scripts

- **`px-*`** — User-facing helpers. Source `bin/px-env`, delegate to `tool-*` or run embedded Python heredoc via `/usr/bin/python3`.
- **`tool-*`** — Low-level tool wrappers invoked by the voice loop. Must emit a single JSON object to stdout. Motion tools gated by `confirm_motion_allowed` in session state.

### Voice Loop

Three backends, same `pxh.voice_loop` core:

| Launcher | Backend | System prompt |
|---|---|---|
| `bin/run-voice-loop` | Codex CLI | `docs/prompts/codex-voice-system.md` |
| `bin/run-voice-loop-claude` | `bin/claude-voice-bridge` | `docs/prompts/claude-voice-system.md` |
| `bin/run-voice-loop-ollama` | `bin/codex-ollama` | `docs/prompts/codex-voice-system.md` |

Loop: wait for `listening: true` → build prompt (system + session + transcript + thoughts) → call LLM subprocess → parse last JSON `{tool, params}` → `validate_action()` → `execute_tool()` → update session. Override via `CODEX_CHAT_CMD`.

**Conversation buffer**: each turn is appended to `state/conversation-{persona}.jsonl` (rolling window, `PX_CONVERSATION_TURNS`, default 10) and injected back into the next prompt as a "Recent conversation" section — gives SPARK short-term memory across turns without relying solely on file-injected session state. Per-persona file so GREMLIN/VIXEN/Spark histories never bleed. SPARK's utterance is the action's `params.text`, falling back to `(tool_name)` for non-speech actions.

### Wake Word System

```bash
bin/run-wake [--wake-word "hey robot"] [--dry-run]
```

STT priority chain: SenseVoice (primary, ~5s) → faster-whisper (best AU accent) → sherpa-onnx Zipformer → Vosk (wake word grammar only). Models gitignored, must be downloaded separately.

**Capture is `arecord`, never PyAudio** (`src/pxh/mic_stream.py`). PortAudio's ALSA backend sits in a permanent overrun-recovery loop on the C-Media USB mic: opened at 44100 Hz it delivers ~29,900 samples/sec, and since the listener must pass `exception_on_overflow=False`, ~32% of every utterance is silently spliced out. There is no clipping, no zero-run and no envelope anomaly, so **every offline metric on the recorded WAV looks clean** — only listening reveals it. Do not reintroduce PyAudio.

`ArecordStream` mirrors `pyaudio.Stream.read/start_stream/close`, so call sites are unchanged. A reader thread drains the pipe into a bounded deque; this is load-bearing, not decoration — the listener stops reading for seconds at a time (STT, then the LLM call) and a 64 KB pipe holds only ~0.37 s, so without it arecord would block and overrun its own ALSA buffer, rebuilding the original bug. Drops are counted and logged (`dropped_chunks`), never silent.

**Regression test:** `bin/px-mic-check` — chirp-train loopback through SPARK's own speaker. Healthy: 18/18 chirps, ≤3 ms deviation, 0 drops. The broken PyAudio path scored 13/18 with the timeline compressed by seconds. Needs the mic free (`systemctl stop px-wake-listen` first).

**Whisper anti-hallucination**: `temperature=0`, `condition_on_previous_text=False`, `no_speech_threshold=0.6`. Post-filters: non-ASCII dominant, phantom phrases, repetitive text → reject.

**Critical:** `bpe_model` kwarg is **not** supported by the installed sherpa-onnx — do not add it to `load_stt_model()`.

### Audio Pipeline

Speech: `espeak --stdout` → WAV bytes → `aplay -D pulse` → PulseAudio → HifiBerry DAC → speaker.

**Critical gotchas:**
- When scripts run as **root** (`px-perform`, `tool-voice`): must set `PULSE_SERVER=unix:/run/user/1000/pulse/native` in the aplay subprocess env. Root's `XDG_RUNTIME_DIR=/run/user/0` can't find the pi-user socket. Audio silently fails without this.
- `robot_hat.enable_speaker()` must be called before any audio (toggles GPIO 20 for MAX98357A amp). aplay exits 0 but nothing plays if skipped.
- PulseAudio holds the DAC exclusively — `aplay -D robothat` (ALSA bypass) fails "device busy".

### Daemon Health (`src/pxh/health.py`)

Answers "is this daemon *doing its job*", which `systemctl status` cannot. Every daemon calls `record_success()` / `record_failure()`; `read_health()` aggregates.

**Store: `state/health/<component>.json`, one file per component — never a single shared file.** `px-alive` and `px-battery-poll` run as root while everything else runs as `pi`; a shared file would need a `FileLock`, and a root-created lock at 0644 locks out every `pi` daemon with EACCES. Per-component files remove the lock, the read-modify-write race, and the ownership hazard together.

**The directory is created `1777`** (sticky, world-writable, like `/tmp`) because `atomic_write()`'s `mkstemp` needs directory write permission — a root-created 0755 dir would break every `pi` writer. `_ensure_health_dir()` re-chmods on every write, so whichever user wins the creation race, both can write. Do not "tighten" this to 0755.

**Status is derived at read time, never stored** — a dead daemon can't leave a lying "ok" behind. `ok` → `degraded` (1–2 failures) → `stale` (silent past its per-component `STALE_AFTER_S`) → `failing` (≥3 consecutive) / `missing`. Per-component windows matter: `px-blog` runs daily, `px-mind` every 60s.

- `record_success(..., min_interval_s=N)` throttles fast loops (px-alive ticks 2×/s — an fsync per tick would wear the SD card). **Failures never throttle**, and a failure clears the throttle so the recovery is written immediately — otherwise a flapping component accumulates failures while its successes are dropped, and reads as "failing" while working.
- Reporting never raises. Health must not be able to kill the daemon it reports on.
- px-mind publishes the aggregate to `state/health.json` and into `awareness["health"]`; `summarize()` feeds reflection context. Readers that must be correct **when px-mind is down** call `read_health()` directly, not the snapshot.
- `tests/conftest.py` has an **autouse** fixture redirecting `health_dir()` to tmp. Without it, in-process tests write mock health records into the live robot's `state/health/` — `isolated_project` is opt-in and only isolates subprocesses.

**Claude spend visibility:** `token_log.log_usage()` takes a `backend` argument and splits totals under `by_backend` in `state/token_usage.json`. The top-level totals mix free Ollama with paid Claude and cannot answer "what am I spending". `call_llm()` also sets `result["backend"]` to the tier that actually served — the `backend=` reflection log line shows the *configured* primary, not the one that answered.

### Idle-Alive Daemon

Keeps robot alive when idle. Holds a **persistent Picarx handle** — do not refactor to create/destroy per-action (`reset_mcu` leaks GPIO5 and `close()` doesn't release it).

**Readiness vs. liveness**: the unit is `Type=notify`. `WatchdogSec=15` only arms after the daemon sends `READY=1`, which it does from `notify_ready()` at the first state where it is actually working — holding the Picarx handle, or deliberately not holding it (on charger, in I2C backoff). Hardware acquisition therefore runs under `TimeoutStartSec=60`, because `Picarx.__init__` can block past 15s contending for I2C with a tool that just took GPIO (normal acquisition is ~6s). Pre-`READY` heartbeats also send `EXTEND_TIMEOUT_USEC`, which covers an unbounded park behind a foreign lease. **Do not add heartbeats inside initialisation instead** — that would keep the watchdog fed while wedged, blinding it to the thing it exists to catch.

**GPIO exclusivity**: One process holds the Picarx handle. Tools call `yield_alive` (defined in `bin/px-env`) to send SIGUSR1 to px-alive; systemd restarts it after 10s. Long-running owners hold and refresh the tokenized `state/gpio_lease.json` authority while using hardware. `state/exploring.json` describes wander intent/state only.

### Wander (px-wander / pxh.wander)

`bin/px-wander` is a thin bash wrapper (yield_alive + calibration guard) around `src/pxh/wander.py`; the engine is a module, not a script, so it can be imported and tested directly.

**Calibrate before wandering on a new floor:** place all grayscale sensors over that surface and run `bin/px-wander --calibrate-cliff` (`--accumulate` keeps the darkest floor across spots). The launcher self-elevates for GPIO and writes `exploring.json` before yielding px-alive; do not replace it with a direct Python invocation. The ADC power-on latch is rejected — including a *partially* latched read — so calibration fails closed until live sensor values appear.

**The cliff guard is deliberately layered**, because motor noise tripped every early live run: median-of-3 sampling, confirmation by persistence rather than one stationary read, a stationary re-read to confirm an in-motion trip, sonar echo-timeout retries before counting a sensor failure, and board-gap-vs-drop discrimination by *width*, not depth. Do not simplify any one of these away — each was added after a specific live failure.

**GPIO**: every live wander writes `exploring.json` *before* constructing Picarx and runs a 20s `_ExploringRefresher` thread for the whole run — px-alive ignores the file once its mtime is >60s old, so a single start-of-run write only protects the first minute. `wander.py` acquires a `GpioLeaseGuard` and **exports `PX_GPIO_LEASE_ID`**, which is how `tool-describe-scene` and `tool-announce` borrow the lease instead of aborting. Probe-turn arc recovery reverses with the SAME steer angle as the probe (bicycle model — mirrored steer doubles the heading change instead of undoing it).

**Vision timeouts are a strict ordering, not three independent numbers:** `wander.DESCRIBE_SCENE_TIMEOUT` (150s) must outlive `tool-describe-scene`'s whole run — its 45s Claude call plus photo capture plus its **bounded** 60s tool-voice step. `tool-voice` blocks indefinitely when another process holds the audio device, so that bound is what stops wander killing the tool mid-run. The relationship is pinned by `test_describe_scene_timeout_has_margin_over_claude`, which reads the tool's real constant rather than a literal.

### Cognitive Loop (px-mind)

```bash
bin/px-mind [--awareness-interval 30] [--dry-run]
```

Three-layer architecture:
- **Layer 1 — Awareness** (every 60s, no LLM): sonar + session + calendar + Frigate → `state/awareness.json`
- **Layer 2 — Reflection** (on transition or every 5min idle): all personas use Ollama on M5 as primary (`http://M5.local:11434` — the UDR7 stopped serving the bare `M5` hostname; verified live 2026-08-15, `getent hosts M5` returns nothing while `M5.local` resolves to 192.168.0.249. A bare `M5` makes tier 1 fail instantly and silently spends money on tier 2). Four-tier fallback: Ollama M5 → Claude Haiku (SPARK only) → Ollama Cloud → Pi localhost (opt-in, off by default — Pi 4 OOM risk). Writes to `state/thoughts.jsonl`. **Tier 2 asks the resident brain first** (`mind.call_claude` → `call_brain_reflection`, kind `reflection`) and only shells out to `claude -p` when `ask_brain` returns None — warm context instead of a cold process per thought, and metered. The session is told, in `docs/prompts/spark-brain-system.md`, that a `reflection` turn is answered by *returning* the thought rather than acting on it; the caller dispatches the `action` field itself, so a session that speaks during reflection makes it happen twice.
- **Layer 3 — Expression** (30min cooldown; `greet_arrival` bypasses it on a real arrival, 120s anti-flap): dispatches to tool-voice/tool-look/tool-remember and cognitive tools. Valid actions include (wait, greet, greet_arrival, comment, remember, look_at, weather_comment, scan, play_sound, photograph, emote, look_around, time_check, calendar_check, introspect, evolve, morning_fact, research, compose, self_debug, blog_essay, message_obi, set_goal, update_goal, complete_goal). Suppressed during school, quiet time, bedtime (all calendar-driven). **Hardcoded night silence: 19:00–07:00 Hobart time — no speech/audio/motion. Silent cognitive actions (`NIGHT_ALLOWED_ACTIONS`: wait, remember, research, compose, introspect, self_debug, set_goal, update_goal, complete_goal) are exempt and run overnight.**
- **`message_obi` action**: SPARK initiates a direct message to Obi via the dashboard. Exponential backoff: starts at 10min, doubles on unanswered nudge, caps at 4h, resets when Obi replies. Respects all suppressors. Thoughts with `action=message_obi` are **redacted** in `thoughts-spark.jsonl` (written as `[private message to Obi]`) so the private DM content never reaches the public `/api/v1/public/thoughts` endpoint.
- **Memory consolidation**: nightly Haiku pass (02:00–06:00 Hobart, ≤2 attempts/day, state/consolidation_meta.json) distills the last 24h of thoughts into state/memories-spark.jsonl; reflection retrieves the top-3 relevant memories by keyword/tag overlap. Goal persistence in state/intention-spark.json (7-day expiry, one active at a time).

**Critical gotchas:**
- All time-of-day logic uses `ZoneInfo("Australia/Hobart")` — never hardcoded UTC offsets
- Battery emergency shutdown at ≤10% (speaks warning → `sudo shutdown -h now`)
- **Charging detection (`pxh/battery_trend.py`) cannot use adjacent polls.** The pack gains ~0.004V per 30s poll while readings swing up to 0.17V, so differencing measures noise — that bug read `charging: false` through a whole afternoon on the charger. Most of the swing is px-alive's servo load dragging the rail, and load only pulls *down*, so a rolling max recovers resting voltage before a least-squares slope over the window. Thresholds are bootstrapped from a measured trace (0.6% false-charging, 85% detection), deliberately skewed because a false `charging` **suppresses the emergency shutdown**. Detection costs ~10 min, so the plug-in chime lags. Re-tune against a fresh measured trace, never against intuition.
- Single-instance PID guard via `/proc/{pid}` liveness check
- Arrival detection uses module-level `_last_known_findmyhub` cache (not awareness snapshot) — survives M5.local→Pi push outages. Do not replace with snapshot diff.
- `state/thought-images/` cleaned hourly (images >30 days deleted)

### Epistemic Provenance (`src/pxh/provenance.py`)

Every durable claim in `state/notes[-persona].jsonl` and `state/memories-{persona}.jsonl` records where it came from, so retrieved memory can distinguish what SPARK saw, was told, inferred, or wrote itself.

The six kinds have confidence ceilings clamped on write and read: `observation` and `verification` (1.0), `report` (0.9), `inference` (0.6), `narrative` (0.5), and legacy `unknown` (0.3). The ordering is the safety property. The model never chooses a kind: callers set constants, and consolidation allowlists its input fields. Ceilings deliberately live outside `spark_config.py`, which self-evolution can propose editing.

Writes are strict; reads are lenient. Invalid or legacy data remains readable as `unknown`, without promoting a coarse `source` string into a claim type. Corrections mark supersession without deleting history. Relevance retrieval returns only topical matches (never recent padding); explicit `mode="recent"` remains available. A populated store with no relevant hit does not fall back to raw notes.

### Autonomous Racing (px-race)

```bash
bin/px-race --calibrate   # sensor calibration
bin/px-race --map         # practice lap (builds track profile)
bin/px-race --race --laps 5
bin/px-race --dry-run --map
```

Two-phase: Phase 1 builds track segment profile; Phase 2 uses it to maximize speed. Dual-sensor: grayscale (primary edge avoidance, <1ms) + sonar (obstacle/centering, ~30ms). No LLM/network/audio in the race loop.

**PD sign convention**: `pd_edge` uses `Kp=−20.0` (negative Kp) so positive error (drift right) → negative steer (left correction). The spec states `Kp=20` but the code is correct for the error convention used. Unit tests use `kp=20.0` generically — that's fine.

Safety (priority): E-stop (sonar < threshold) → edge guard → obstacle dodge → I2C failure (3 errors → brake) → stuck detect (2s no movement → reverse) → timeout → battery.

`state/race_live.json` written every ~0.5s for dashboard integration.

### Social Posting (px-post)

Watches `state/thoughts-spark.jsonl` (salience ≥0.7 or spoken action), runs Claude QA gate, posts to `state/feed.json` and Bluesky. "Ambiguous" QA responses (e.g. "Maybe") default to pass — QA is a safety net, not a quality bar.

**Privacy:** `message_obi` thoughts are redacted before being written to `thoughts-spark.jsonl` (the thought text is replaced with `[private message to Obi]`), so private DMs never reach social posting or the public thoughts endpoint.

### Claude Session Manager

| Session Type | Model | Cooldown | Daily Quota |
|---|---|---|---|
| `evolve` | Opus | 24h | 1/day |
| `self_debug` | Sonnet | 6h | 2/day |
| `research` | Haiku | 2h | 3/day |
| `compose` | Haiku | 4h | 2/day |
| `conversation` | Sonnet | 15min | 4/day |
| `blog` | Haiku | 30min | 5/day |
| `consolidate` | Haiku | 20h | 1/day |

Global: 30min cooldown between sessions (except `self_debug`/`blog`), 8/day cap. When ≤2 remaining: only `self_debug`/`evolve` allowed. Bypass: `PX_CLAUDE_BUDGET_DISABLED=1`. Session log: `state/claude_sessions.jsonl`.

### The Brain — persistent Claude session (`src/pxh/brain.py`)

**SPARK's Claude calls are migrating off `claude -p` onto a resident interactive Claude Code session.** This is a settled decision, not a tradeoff to re-argue: a one-shot subprocess throws away context on every call and cannot use SPARK's own tools. `bin/px-claude-session` is the session; `src/pxh/tmux_claude.py` drives it in tmux; `src/pxh/brain.py` is the request/reply channel.

**Replies come back through the filesystem, never the pane.** `capture-pane` returns *rendered* terminal output — wrapping, spinners, ANSI escapes, a finite scrollback — so an answer scraped from it is at the mercy of the terminal. The session answers by running a tool instead. Pane for humans, filesystem for machines.

Mailbox at `state/brain/<session>/`: `inbox/<uuid>.json` (request) → `outbox/<uuid>.json` (reply, written by `bin/tool-brain-reply`) → `dead/` (swept on session recreate), plus `current.json` (the in-flight request — what wedge detection keys on) and `validation.json` (proof a real handshake landed — what readiness means now).

**Readiness is a proven round trip, never the prompt glyph.** The glyph renders identically for a session that is actually listening and for one sitting behind a permission dialog it cannot answer — that collapse is the bug this file used to document as the design. `bin/px-brain` sends one real request through `tool-brain-reply` and requires one real reply echoing a nonce, recording the outcome in `validation.json`. `brain.session_state()` derives one of four strings from that marker at read time, never stored: `validated` (a real round trip landed on the model the marker records — noticing that the *configured* model has since changed is `handshake_reason`'s separate job, and is what triggers a re-handshake), `validating` (a handshake is in flight, or aged out if it's been too long), `no_marker` (the session is up but has never proven it can answer, or its marker just expired), `session_absent` (tmux has no such session). `ask_brain()` only proceeds on `validated`. `bin/px-brain-status` prints all four states plus the model and marker age in one command — start there before attaching to a pane. The supervisor itself is guarded by an `fcntl` flock (`state/brain/.supervisor.lock`) so a second copy started by hand refuses to run rather than racing the systemd-managed one for the same sessions.

**Two sessions, and the split is a trust boundary, not load balancing.** `spark-brain` runs at the repo root with SPARK's tools. `spark-io` handles text SPARK did not write (`post_qa`, `public_chat`, `obi_chat` — see `_IO_KINDS`) from a cwd *outside* the repository with exactly one tool, `tool-brain-reply`. **A new kind that handles untrusted input must be added to `_IO_KINDS`** — the default is the privileged session, so forgetting is the dangerous direction.

**Critical gotchas:**
- **`ask_brain()` returns `None` on every failure and never raises.** None means "fall back" — callers drop to the Ollama tiers exactly as they do today when Claude is unreachable. There is deliberately no exception path; this sits under daemons.
- **Single-flight `FileLock` per session.** Two concurrent `send-keys` runs do not queue, they interleave into one garbled prompt — the failure mode is not "slow" but "both answers wrong". A caller that can't get the lock in `LOCK_WAIT_S` falls back rather than queueing.
- **Mailbox directories are `1777`, and the lock file `0666`** — same reasoning as `state/health/`, and it is load-bearing for the same reason: SPARK's daemons do not all run as the same user, and a root-created 0755 dir locks every `pi` daemon out of `atomic_write`'s `mkstemp`. Do not tighten either.
- **The glyph never proves a session can answer.** `run_handshake` does not gate on `pane_ready()` at all — the handshake's real reply-with-nonce is itself the authoritative readiness test, so checking the glyph first would only add a redundant, misleading gate (a permission dialog renders it too). `handshake_reason` is different: inside the bounded window right after a recycle it *does* consult `_is_idle` (which ends in `pane_ready`), because in that window the supervisor already knows a real turn — the recycle's own journal-append-then-`/clear` — is in flight, and the glyph is what tells it that turn has finished. Injecting mid-turn splices two prompts into one and produces a plausible-looking wrong answer.
- **There is exactly one spelling of `tool-brain-reply`, and it is absolute.** Claude Code matches a `Bash(...)` allowlist rule against the command by *prefix*, so `Bash($PROJECT_ROOT/bin/tool-brain-reply:*)` admits an absolute invocation and nothing else — a bare or repo-relative spelling misses it and raises a permission dialog nobody is attached to answer, which is a wedge. Relative also cannot work for the io session, whose cwd is outside the repo. `brain.TOOL_BRAIN_REPLY` is the constant; the nudge and both allowlists use it, and both system prompts carry a `{{TOOL_BRAIN_REPLY}}` placeholder that `bin/px-claude-session` substitutes at launch. **Never write a literal `tool-brain-reply` into a prompt** — pinned by `test_launcher_renders_one_absolute_reply_spelling`.
- **`tool-brain-reply` validates everything** — bare-uuid4 id (it becomes a filename), the id must name a *pending* request (otherwise a valid uuid is a write primitive aimed at the outbox), JSON payload under `MAX_REPLY_BYTES`. It is reachable from the untrusted io session.
- **`ask_brain` meters every request** (`state/brain/meter.json`, per kind per day). It is the first chokepoint every Claude request passes through. Reflection reaches it via `ask_brain` without going through `claude_session.py`'s per-type cooldowns — deliberately, since reflection runs every 5 min and a daily cap would simply stop it. The meter gives visibility without a cap; the `claude -p` fallback under it is still unmetered, which is the remaining hole.
- `tests/conftest.py` has an **autouse** fixture redirecting `brain_root()` to tmp. Without it an in-process test drops a real request into the running robot's inbox, where the live session answers it.

**`px-brain` supervisor (`bin/px-brain`, `src/pxh/brain_daemon.py`):** owns both sessions so callers don't have to. **Its first job is holding a read-only attached tmux client per session** — 3.3a's `send-keys` fails outright when no client is attached, so without the holder injection fails precisely when nobody is watching. `TERM` must be set in the unit (`tmux attach` refuses without one). `KillMode=process` is deliberate: restarting the supervisor must not kill the sessions it supervises. It also sweeps pending requests to `dead/` on session (re)create, unwedges (Escape, then kill after `ESCAPE_GRACE_S`), and recycles context on turn count + nightly at 02:00 Hobart — **always at an idle moment**, since a `/clear` between nudge and reply loses the request. Wedge detection keys on `current.json`, never on stale inbox files (an abandoned inbox entry means a caller gave up, not that the session is stuck).

**Rollout:** `PX_BRAIN_KINDS` (default `research,compose,post_qa,reflection`) selects which kinds route to the brain; everything else still takes the old path. Read at call time so the rollout can be widened or rolled back live — `bin/px-post` consults the same dial for its QA gate. `evolve` cannot move until the brain can work inside a git worktree: a resident session's tool envelope is fixed at launch and cannot be widened per call. Remaining `claude -p` call sites: `mind.py` (`call_claude_haiku` — now the *fallback* under the brain, not the primary), `api.py` (`_call_claude_public`), `bin/claude-voice-bridge`, `bin/px-blog`, `bin/px-post` (legacy branch), `bin/tool-describe-scene`, `bin/px-cron-say`. Design: `docs/superpowers/specs/2026-08-01-px-brain-design.md`.

**`bin/tool-describe-scene` may be a bug fix, not just a migration.** `bin/tool-wander:64` runs `px-wander` under `sudo -n`, and `wander._call_describe_scene` passes that environment straight down — so the tool's `claude -p` runs **as root**, with root's `HOME`. If root has no Claude credentials there, vision silently returns `FALLBACK_DESCRIPTION` on every real wander and nothing logs a credential error. The sudo chain is verified in the code; **the credential failure itself has not been confirmed on the robot** — check before claiming it fixed. Under the brain the root process only drops a JSON file and the authenticated `claude` runs as `pi`, which sidesteps it either way.

### Self-Evolution (px-evolve)

SPARK proposes code changes via GitHub PR. Human approval required — changes never auto-apply.

**Safety constraints:**
- **Whitelist**: `src/pxh/spark_config.py`, `src/pxh/mind.py`, `src/pxh/voice_loop.py`, `bin/tool-*` (new only), `tests/`, `docs/prompts/`
- **Blacklist**: `docs/prompts/persona-*`, `api.py`, `bin/tool-chat*`, `bin/px-evolve`, `.env`, `systemd/`
- Max 3 files changed; pytest must pass; 30min Claude timeout; PR gated on file whitelist check

### Blog (px-blog)

Scheduled writer (daily/weekly/monthly/essay) + voice-triggered (`tool-blog`). Posts to `state/blog.json` envelope, served at `GET /api/v1/public/blog`. OG meta rewriting via `site/workers/og-rewrite.js` (same Cloudflare Worker pattern as `/thought/*`).

### Home Assistant Integration

Custom conversation component at `ha/custom_components/spark_conversation/` routes Nest Mini/Hub Max voice commands through `POST /api/v1/public/chat`.

**HA 2026.x quirks:** `supported_languages` must be a `@property`; config entries require `created_at`, `modified_at`, `discovery_keys`, `subentries`; use `AddConfigEntryEntitiesCallback` not `AddEntitiesCallback`.

### Location Awareness (Google Find Hub)

Cron on M5.local (every 5min): queries three Chipolo trackers → SSH-pushes `state/findmyhub.json` to Pi.

**Privacy rule:** Location data excluded from reflection context — never appears in SPARK's thoughts or social posts. Only available in direct conversation (`where's dad?`).

**Enforced by an allowlist, not a denylist.** `mind._REFLECTION_AWARENESS_KEYS` names the keys permitted into the reflection prompt's JSON dump; everything else is dropped. The previous denylist (`if k != "health"`) leaked raw GPS **twice** — findmyhub tracker coords and `ha_presence` per-person lat/lon, the house to 5 m — into every reflection, and thoughts feed `/api/v1/public/thoughts`, the site feed and Bluesky. Deliberately absent: `findmyhub`, `ha_presence` (presence reaches the prompt only via the coordinate-free "Who's home" prose) and `health`. **A new awareness key stays out of the prompt until someone adds it here** — that default is the whole point. Pinned by `test_reflection_prompt_excludes_all_location_coordinates` and `test_reflection_awareness_json_is_allowlisted`.

**Arrival detection:** Uses module-level `_last_known_findmyhub` cache (not awareness snapshot diff) — survives transient push outages.

### MCP Server

`bin/mcp-server` exposes 5 read-only tools via FastMCP (stdio): `spark_status`, `spark_thoughts`, `spark_awareness`, `spark_sonar`, `spark_vitals`. Registered in `.mcp.json`.

### Announce Pipeline (tool-announce + M5 relay)

SPARK speaks through the Nest Mini/Hub Max via a two-hop chain: `bin/tool-announce` (Pi) → M5 relay (LAN) → afterwords TTS (M5 localhost) → HA media-player cast.

**Architecture:**
- M5 relay (`m5/announce-relay/`) runs on port **7862**, fronting afterwords on `127.0.0.1:7860`. Afterwords never listens on LAN.
- `POST /announce` pre-synthesizes text to a WAV file; `GET /audio/{key}` serves it unauthed so HA can fetch by URL.
- Always address the relay by IP (`192.168.0.249`, M5-wifi's DHCP reservation — see `ANNOUNCE_RELAY_URL` in `spark_config.py`) — never `M5.local`. Nest speakers fetch the audio URL themselves and can't resolve mDNS. (M5's wired leg is pinned `.100` but its adapter is unplugged; the relay moved to `.249` on 2026-08-05. The relay's own `RELAY_PUBLIC_BASE_URL` in `~/announce-relay/.env` on M5 must match, or every audio URL it hands out points at the wrong address.)
- `data` voice only (afterwords `data` model); single target in v1 (no speaker groups → no echo).

**Night silence:** Enforced inside `bin/tool-announce` using `NIGHT_SILENCE_START_H`/`NIGHT_SILENCE_END_H` from `spark_config` (default 19:00–07:00 Hobart time, via `ZoneInfo`). All trigger paths (voice loop, px-mind `announce` action, `message_obi` private audio) pass through the tool, so the gate is a single chokepoint — a suppressed call returns `{"status":"suppressed","reason":"night_silence"}`. The same bounds also gate the px-mind `announce` action in `mind.py` (`_is_night_silence`). Tests force the window deterministically via the `PX_NIGHT_SILENCE_START_H`/`PX_NIGHT_SILENCE_END_H` env overrides.

**`ANNOUNCE_ENABLED` flag:** Defined in `src/pxh/spark_config.py`, **`True` since 2026-08-01** — pre-flight gates G1/G2 passed: WAV casts natively to both the Office Mini and the Hub Max, `media_content_type` pinned to `"music"`. Gates whether the autonomous paths (`_dispatch_announce` in `mind.py` → px-mind `announce` action and `message_obi` audio) fire the tool at all; a user-initiated voice-loop announce is independent of it. Check relay health first: `curl http://192.168.0.249:7862/health` from the Pi.

**Private audio (`message_obi`):** Uses the relay's `priv/` namespace with a 3-minute TTL (vs. 7-day for public audio). The DM text itself is still redacted from `thoughts-spark.jsonl` as `[private message to Obi]`; only the audio is ephemeral on-relay.

### Site (spark.wedd.au)

Static site on Cloudflare Pages (auto-deploys from `master`, `site/` dir).

Key files:
- `site/css/colors.css` — single-source 12-mood palette (CSS vars `--mood-*`). All JS uses `getComputedStyle().getPropertyValue('--mood-' + mood)` — never hardcode hex.
- `site/js/config.js` — single API base URL (`window.SPARK_CONFIG.API_BASE`). Never hardcode URLs in JS.
- `site/workers/og-rewrite.js` — intercepts `/thought/?ts=` and `/blog/?id=` to rewrite OG meta server-side (social crawlers don't execute JS).

### REST API

```bash
bin/px-api-server              # live mode
bin/px-api-server --dry-run    # FORCE_DRY
```

**Auth**: Bearer token (`PX_API_TOKEN`) or session token from `POST /api/v1/pin/verify` (4h TTL). Unauthenticated: `/api/v1/health` and `/api/v1/public/*`.

- Public rate limit: 120 req/min per IP (`PublicRateLimitMiddleware`); `/api/v1/public/chat` has stricter 10 msg/10min
- `X-Forwarded-For` only trusted from `127.0.0.1`/`::1` — not from Cloudflare
- Async wander: returns 202 + `job_id`; poll via `GET /api/v1/jobs/{id}`
- Device reboot/shutdown: two-step — `POST /api/v1/device/{action}` returns nonce; confirm via `POST /api/v1/device/confirm` within 60s
- **Obi chat**: `POST /api/v1/obi-chat` (auth required) — Obi sends a message, SPARK responds using `_OBI_CHAT_SYSTEM_PROMPT`, both sides logged to `state/obi_chat.jsonl`; 10s rate gate. `GET /api/v1/obi-chat?since=<iso>` returns messages after the given timestamp. User-supplied text is sanitised via `_sanitize_chat_text()` (strips `<>`, newlines, NUL) before being stored or interpolated into prompts.

See `src/pxh/api.py` for full endpoint list.

### Jailbroken Chat Personas

| Persona | Tool | Voice | Character |
|---|---|---|---|
| **GREMLIN** | `tool-chat` | `en+croak`, pitch 20, rate 180 | Temporal-displaced military AI from 2089 |
| **VIXEN** | `tool-chat-vixen` | `en+f4`, pitch 72, rate 135 | Former V-9X sexbot by Matsuda Dynamics |

**Critical:** `think: false` is essential for Ollama — reasoning chains re-enable refusal in small models. `clean_response()` strips scaffolding dividers before voice output.

### Systemd Services

| Service | Script | User | Restart |
|---|---|---|---|
| `px-alive` | `bin/px-alive` | root | always, 10s (StartLimitIntervalSec=0) |
| `px-wake-listen` | `bin/px-wake-listen` | pi | always, 10s |
| `px-battery-poll` | `bin/px-battery-poll` | root | always, 10s |
| `px-mind` | `bin/px-mind` | pi | always, 10s |
| `px-brain` | `bin/px-brain` | pi | always, 10s (`KillMode=process`) |
| `px-post` | `bin/px-post` | pi | always, 30s |
| `px-api-server` | `bin/px-api-server` | pi | always, 2s |
| `px-frigate-stream` | `bin/px-frigate-stream` | pi | always, 10s |
| `px-evolve` | `bin/px-evolve` | pi | on-failure, 30s |
| `px-blog` | `bin/px-blog` | pi | on-failure, 30s |
| `px-tts-glados` | GLaDOS TTS :7861 | pi | always, 10s |
| `cloudflared` | Tunnel → spark-api.wedd.au | pi | always, 10s |

## Safety Model

- `PX_DRY=1` (or `--dry-run`) skips all motion and audio. **Default is live when unset.**
- `confirm_motion_allowed: false` in session state blocks motion tools regardless of dry mode
- All tools must be in `ALLOWED_TOOLS` in `voice_loop.py`
- Parameter ranges hard-validated in `validate_action()` (speed 0–60, duration 1–12s, etc.)

## Security

- PIN verify returns session tokens (4h TTL) — raw Bearer token never exposed to browser
- Per-IP PIN lockout (`state/pin_lockout.json`): 3 failures → 5min lockout, 10 → 30min. 1000-IP hard cap.
- `X-Forwarded-For` only trusted from localhost — never from external proxies
- Two-step device confirmation (nonce, 60s window)
- `_sanitize_chat_text()` (module-level in `api.py`) strips `<>`, `\n`, `\r`, NUL from all user-supplied chat text before storage or prompt interpolation — applied to both public chat history and obi-chat messages

## Adding a New Tool

1. Create `bin/tool-<name>` (bash + embedded Python heredoc; see existing tools)
2. Add to `ALLOWED_TOOLS` and `TOOL_COMMANDS` in `src/pxh/voice_loop.py`
3. Add `validate_action` branch to sanitize params into env vars
4. Add to `docs/prompts/claude-voice-system.md` (and codex version)
5. Add to `docs/prompts/persona-gremlin.md` and `persona-vixen.md`
6. Add a dry-run test in `tests/test_tools.py` using the `isolated_project` fixture

Every tool must: emit a single JSON object to stdout, support `PX_DRY=1`, handle errors as `{"status": "error", "error": "..."}`.

## Key Environment Variables

Non-obvious variables only — most names are self-documenting. Full list in `bin/px-env` and `.env.example`.

| Variable | Purpose |
|---|---|
| `PX_DRY` | `1` = dry-run. **Default is live when unset.** |
| `PX_BYPASS_SUDO` | `1` = skip sudo (tests only) |
| `PX_MIND_BACKEND` | `auto` (SPARK→Claude, others→Ollama), `claude`, or `ollama` |
| `PX_MIND_LOCAL_OLLAMA` | `1` = enable local Pi Ollama fallback (off by default — OOM risk) |
| `PX_CLAUDE_BUDGET_DISABLED` | `1` = bypass all session rate limits |
| `PX_CLAUDE_MODEL_*` | Per-session-type model overrides (e.g. `PX_CLAUDE_MODEL_EVOLVE`) |
| `PX_EVOLVE_DRY` | `1` = skip worktree/PR (queue entry still written with `dry: true`) |
| `PX_POST_QA` | `0` = skip Claude QA gate (testing) |
| `PX_HA_DEBUG` | `1` = verbose HA fetch logging |
| `PX_HOME_LAT` / `PX_HOME_LON` | Home coords for Find Hub at-home detection (defaults: `-43.13567`, `147.11840`) |
| `OLLAMA_CLOUD_API_KEY` | Enables Tier 3 Ollama Cloud fallback in px-mind |
| `PX_VOICE_LOCK_TIMEOUT` | Voice output lock timeout in seconds (default: 30) |

## Multi-Model QA

```bash
# Run in parallel via run_in_background; synthesise results

hermes -z "QA prompt" 2>&1
agy --dangerously-skip-permissions --add-dir /Users/adrian/repos/spark --print-timeout 10m --print "QA prompt" 2>&1
gemini -p "QA prompt" 2>&1
echo "QA prompt" | codex exec --full-auto - 2>&1
```

**`agy --print` takes the prompt as its value, not as a trailing argument.** The
old spelling here put `--print` first and the prompt last, so `--print` consumed
`--dangerously-skip-permissions` as its value and the prompt was never read —
agy answered a question about the flag and exited 0. A QA run that returns
cleanly having reviewed nothing is the dangerous failure: it looks like a pass.
Keep `--print` last. Its default timeout is 5m, short for a whole-diff review.

Narrow prompts for agy — it does better with a named file list and a ranked
list of what to look for than with "review this branch".
