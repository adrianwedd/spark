# CLAUDE.md

This file provides the canonical engineering constitution for Claude Code,
Codex, and every coding agent working in this repository.

## Execution Default

Act, verify, continue. Once a goal is authorised, perform all reversible in-scope steps needed to complete it. Routine repository actions implied by an authorised engineering goal — branch, commit, push, open/update PR, and CI — inherit that authorization and do not require a second approval. Ask only for genuinely missing decisions, unapproved destructive/external actions, credentials, or unsensed physical-safety facts. Never ask merely to reconfirm the goal.

## Project Overview

Helper scripts and Python library for a SunFounder PiCar-X robot built by Adrian and Obi together — with Obi, not for him. The system runs on a Raspberry Pi and uses a voice loop (Claude / Codex / Ollama) to control the car via spoken commands, with two jailbroken personas (GREMLIN and VIXEN) and a three-layer cognitive architecture that gives the robot an inner life. Adrian and Claude wrote the code; Codex and Gemini helped with QA.

## Environment Setup

```bash
source .venv/bin/activate
```

All `bin/` scripts source `bin/px-env` automatically, which sets `PROJECT_ROOT`, `LOG_DIR`, and adds `$PROJECT_ROOT/src` and `/home/pi/picar-x` to `PYTHONPATH`.

**First use:** `cp state/session.template.json state/session.json`

### Deploying to the robot

This checkout is a dev copy. The robot runs `/home/pi/picar-x-hacking` and is
**downstream of `master`** — a merge here changes nothing there until it is
deployed.

```bash
ssh pi@picar
cd /home/pi/picar-x-hacking && git status --short    # must be empty
git fetch origin master && git merge --ff-only origin/master
```

**A change that deletes or renames a file a unit names must be ordered *around*
that unit, not after it.** systemd resolves `ExecStart` when the unit starts, so
a deleted entry point does not fail the deploy — it leaves the unit
crash-looping against a missing file. That is #315's shape: loud, pointless, and
it needs a human. Check before deploying:

```bash
for u in $(systemctl list-units --type=service --all --no-legend --plain | awk '{print $1}'); do
  es=$(systemctl show "$u" -p ExecStart --value | grep -o '/home/pi/picar-x-hacking/[^ ;"]*' | head -1)
  [ -n "$es" ] && [ ! -e "$es" ] && echo "$u -> MISSING $es"
done
```

Repoint or disable those units **first**. #317 Phase 3 is the worked example: it
deletes `bin/px-brain`, so `sudo systemctl disable --now px-brain` precedes the
merge rather than following it.

**Retiring a service leaves residue, and the residue lies.** Stopping a unit
does not remove what it owned, and none of it is visible to the code that reads
that kind of file. #317 Phase 3 left three, and each was found by listing a
directory by hand rather than by anything failing:

| residue | why it lied |
|---|---|
| an orphaned `claude` process and its tmux server, ~594 MB | `KillMode=process` is deliberate — a supervisor restart must not kill the sessions it supervises — so `systemctl stop` leaves them running, unreachable, forever |
| `/tmp/tmux-1000/px-mind{.supervisor.lock}` | a socket file with no server reads as "there is a server here" |
| `state/health/px-brain.json` | `read_health()` deliberately reports any component that has *ever* written a file, so a retired service reports `stale` forever — a permanent false alarm on the operator board |

One residue class now cleans itself: `state/health/`'s orphaned `atomic_write`
temps are swept hourly by whoever next writes health there (#292), with each
removal logged to `logs/tool-health-sweep.log`. Everything else still needs the
by-hand listing.

So the deploy checklist for a retirement is: stop the unit, kill what it owned
(check `ps` for the process *and* the tmux socket), remove its health record,
then watch the board until the name is gone.

**Then restart the units whose *in-memory* state points at something that
moved.** A daemon that resolved a path at startup keeps the old one until it
restarts, and the symptom appears hours later rather than at deploy time:
`px-wake-listen` resolves its voice launcher once, so a launcher rename needs
`systemctl restart px-wake-listen` as well as `px-mind`. A *lazy* import does
not save a daemon either — the compiled statement keeps the old module name, so
a rename fails inside the daemon's next call, hours after a deploy that looked
clean (#332: the 20:51 deploy restarted 3 of 10 units and left three executing
code that still named the deleted `pxh.claude_session`; the first symptom was a
22:00 blog line nobody was watching for).

Source state and process state are separate realities, and only the second one
is running, so do not derive that list from memory:

```bash
bin/px-deploy-check     # on the robot, immediately after the ff-merge
```

It reads the deploy's changed files from git (`HEAD@{1}..HEAD`), reads every
`px-*` unit's `ExecStart` and start time from systemd, and flags any unit whose
entry point — or any `pxh` module reachable from it, including shell wrappers
that run `python -m pxh.x` — this deploy changed while the process was already
running. Replayed against the 2026-09-16 20:51 deploy it names six units: the
four #332 had to restart by hand, plus `px-mind` and `px-wake-listen`, and none
of the other four. **A deploy is not complete until it exits 0** — the source
tree being correct is not the property; the processes executing it is.

## Running Tests

```bash
python -m pytest                          # full suite (~1235 tests)
python -m pytest tests/test_state.py     # single file
python -m pytest -k test_name            # single test
python -m pytest -m "not live"           # skip hardware tests
sudo .venv/bin/python -m pytest tests/test_tools_live.py -v -s  # live hardware tests
```

**CI runs `pytest -m "not live"` on every PR** (`.github/workflows/tests.yml`,
Python 3.11 to match Bookworm). Prefer it over a full local run: this repo is
checked out on SPARK itself, so a local suite competes with the running robot
for a memory envelope that is already tight (#218, #219). Since #221 the suite
no longer writes production-shaped records into the live logs — `LOG_DIR` and
the tmux socket are isolated per test — but CI remains the gate regardless: a
suite that only ever runs in one environment cannot distinguish "my code is
correct" from "my machine is the code". See `docs/testing.md`. Targeted local
runs (`-k`, a single file) are fine; the `live` tests can only run here.

Test deps that the robot does not need live in `requirements-dev.txt`.

Test env vars come from two different places, and the difference matters. `conftest.py`'s **autouse** fixtures isolate every test in-process — `LOG_DIR` and `PX_BRAIN_TMUX_SOCKET` (`_isolate_observability`), `PX_SESSION_PATH` (`_isolate_session`), plus the health, mailbox and heartbeat roots. The **opt-in** `isolated_project` fixture is separate: it builds a tmp project tree and passes `PX_BYPASS_SUDO=1`, `LOG_DIR`, `PX_SESSION_PATH`, `PX_VOICE_DEVICE=null` into *subprocess* envs. This file previously described the second as doing the first, which is a large part of why #221 stayed invisible: nothing autouse was isolating logs at all.

A test that wants the real log dir or the real tmux socket must be marked `live`. Bypassing production isolation is explicit, never accidental.

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
| `model_session.py` | Budget/quota dispatch for every SPARK-initiated model call that is not reflection. Was `claude_session.py` until #317 Phase 3; the `claude_*` name went with the session it was named after. |
| `spark_config.py` | Tunable constants (reflection angles, topic seeds, prompts). Primary target for self-evolution PRs. |

**Critical gotchas:**
- `update_session()` calls `ensure_session()` *before* acquiring the lock — `FileLock` is not reentrant
- **The session lock is shared by two users, so its mode is a contract, not a default (#315).** `state/session.json.lock` is taken by the `pi` daemons *and* by root tools launched through `px-gpio-run` (`tool-look`, `tool-emote` and the rest call `update_session()`), and `filelock` opens it for *writing* — so whoever creates it sets the permissions for everyone. `_session_lock()` therefore creates it `0666` and reclaims one it cannot open (unlink, which needs write permission on `state/`, not on the file). Do not "tighten" it to 0644: a root-created 0644 lock is unopenable from every `pi` daemon, and px-mind crash-looped 59 times on exactly that on 2026-09-16. Note that a *held* lock leaves no file behind (`UnixFileLock._release()` unlinks it), so the hazardous state is a **lingering** one — what a holder that was killed rather than released leaves.
- A **daemon that dies on startup records a health failure**. Absence is the one state the health store cannot report, so a crash-looping component used to read `ok` on the strength of the previous run's last write (`px-mind`, same incident). `mind.main()`'s fatal path now records before returning.
- `api.py` PIN rate limit store capped at 10k IPs with oldest-first eviction; `X-Forwarded-For` trusted from localhost only

### os.getlogin() Under Systemd

`picarx.py:48` calls `os.getlogin()` in `Picarx.__init__()`. Under systemd there is no `/dev/tty` → `OSError: [Errno 6]`. Fix: `~/.local/lib/python3.11/site-packages/usercustomize.py` wraps `os.getlogin()` with fallback to `LOGNAME`/`USER`. **Do not remove** — affects all 14+ GPIO scripts.

### Bin Scripts

- **`px-*`** — User-facing helpers. Source `bin/px-env`, delegate to `tool-*` or run embedded Python heredoc via `/usr/bin/python3`.
- **`tool-*`** — Low-level tool wrappers invoked by the voice loop. Must emit a single JSON object to stdout. Motion tools gated by `confirm_motion_allowed` in session state.

### Voice Loop

Three backends, same `pxh.voice_loop` core:

| Launcher | `--backend` | System prompt |
|---|---|---|
| `bin/run-voice-loop` | `command` (Codex CLI) | `docs/prompts/codex-voice-system.md` |
| `bin/run-voice-loop-tier` | `tier` (cognition tier) | `docs/prompts/voice-system.md` |
| `bin/run-voice-loop-ollama` | `command` (`bin/codex-ollama`) | `docs/prompts/codex-voice-system.md` |

Loop: wait for `listening: true` → build prompt (system + session + transcript + thoughts) → call the model → parse last JSON `{tool, params}` → `validate_action()` → `execute_tool()` → update session.

**`--backend brain` is now `--backend tier`, and the wake listener uses it.** `voice_loop.run_voice_turn` calls the **cognition tier** (`pxh.m5`) directly, with the same 45s deadline and the same `VOICE_UNAVAILABLE_ACK` fallback. Nothing about the turn changed for the person standing there; what changed is that a lost answer is now a *classified* failure (`busy` / `timeout` / `offline` / `bad_response`) instead of an unattributed `None`, so the retry rule is stated instead of inferred: only `offline`/`bad_response` — the faults a cheap immediate retry actually fixes — are retried, and `busy`/`timeout` go straight to the acknowledgement rather than asking a saturated tier twice. The lock wait is the one place the interactive path *differs* from the tier's default: background callers wait zero (`do not enqueue`), while a voice turn waits `VOICE_TURN_LOCK_WAIT_S` (5s), because reflection holds the same lock for a few seconds every few minutes and refusing a child instantly trades a certain answer for a certain "give me a second". The old spelling named the session that used to serve the turn; #317 Phase 3 deleted the session, and a flag named after a deleted thing is a flag that lies in `--help`. **Deploying this rename needs `px-wake-listen` restarted as well as `px-mind`** — the listener resolves the launcher path once at startup.

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

**Crashed writers leave temps, and the directory sweeps them itself (#292).** `atomic_write()` unlinks its `tmp*.tmp` on any exception, but **SIGKILL cannot run that branch** — a daemon killed mid-`fsync` (the SD-card stalls in #247/#283/#287) leaves a complete-looking temp behind forever, and 81 of the 99 accumulated by 2026-09-17 were root-owned inside that 1777 directory, unremovable by any `pi` daemon that noticed them. `_ensure_health_dir()` now calls `state.sweep_stale_temps()` at most once an hour per process — and always on the **first** health write of a process, which is the "a previous run may have been killed" case. It is age-based (`STALE_TEMP_AGE_S`, 1 h) so a live write in another process never loses its temp, refusals under the sticky bit are counted rather than raised, and removals are recorded in `logs/tool-health-sweep.log` because cleanup nobody can see is the residue problem again. Do not move the sweep onto every health write: that is a directory scan on a 2 Hz path, on the card this system is already bottlenecked on.

**Status is derived at read time, never stored** — a dead daemon can't leave a lying "ok" behind. `ok` → `degraded` (1–2 failures) → `stale` (silent past its per-component `STALE_AFTER_S`) → `failing` (≥3 consecutive) / `missing`. Per-component windows matter: `px-blog` runs daily, `px-mind` every 60s.

- `record_success(..., min_interval_s=N)` throttles fast loops (px-alive ticks 2×/s — an fsync per tick would wear the SD card). **Failures never throttle**, and a failure clears the throttle so the recovery is written immediately — otherwise a flapping component accumulates failures while its successes are dropped, and reads as "failing" while working.
- Reporting never raises. Health must not be able to kill the daemon it reports on.
- px-mind publishes the aggregate to `state/health.json` and into `awareness["health"]`; `summarize()` feeds reflection context. Readers that must be correct **when px-mind is down** call `read_health()` directly, not the snapshot.
- `tests/conftest.py` has an **autouse** fixture redirecting `health_dir()` to tmp. Without it, in-process tests write mock health records into the live robot's `state/health/` — `isolated_project` is opt-in and only isolates subprocesses.

**A daemon that is alive and a capability that is gone are different axes, and
the board has to show both.** `consecutive_failures` describes the *process*;
`capabilities` on the same record describes a single *boundary* — one named
thing the daemon advertises and cannot currently do. Use
`record_capability_failure(component, capability, error)` at the boundary where
a daemon *was asked to work* and could not because a dependency is gone (an
`ImportError` for a module a deploy renamed or deleted is the canonical case,
#332), and `record_capability_success()` when that same capability runs.

- **Do not** turn every optional import into a health failure. Some imports are
  genuinely optional (a fallback prompt, a degraded mode); only the boundary
  that just failed knows whether it advertised the capability. Record it there
  or not at all.
- A block is **sticky**: `record_success()` on the daemon does not clear it,
  and neither does a restart — the record is on disk. The loop that keeps
  proving a process alive is exactly what hid #332 for three daemons. Only the
  capability succeeding clears it.
- A blocked capability promotes a healthy process to `degraded`, and never
  outranks `failing`/`stale`: "this daemon is dying" is the bigger news.
- Keep the log too. Health answers *is it broken?*; the log answers *what
  happened?*
- Prevention and detection are layered, not substitutes: `bin/px-deploy-check`
  stops a deploy leaving stale processes, and this axis reports the degradation
  if one happens anyway (manual surgery, a bypassed gate, package drift). An
  incident that reads as *semantically broken but healthy* is the signal to add
  the missing boundary, not to widen a status window.

**Long-term memory formation is a health component, `px-mind-consolidation`.**
Until it existed, the nightly consolidation pass could fail every night while
`read_health()` reported `overall: ok` — it was simply absent from
`KNOWN_COMPONENTS`, and absence is the one state this module cannot report. Its
window is **30h**, not px-blog's 86400: consolidation fires anywhere inside
02:00–06:00 Hobart, so two healthy runs can sit ~28h apart and a daily window
would call a working job stale most afternoons. `_consolidation_tick`
distinguishes three outcomes and the distinction is load-bearing — `ok`/`skipped`
record a success (too few thoughts is a correct night, not a failure), a failed
attempt records a failure, and **`None` records nothing**, because "not due
tonight" must not refresh the record on every 60s tick.

`health.memory_formation()` answers "when did SPARK last form a long-term
memory", promoted to a top-level `memory_formation` key in `read_health()` and
rendered by `bin/px-motd`. It keys on **`last_success_ts`, never `updated_ts`** —
a failing attempt refreshes the record without distilling anything, so a pass
that fails nightly stays inside its staleness window and under the failure
threshold while the store has not grown for days. That is why `overdue`
(>48h) is a separate axis from `status`, and why `summarize()` returns a
non-empty string for overdue memory on an otherwise clean bill.

**Consolidation runs on a background daemon thread, never on the tick (#291).**
`_consolidation_tick` only *supervises*: it heartbeats the in-flight marker,
reaps what a previous process left behind, and decides whether to start a
worker. It must always return promptly, because awareness, reflection and the
battery check run behind it. The reason is arithmetic, not taste — the kind's
declared deadline is **600s** and px-mind's own staleness window is **300s**, so
an inline call that honoured its budget guaranteed px-mind read `stale`. That
made the three defects mutually reinforcing: the pass could not be given enough
time, so it was given an ad-hoc `timeout=180` that silently overrode the
declared deadline, so every failure timed out at exactly 180.1s.

**`state/consolidation_job.json` is the in-flight marker**, and it is keyed on
a real **instance identity** — `(boot_id, pid, pid_starttime)` — plus a
heartbeat, not on a bare pid: *a pid is a slot the kernel reuses, not a name for
a process*. A health record says what the last *finished* attempt did; nothing
else answers "is one running right now". The worker thread dies with its
process, so a marker outliving its owner is always a lie, and
`consolidation_job_is_stale()` has four ways to catch it: a **different
`boot_id`** (a reboot means the owner is certainly gone, so the marker is
reclaimable at once instead of waiting out a heartbeat that will never arrive),
a **`pid_starttime` mismatch** (`/proc/<pid>/stat` field 22 — the canonical
within-boot PID-reuse discriminator; parse it by splitting on the **last** `)`,
since the comm field can contain spaces and parens), `/proc/<pid>` gone or not
px-mind, **or** the heartbeat quiet past `JOB_HEARTBEAT_STALE_S`. The heartbeat
stays the load-bearing backstop — it is the only one that catches an owner that
is genuinely alive and has simply stopped ticking; the identity checks are
exactness and speed, not a replacement. Missing or unparseable identity reads as
**stale**, the module's lenient-read posture: an unreadable marker must never
block tonight's attempt. `boot_id` is the same idiom as
`m5` / `wake_grant` (and, before #317 Phase 3 deleted it,
`brain_daemon`), and earns its place for the
same host-specific reason — this Pi has no RTC and timesyncd steps the clock, so
boot_id is the one member of the tuple a clock step cannot move. A restart
therefore cannot leave a false in-progress claim behind, and the tick records
that cleanup as a *failure* — no memory formed that night — rather than
silently resetting. The
heartbeat is written by the **tick**, not the worker: the worker spends its
whole life blocked in the tier call and could not beat if it wanted to. Past
`JOB_OVERRUN_AFTER_S` an unfinished run is reported **once**, not every 60s.

**Two attempts a night, spaced 40 min apart** (`memory.RETRY_SPACING_S`). All
three numbers used to disagree: `MAX_ATTEMPTS_PER_DAY` promised 2 while the
`consolidate` quota was 1 and its type cooldown 20h, so attempt 2 was
structurally unreachable. The spacing deliberately **clears** the 30-min global
cooldown rather than adding `consolidate` to `_GLOBAL_COOLDOWN_EXEMPT` — nobody
is waiting on a 3am retry, and each exemption is one more way for a background
job to crowd a session someone *is* waiting on. Success **or a correct skip**
marks the date done; only a failure leaves the later retry open.

**Spend visibility:** `token_log.log_usage(input, output, backend)` splits totals under `by_backend` in `state/token_usage.json`. The top-level totals mix metered Claude, metered-in-a-different-way Ollama Cloud and robot-local work, and cannot answer "what am I spending" — only the split can. **`backend` is required, with no default (#306).** It used to default to `"unknown"`, and the voice loop's one production call site took the default: 1,357 voice turns landed in an `unknown` bucket that answers nothing while the `claude` bucket sat frozen at 2026-08-20. Naming the tier is now the caller's job, in both places that know it: `pxh.m5.backend_label()` for anything the cognition tier served (hosted → `ollama-m5`, LAN daemon → `ollama-local`, per #308), and `voice_loop.command_backend_label(cmd)` for `--backend command` turns (the adapter is the only thing that knows the provider — `bin/codex-ollama` → the Ollama bucket for *its* `OLLAMA_HOST`, which defaults to Ollama Cloud, and the plain Codex CLI → `codex`, anything unrecognised → its own `command:<name>` bucket so it cannot hide in `unknown`). `run_voice_turn()` returns the serving label as its fourth element rather than leaving the loop to guess from configuration; `call_llm()` sets `result["backend"]` the same way. Do not reintroduce a default, and do not pass a literal host label — a literal is how a LAN daemon got billed to the cloud bucket.

**The `ollama-m5` bucket is no longer free (#308).** It used to mean "Adrian's own hardware, $0". It now means "Ollama Cloud, on a plan" — so a rising `ollama-m5` count is real consumption, not free local compute, and reading that label as `$0` is the mistake this note exists to prevent.

### Idle-Alive Daemon

Keeps robot alive when idle. Holds a **persistent Picarx handle** — do not refactor to create/destroy per-action (`reset_mcu` leaks GPIO5 and `close()` doesn't release it).

**Readiness vs. liveness**: the unit is `Type=notify`. `WatchdogSec=15` only arms after the daemon sends `READY=1`, which it does from `notify_ready()` at the first state where it is actually working — holding the Picarx handle, or deliberately not holding it (on charger, in I2C backoff). Hardware acquisition therefore runs under `TimeoutStartSec=60`, because `Picarx.__init__` can block past 15s contending for I2C with a tool that just took GPIO (normal acquisition is ~6s). Pre-`READY` heartbeats also send `EXTEND_TIMEOUT_USEC`, which covers an unbounded park behind a foreign lease. **Do not add heartbeats inside initialisation instead** — that would keep the watchdog fed while wedged, blinding it to the thing it exists to catch.

**GPIO exclusivity**: One process holds the Picarx handle. Tools call `yield_alive` (defined in `bin/px-env`) to send SIGUSR1 to px-alive; systemd restarts it after 10s. Long-running owners hold and refresh the tokenized `state/gpio_lease.json` authority while using hardware. `state/exploring.json` describes wander intent/state only.

### Wander (px-wander / pxh.wander)

`bin/px-wander` is a thin bash wrapper (yield_alive + calibration guard) around `src/pxh/wander.py`; the engine is a module, not a script, so it can be imported and tested directly.

**Calibrate before wandering on a new floor:** place all grayscale sensors over that surface and run `bin/px-wander --calibrate-cliff` (`--accumulate` keeps the darkest floor across spots). The launcher self-elevates for GPIO and writes `exploring.json` before yielding px-alive; do not replace it with a direct Python invocation. The ADC power-on latch is rejected — including a *partially* latched read — so calibration fails closed until live sensor values appear.

**The cliff guard is deliberately layered**, because motor noise tripped every early live run: median-of-3 sampling, confirmation by persistence rather than one stationary read, a stationary re-read to confirm an in-motion trip, sonar echo-timeout retries before counting a sensor failure, and board-gap-vs-drop discrimination by *width*, not depth. Do not simplify any one of these away — each was added after a specific live failure.

**GPIO**: every live wander writes `exploring.json` *before* constructing Picarx and runs a 20s `_ExploringRefresher` thread for the whole run — px-alive ignores the file once its mtime is >60s old, so a single start-of-run write only protects the first minute. `wander.py` acquires a `GpioLeaseGuard` and **exports `PX_GPIO_LEASE_ID`**, which is how `tool-describe-scene` and `tool-announce` borrow the lease instead of aborting. Probe-turn arc recovery reverses with the SAME steer angle as the probe (bicycle model — mirrored steer doubles the heading change instead of undoing it).

**Vision timeouts are a strict ordering, not three independent numbers:** `wander.DESCRIBE_SCENE_TIMEOUT` (150s) must outlive `tool-describe-scene`'s whole run — its 45s Claude call plus photo capture plus its **bounded** 60s tool-voice step. `tool-voice` blocks indefinitely when another process holds the audio device, so that bound is what stops wander killing the tool mid-run. The relationship is pinned by `test_describe_scene_timeout_has_margin_over_describe_scene`, which reads the tool's real constant (`vision.DESCRIBE_TIMEOUT_S`) rather than a literal.

**Cognition-tier vision is a sparse semantic escalation, not routine perception.** Frigate labels and sonar are the continuous local perception layer and are trusted on their own — a label Frigate can already name is already information, and uploading a photo so Claude can redescribe "person" or "chair" buys nothing. Autonomous wander therefore never calls Claude vision unless `PX_WANDER_VISION_ENABLED=1` (off by default, and deliberately an env var rather than a `spark_config.py` constant — that file is px-evolve's whitelisted target, and self-evolution must not be able to propose turning autonomous vision back on). Even enabled, the only trigger left is `wander._vision_trigger`'s novelty escalation: sonar under `NOVELTY_SONAR_CM` (40cm) with **no** Frigate label at all — genuine local ambiguity, not proximity or a new label Frigate already explained — budgeted far tighter than routine perception (`NOVELTY_VISION_COOLDOWN_S`=300s, `NOVELTY_VISION_DAILY_CAP`=5/day, vs. the old eager 30s/50-day figures). Interactive `tool_describe_scene` ("what do you see") is unaffected and always available on demand. Every call — interactive or autonomous — records `origin`, `reason`, `task_id`, `backend` and `model` in `logs/tool-describe_scene.log` via `PX_VISION_ORIGIN`/`PX_VISION_REASON`/`PX_VISION_TASK_ID`, so the call graph is a `grep`/`jq` away rather than reconstructed from memory.

### Cognitive Loop (px-mind)

```bash
bin/px-mind [--awareness-interval 30] [--dry-run]
```

Three-layer architecture:
- **Layer 1 — Awareness** (every 60s, no LLM): sonar + session + calendar + Frigate → `state/awareness.json`
- **Layer 2 — Reflection** (on transition or every 5min idle): the hosted
  cognition tier (`PX_M5_SPARK_MODEL`, an explicit model — `auto` and, on a
  hosted host, `resident` are rejected) handles reflection through a
  process-shared nonblocking gate. Since #308 that tier is **Ollama Cloud**
  (`https://ollama.com`, `deepseek-v4.1-flash:cloud`, `OLLAMA_API_KEY`), not a
  daemon on a LAN machine. `busy` defers without opening the circuit; timeout,
  offline, and malformed responses open a five-minute monotonic circuit.
  Reflection never falls back to Claude or to Pi-local Ollama — the ladder is
  gone, and a cognition-tier failure **defers**. Writes to
  `state/thoughts.jsonl` only after a valid response.
- **Layer 3 — Expression** (30min cooldown; `greet_arrival` bypasses it on a real arrival, 120s anti-flap): dispatches to tool-voice/tool-look/tool-remember and cognitive tools. Valid actions include (wait, greet, greet_arrival, comment, remember, look_at, weather_comment, scan, play_sound, photograph, emote, look_around, time_check, calendar_check, introspect, evolve, morning_fact, research, compose, self_debug, blog_essay, message_obi, set_goal, update_goal, complete_goal). Suppressed during school, quiet time, bedtime (all calendar-driven). **Hardcoded night silence: 19:00–07:00 Hobart time — no speech/audio/motion. Silent cognitive actions (`NIGHT_ALLOWED_ACTIONS`: wait, remember, research, compose, introspect, self_debug, set_goal, update_goal, complete_goal) are exempt and run overnight.**
- **`message_obi` action**: SPARK initiates a direct message to Obi via the dashboard. Exponential backoff: starts at 10min, doubles on unanswered nudge, caps at 4h, resets when Obi replies. Respects all suppressors. **Redaction is a property of the record, not of a call site.** `mind.redact_private_dm()` moves the private text off `thought["thought"]` (replacing it with `mind.PRIVATE_DM_PLACEHOLDER`) the moment the thought exists, onto `PRIVATE_DM_TEXT_KEY` — an in-process delivery field that `without_private_dm_text()` strips from every persistence path and that exactly one consumer reads (`_emit_message_obi`). It is applied twice, idempotently: at record creation in `reflection()` and again at the dispatch boundary in `expression()`, so a record built by anything else is still safe. This shape exists because the previous one — a `display_text` local used only for the thoughts-file write — leaked the raw DM into session history, and from there into the voice-loop prompt (GREMLIN/VIXEN included), the awareness conversation digest, the reflection prompt and `GET /api/v1/session`. **Do not reintroduce a sink that reads `thought["thought"]` expecting raw text.** Pinned by `tests/test_message_obi_redaction.py`.
- **Memory consolidation**: nightly Haiku pass (03:00–06:00 Hobart, ≤2 attempts/day ≥55min apart, state/consolidation_meta.json) distills the last 24h of thoughts into state/memories-spark.jsonl; reflection retrieves the top-3 relevant memories by keyword/tag overlap. **Runs on a background daemon thread with an identity-keyed job marker (`state/consolidation_job.json`), never inline on the tick** — see the health section. Goal persistence in state/intention-spark.json (7-day expiry, one active at a time).

**Critical gotchas:**
- All time-of-day logic uses `ZoneInfo("Australia/Hobart")` — never hardcoded UTC offsets
- Battery emergency shutdown at ≤10% (speaks warning → `sudo shutdown -h now`)
- **Charging detection (`pxh/battery_trend.py`) cannot use adjacent polls.** The pack gains ~0.004V per 30s poll while readings swing up to 0.17V, so differencing measures noise — that bug read `charging: false` through a whole afternoon on the charger. Most of the swing is px-alive's servo load dragging the rail, and load only pulls *down*, so a rolling max recovers resting voltage before a least-squares slope over the window. Thresholds are bootstrapped from a measured trace (0.6% false-charging, 85% detection), deliberately skewed because a false `charging` **suppresses the emergency shutdown**. Detection costs ~10 min, so the plug-in chime lags. Re-tune against a fresh measured trace, never against intuition.
- Single-instance PID guard via `/proc/{pid}` liveness check
- Arrival detection uses module-level `_last_known_findmyhub` cache (not awareness snapshot) — survives M5.local→Pi push outages. Do not replace with snapshot diff. It is also the hysteresis latch's memory (`at_home` there is the *decided* state), and the radii live in `src/pxh/presence.py` — do not re-derive `at_home` from a bare distance threshold anywhere (#305: 277 transitions in 12.6 days from one 34 m-accurate tracker jittering across a single 150 m radius).
- `state/thought-images/` cleaned hourly (images >30 days deleted)

### Epistemic Provenance (`src/pxh/provenance.py`)

Every durable claim in `state/notes[-persona].jsonl` and `state/memories-{persona}.jsonl` records where it came from, so retrieved memory can distinguish what SPARK saw, was told, inferred, or wrote itself.

The six kinds have confidence ceilings clamped on write and read: `observation` and `verification` (1.0), `report` (0.9), `inference` (0.6), `narrative` (0.5), and legacy `unknown` (0.3). The ordering is the safety property. The model never chooses a kind: callers set constants, and consolidation allowlists its input fields. Ceilings deliberately live outside `spark_config.py`, which self-evolution can propose editing.

Writes are strict; reads are lenient. Invalid or legacy data remains readable as `unknown`, without promoting a coarse `source` string into a claim type. Corrections mark supersession without deleting history. Relevance retrieval returns only topical matches (never recent padding); explicit `mode="recent"` remains available. A populated store with no relevant hit does not fall back to raw notes.

### Person Memory (`src/pxh/people.py`)

Facts a person literally stated about themselves, extracted **deterministically**
(regex, no model in the write path) into `state/people-{persona}.jsonl` as
provenance kind `report`. **Reflection must never read this store** — its output
reaches `/api/v1/public/thoughts`, the feed, the blog and Bluesky, so the
separate file *is* the privacy firewall, enforced by the filesystem rather than a
prompt. Pinned by `tests/test_people_invariants.py`; `people.py` and that test
are blacklisted from px-evolve because `mind.py` is a whitelisted target. Design
rationale, TTL policy and the bias-to-rejection matcher are in the module
docstring; the false-positive corpus in `tests/test_people.py` is the spec.
Operator seeding: `bin/px-person-seed` (dry-run by default, `--write` to append)
lands records with `source: operator_seed` + `source_actor` — a seeded fact must
never render as "Obi told me", and the attribution lives in the record itself.

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

**Privacy:** `message_obi` thoughts arrive at `thoughts-spark.jsonl` already redacted — the record's own `thought` field is the placeholder (see the `message_obi` bullet under Cognitive Loop), so px-post, `feed.json`, Bluesky, px-blog and the public thoughts endpoint are structurally safe rather than each responsible for remembering to redact.

### Claude Session Manager

| Session Type | Model | Cooldown | Daily Quota |
|---|---|---|---|
| `evolve` | Opus | 24h | 1/day |
| `self_debug` | Sonnet | 6h | 2/day |
| `research` | Haiku | 2h | 3/day |
| `compose` | Haiku | 4h | 2/day |
| `conversation` | Sonnet | 15min | 4/day |
| `blog` | Haiku | 30min | 5/day |
| `consolidate` | Haiku | 40min | 2/day |

`consolidate` is 40min/2 rather than 20h/1 so `memory.MAX_ATTEMPTS_PER_DAY`'s
second nightly attempt can actually be spent (#291). 40min also clears the
30-min global cooldown, so the retry is *spaced past* it rather than exempted
from it.

**The retry gap is `memory.RETRY_SPACING_S` (55min), not the 40min cooldown
(#310).** They measure different intervals — the cooldown starts when an attempt
*finished*, the spacing when it *started* — and setting them equal is what cost
nine nights of memory: an attempt that spent its whole 600s deadline left the
retry 1800s into a 2400s cooldown, so it was refused on exactly the nights the
first attempt was slow. The spacing has to cover the cooldown *plus* the attempt
that precedes it.

**Deadlines are declared once per kind, at the caller, and the provider's own
HTTP timeout is the ceiling.** `run_model_session`'s `timeout` defaults to
`None`; interactive kinds declare their own (`VOICE_TURN_DEADLINE_S` 45s,
`px-cron-say` 90s, vision 60s, `memory.CONSOLIDATE_DEADLINE_S` via the tier).
The per-kind table this used to defer to (`brain._DEADLINE_S`) was deleted with
the session it belonged to. An override that is *tighter* than the provider's
silently wins and makes the declared value unreachable — an ad-hoc
`timeout=180` did exactly that to `consolidate`'s then-600s: every live failure
measured 180.1s while every success took 30–65s (#291). On a tier call the
binding limit is `PX_M5_SPARK_TIMEOUT_S`, so a caller's larger number is a
comment rather than a budget.

Global: 30min cooldown between calls (except `self_debug`/`blog`), 8/day cap.
Only a call the model actually *answered* arms that cooldown: an rc=1 entry —
`cognition_timeout`, `cognition_offline`, `cognition_bad_response`,
`cognition_busy`, and in the historical log `brain_unavailable` — is a record
that nothing was spent, and letting it lock out every other component for 30
minutes is #310's third defect. When ≤2 remaining: only `self_debug`/`evolve` allowed. Bypass: `PX_MODEL_BUDGET_DISABLED=1`. Live log: `state/model_sessions.jsonl`.

**The day the name changed, the day's accounting reset — once.** `check_budget` reads only the live log, and `state/claude_sessions.jsonl` is the same log under its historical name: written by the retired module, never read here, never rewritten (provenance). So the first deploy of #317 Phase 3's rename starts the day's cap and cooldowns fresh. That is a handful of extra calls on one day; the alternative was a permanent branch reading a file named after a provider that no longer exists.

### Cognition — one tier, one call (`src/pxh/m5.py`)

> ### Hard invariant — no production code invokes Claude
>
> **No `claude -p`. No helper whose implementation is `claude -p`. No resident
> session, no mailbox, no reply tool, no tmux delivery.**
>
> The resident `spark-brain` session was SPARK's sole permitted Claude
> substrate from 2026-08 to 2026-09-16; it is **deleted**, not stopped. Every
> kind that needs a model runs on the cognition tier as one direct API call.
> Enforced by `tools/check_resident_claude.py` in CI (which now also fails if
> any of the retired paths reappears), pinned by
> `tests/test_resident_only_invariant.py`, and both are blacklisted from
> px-evolve.

**Why the rule changed shape rather than being dropped.** The rule was never
"Claude is good" — it was *nothing reaches a language model by an unmeasured
second path*. The second path was a cold `claude -p`, and the reason it was
banned is worth keeping in full, because it is the argument for the tier as
well:

> A cold start costs more to run than the session it claims to be rescuing. A
> resident-brain failure answered by spawning a fresh Claude on the same 4-core
> Pi does not degrade; it amplifies the contention that caused the failure.
> Observed 2026-08-19: a 5-second tmux *delivery* timeout under load was
> reported as "brain unavailable", reflection fell through to `claude -p`, that
> second Claude competed with the voice turn's own `claude -p`, both slowed,
> the 120s timeout tripped, and the ladder continued into Ollama M5 and Ollama
> Cloud (403). A child said "Hey Spark" and waited **151 seconds** for an
> answer. The brain was healthy and idle throughout.

**Why the resident session went too (#317 Phase 3).** It solved the cold-start
problem and introduced a worse one: the transport could lose an answer the
model had already produced. Not theoretically — every item below is an observed
event on this robot:

| what happened | cost |
|---|---|
| the session answered `tool-brain-reply <previous-turn-id>` from its own context | the answer was discarded; the caller waited its full 600s deadline (#314) |
| a `Contains subshell / Do you want to proceed?` dialog appeared in the pane | nobody is attached to answer it; the whole night's consolidation sat behind it (#314) |
| the Claude login expired | **no** supervisor recovery rung could manufacture a credential; `/login` by a human was the terminal action (#311) |
| the supervisor's 02:00 recycle cleared the session's validation marker | every caller refused with `brain_unavailable` at dur=0.0s; nine consecutive nights lost (#310, #278) |

The fix was not a better mailbox. It was to delete the mailbox: **the
application owns transport, the model supplies content.** A direct API call has
no session to be logged out of, no correlation id to copy, no permission dialog
and no reply command, so a produced answer cannot be lost in transit.

**What remains, and where to look:**

- `model_session.run_model_session(kind, prompt, ...)` is the dispatcher
  — the `claude_*` names are provenance and are the next thing to go. Every
  kind it serves is in `_COGNITION_KINDS`; anything else raises
  `ColdStartForbidden`. There is no dial, no `PX_BRAIN_KINDS`, and no second
  destination.
- A cognition kind that asks for `allowed_tools` is **refused**
  (`CognitionTierToolsForbidden`), not quietly answered without them: a
  tool-less answer to a tool-bearing request is indistinguishable from a
  working one.
- Failures are classified (`cognition_timeout`, `cognition_offline`,
  `cognition_bad_response`, `cognition_busy`) and the session log records the
  `provider` that actually served the call, so "which tier spent this" is
  answerable after the fact.
- **Deadlines are declared once per kind, at the caller, and the tier's own
  `PX_M5_SPARK_TIMEOUT_S` is the ceiling.** `run_model_session`'s `timeout`
  defaults to `None`; interactive kinds declare their own
  (`VOICE_TURN_DEADLINE_S` 45s, `px-cron-say` 90s, vision 60s). An override
  that is *tighter* than the provider's silently wins and makes the declared
  number unreachable — an ad-hoc `timeout=180` did exactly that to
  `consolidate`'s then-600s budget, and every live failure measured 180.1s
  while every success took 30–65s (#291). On a tier call the binding limit is
  the HTTP timeout, so a caller's number larger than `PX_M5_SPARK_TIMEOUT_S`
  is a comment, not a budget.

**`self_debug` collects its evidence in Python.** `mind.self_debug_snapshot()`
assembles a bounded snapshot (health board, awareness through the *reflection*
allowlist, the tail of px-mind.log) truncated to `SELF_DEBUG_SNAPSHOT_CHARS`.
It used to ask for `Read,Glob,Grep` and send a model looking through the repo
to answer a question about px-mind's own recent behaviour. Giving a cloud model
repo authority to reach data the caller already has is the trade this avoids.

**`describe_scene` runs on the tier, with the photo inlined** (`pxh.m5`'s
`images`, base64). This **inverts** a property the previous design asserted on
purpose — the old comment read "the image is never inlined into the payload …
it would put the photo through the mailbox, the log and any future outbox
dump", which was true and is now the reason the bytes travel: the mailbox is
what was retired. The scope rule is unchanged and now guards more than it did:
`vision._within_photos` used to bound what a session was *told to read*; it
bounds what leaves the robot, and `vision.MAX_IMAGE_BYTES` (2 MB) bounds how
much of it. `vision.CLAUDE_TIMEOUT` became `DESCRIBE_TIMEOUT_S` in the same
change — a constant named after a provider outlives the provider. The
disclosure surface is deliberately unchanged: photos already left the robot for
a cloud model on this exact path; only the vendor is different.

**`evolve` is disabled, not cold-started.** It needs a git worktree, and a
tool-free API call has no filesystem to widen. `px-evolve` raises
`ColdStartForbidden` — a deliberate outage rather than an exemption.

**The retired paths are forbidden by name**, not merely absent:
`src/pxh/brain.py`, `src/pxh/brain_daemon.py`, `src/pxh/tmux_claude.py`,
`bin/px-claude-session`, `bin/tool-brain-reply`, `bin/px-brain`,
`bin/px-brain-status` and `systemd/px-brain.service` are each a CI failure if
they reappear (`tools/check_resident_claude.py`'s `FORBIDDEN_PATHS`). An `if`
that is currently false can be flipped and a stopped service can be started; a
file that does not exist cannot be reached.

**Readiness is now the HTTP response.** There is no handshake to prove, no
glyph to misread, and no four-state session marker. `ask_m5` returns a
classified `M5Result`; `probe()` answers "is the tier reachable and serving the
configured model" once at px-mind startup. Run
`python tools/check_resident_claude.py --list` for the live debt map.

**Cognition tier availability — the blast radius if the key or the provider lapses.**
The tier is a hosted service (`https://ollama.com`) reached with a bearer token
from `.env`, and there is no local model anywhere beneath it. This is the
question an operator asks at 3am when reflection stops answering, so it is
written down rather than worked out under pressure:

- **Unaffected.** All motion, safety, GPIO, wake-word, TTS and policy layers are
  local Python with no model in the path. A dead tier cannot move the robot,
  cannot speak for it, and cannot talk its way past `validate_action()`.
- **Degraded, never broken.** `reflection` defers and backs off; `post_qa` and
  `blog_qa` skip the item and retry next cycle; `cron_say` drops the slot (five
  more follow today); `voice_turn` retries once on a transport fault and then
  speaks the deterministic acknowledgement; `describe_scene` returns "I couldn't
  see anything right now."; `research`/`compose`/`blog`/`consolidate`/`self_debug`
  log and skip. **SPARK goes quiet and blind on demand; he does not brick.**
- **Nothing escalates.** Every one of those is a deferral, and there is no
  second destination to defer *to* — no resident session, no local model, no CLI
  (#308, #317). A failure reduces work; it does not search wider.
- **Two failure shapes worth telling apart.** A *missing* credential fails
  closed and says so every time; a *rejected* one is reported with the provider's
  own words (#324). And an `offline` that lasts exactly five minutes is the
  shared circuit, not the network — check `state/m5/circuit.json` and
  `state/m5/meter.json` before believing anything else.

**Historical detail is in git, not here.** The mailbox layout
(`state/brain/<session>/{inbox,outbox,dead}`, `current.json`,
`validation.json`), the handshake protocol, the supervisor's recycle and wedge
detection, and the `Read`-envelope argument all lived in this file until
#317 Phase 3 deleted the machinery they described. They are recoverable with
`git log -p CLAUDE.md`, and the design docs are in
`docs/superpowers/specs/` and `docs/superpowers/plans/`.

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

**Arrival hysteresis (#305).** `at_home` is a *latched state*, not a threshold verdict on one fix: enter at `presence.ENTER_RADIUS_KM` (0.15 km), leave only past `presence.EXIT_RADIUS_KM` (0.30 km) **and only on `presence.EXIT_CONFIRMATIONS` (2) consecutive usable far fixes**, and ignore a fix whose `accuracy_m` is worse than the radius the applicable test uses. The confirmation is only on the leaving side — a departure greets nothing, so waiting there is free, while confirming arrivals would trade a spurious greeting for a late one. `mind._detect_findmyhub_arrivals` fires only on `at_home is True` after `at_home is False`: **"unknown" must never read as "away"**, or the first sighting after a restart becomes a fabricated departure (2026-09-17: one ±100 m far fix right after a deploy produced a real greeting for an arrival that never happened). `mind._enrich_tracker` therefore sets no `at_home` at all for coordinate trackers — `mind._latch_findmyhub_states` decides, because it is the only place that knows the previous state, and the edge detector plus `awareness["findmyhub"]` both read that one latched value. `voice_loop`'s prompt label uses `presence.describe_location`'s bands ("at home" / "near home" / "N km from home") rather than its own threshold, so the prompt cannot disagree with the state SPARK acts on. Raising `GREET_ARRIVAL_COOLDOWN_S` is *not* the fix: it damps speech while leaving every other reader oscillating. Suppressed flips are counted per latch (`mind._latch_suppressed`) and reported on the next real transition — one or two lines per event, never one per tick. Pinned by `tests/test_presence.py` and the `#305` block in `tests/test_mind_utils.py`.

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
| `px-post` | `bin/px-post` | pi | always, 30s |
| `px-api-server` | `bin/px-api-server` | pi | always, 2s |
| `px-frigate-stream` | `bin/px-frigate-stream` | pi | always, 10s |
| `px-evolve` | `bin/px-evolve` | pi | on-failure, 30s |
| `px-blog` | `bin/px-blog` | pi | on-failure, 30s |
| `px-tts-glados` | GLaDOS TTS :7861 | pi | always, 10s |
| `px-io-attrib` | `bin/px-io-attrib` | root | always, 30s |
| `cloudflared` | Tunnel → spark-api.wedd.au | pi | always, 10s |

**`px-io-attrib` is root on purpose**, and it is an instrument rather than a
control loop: it samples nothing until io PSI crosses 40 % or `px-alive`'s
heartbeat passes 7 s of its 15 s watchdog, then takes one bounded `/proc`
snapshot and stops for 60 s (`docs/operations/io-attribution.md`). Root is what
lets it read `/proc/<pid>/io` for px-alive and journald; run unprivileged it
still records who was *stalled* (D state, `wchan`, run delay) but says so in
`writers_unavailable_reason` instead of reporting a writer list that looks
empty. It exists because #247/#283/#287 are one transient storage stall with
three faces, and no fix — weighting, journal tuning, or the heartbeat write
path — is choosable until a writer is named.

## Safety Model

- `PX_DRY=1` (or `--dry-run`) skips all motion and audio. **Default is live when unset.**
- `confirm_motion_allowed: false` in session state blocks motion tools regardless of dry mode
- All tools must be in `ALLOWED_TOOLS` in `voice_loop.py`
- Parameter ranges hard-validated in `validate_action()` (speed 0–60, duration 1–12s, etc.)

### Behavioural Policy (#174) — `src/pxh/policy.py`

Quiet mode, night silence and on-call/hot-mic suppression are *behavioural*
invariants: they hold regardless of which prompt, persona or dispatcher proposed
the action. This matters because `voice_loop.py`'s persona swap **replaces** the
system prompt rather than supplementing it, so any safety behaviour that lives
only in prose vanishes the moment GREMLIN or VIXEN is active.

`policy.evaluate()` is the rule and only the rule — pure, no I/O, no clock, no
imports from the dispatchers, and it never executes anything. Callers classify
their own vocabulary into an `Effect` and pass the context in.

**Three enforcement points. The third is the one that closes the hole:**

| Site | Origin | On a blocked verdict |
|---|---|---|
| `voice_loop.validate_action()` | `interactive` | downgrade to a presence-safe substitute |
| `mind.expression()` | `autonomous` | drop the action |
| **`bin/tool-voice`** | `interactive` | `{"status":"suppressed","reason":…}`, exit 0 |

The first two are dispatchers, so they only bind callers that go *through* a
dispatcher. `bin/tool-voice` is the sink every speech producer funnels into
(`tool-chat`, `tool-chat-vixen`, `tool-voice-persona`, `px-cron-say`,
`px-battery-poll`), and it is also what anything holding a shell reaches. The
strongest such thing used to be the resident `spark-brain` session, whose tool
envelope was SPARK's own `bin/`; that session is gone (#317 Phase 3) and the
argument did not depend on which process was holding the shell. Before the sink
gate, prose in a system prompt was the only thing between a tool-bearing model
and the speaker at 3am. The upstream checks stay as defence in depth; **do not
remove one because the other exists.**

**The sink pins `origin` and `effect` rather than accepting them.** A sink
cannot know its caller — that is precisely why it needs its own gate —
`interactive` is the stricter of the two origins so a wrong guess can only ever
suppress, and a caller that could declare its own effect could declare its way
out of the gate entirely.

**The gate sits above both the persona reroute and the `PX_DRY` branch.**
`tool-voice-persona` re-enters `tool-voice`, so a gate below the reroute would
still catch the audio — but only after an Ollama round trip (now a hosted one,
since #308) on text that was never going to be spoken. And a dry run must model the live decision, or every
dry test of a speaking route asserts behaviour the robot will not show.

`src/pxh/policy_context.py` is the **only** loader of the session/awareness/clock
facts `policy.evaluate()` refuses to read for itself; the dispatcher and the sink
both go through it so the two cannot drift. **Its two reads have opposite failure
postures, and that is the point.**

- **Session — fails closed.** `load_session_for_policy()` returns a
  `SessionRead(data, available)`, never a bare dict, and `policy.evaluate()`'s
  rule 0 suppresses audio when `available` is false. A `{}` cannot carry both
  "no quiet flag set" and "no idea": quiet mode is the dysregulation protocol,
  so resolving the second into the first grants permission to speak during a
  meltdown on the strength of a failed file read. The earlier fail-open posture
  argued a contended lock would otherwise mute SPARK under load; it bought no
  such thing, since `tool-voice` calls `update_session()` on that same lock a
  few lines later and dies there — pre-fix, contention produced an utterance
  *and* a traceback. The `except` in the loader is deliberately broad because
  failing closed cannot permit anything; every failure prints to stderr.
- **Awareness — fails open.** An unreadable snapshot yields `{}` and the
  on-call/hot-mic rule goes inactive, rather than muting SPARK for as long as
  px-mind is down. `awareness.json` is written by a daemon that is routinely
  down; the session is written by whatever is running. Quiet mode and night
  silence read nothing from this file.

Both dispatchers still fail open on their own session read (`voice_loop.py` and
`mind.py` catch `FileLockTimeout` into `{}` — they do not use this loader for the
session). That is now backstopped rather than load-bearing: every audio action
they dispatch funnels through the sink, which re-reads and fails closed.

Pinned by `test_direct_tool_voice_is_silent_while_the_session_lock_is_held` and
`test_direct_tool_voice_is_silent_when_the_session_cannot_be_read`, which assert
against a canary player script on disk rather than against tool-voice's own
JSON — a sink that speaks and then crashes prints no self-report at all.

Night-window bounds come from `spark_config.night_silence_bounds()`, which
honours `PX_NIGHT_SILENCE_START_H`/`_END_H`. That seam is load-bearing for the
suite: without it every subprocess test of a speaking tool would pass by day and
return `suppressed` after 19:00 Hobart. `tests/conftest.py` pins the window shut
(`START=99`) for `isolated_project`; tests that mean to exercise night silence
override both values.

**Still ungated, deliberately and on the record:** `bin/tool-play-sound`,
`bin/px-perform`, `px-wake-listen`'s chimes, `wander._speak()`,
`mind._play_alarm_beeps()`, and `px-battery-poll`'s plug/unplug sweep.
`tool-announce` self-gates at its own relay chokepoint, and since it now reads
`policy.is_night_hour()` the Nest path and the onboard speaker cannot disagree
about when night is — but it still enforces night silence *only*, not quiet mode
or on-call.
All of these are inventoried in `tests/test_policy_invariants.py::AUDIO_PRODUCERS`
with a disposition each, and **a new file that reaches `aplay`/`espeak`/a TTS
endpoint fails `test_every_audio_producer_is_inventoried` until someone
classifies it.** That test is the tripwire against the next silent bypass, not a
formality — a `delegates` claim is re-verified against the file rather than
trusted.

`src/pxh/policy.py` and `tests/test_policy_invariants.py` are blacklisted from
px-evolve (see `model_session.BLACKLIST_FILES`). Evolvable policy coverage
lives in `tests/test_policy.py` — keep that split.

### Delegated-Agent Authority Boundary (#281)

A delegated/research Claude Code agent must be *mechanically* unable to
touch production systemd, GPIO, live audio/wake hardware, or state — a
prose instruction not to is not a boundary, and one delegated fork already
took live physical action outside the scope it was given. `spark-investigator`
(`.claude/agents/spark-investigator.md`) is a restricted subagent type
whose `tools:` field is a hard, harness-enforced allowlist (`Read, Grep,
Glob, WebSearch, WebFetch` — no `Bash`/`Write`/`Edit`/`Agent`), pinned by
`tools/check_investigator_agent.py` + `tests/test_agent_authority_invariant.py`
and blacklisted from px-evolve on the same footing as the resident-only
Claude pair. `pi`'s sudoers grant was also tightened from blanket
`NOPASSWD: ALL` to exact absolute commands/units only. This is defence in
depth, not a full security boundary — the agent still runs as the same
Unix user under the same harness — see `docs/operations/agent-authority.md`.
Issue #281 stays open for the deferred OS-level separate-identity work — see
`docs/operations/agent-os-isolation-design.md` for the phase-2 design
(a `DynamicUser=` `spark-research` principal, invoked via a root-owned
`px-signal-alive`-style launcher, read-only checkout, `state/`/`.env`
inaccessible) and a working unprivileged-`bwrap` prototype
(`tools/prototypes/agent-os-isolation/`) proving the sandboxing mechanism —
not yet implemented; no live user/group/sudoers change has been made for it.

**Elevation rides root-owned launchers, never variable sudo lines (#300).**
An exact sudoers rule matches the literal command line, so
`sudo -n env PYTHONPATH=... px-*` can never match one — the #281 tightening
silently broke all 12 GPIO tools *and* the battery emergency shutdown (bare
`shutdown` resolves to `/usr/sbin/shutdown`, the rule said `/sbin/`). All
GPIO/audio elevation now goes through `/usr/local/sbin/px-gpio-run` (source:
`systemd/sbin/px-gpio-run`; sudoers source: `systemd/sudoers.d/`), which
validates its target allowlist and chooses its own environment; `systemctl`/
`shutdown` are always spelled absolutely. Pinned by
`tests/test_sudo_invariants.py` — a new `sudo env`/bare-name call fails it.

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
4. Add to `docs/prompts/voice-system.md` (and codex version)
5. Add to `docs/prompts/persona-gremlin.md` and `persona-vixen.md`
6. Add a dry-run test in `tests/test_tools.py` using the `isolated_project` fixture

Every tool must: emit a single JSON object to stdout, support `PX_DRY=1`, handle errors as `{"status": "error", "error": "..."}`.

## Key Environment Variables

Non-obvious variables only — most names are self-documenting. Full list in `bin/px-env` and `.env.example`.

| Variable | Purpose |
|---|---|
| `PX_DRY` | `1` = dry-run. **Default is live when unset.** |
| `PX_BYPASS_SUDO` | `1` = skip sudo (tests only) |
| `OLLAMA_API_KEY` | Bearer token for Ollama Cloud. Read by `pxh.m5`, `tool-chat`, `tool-chat-vixen`, `tool-voice-persona` and `codex-ollama`; also the variable the `ollama` CLI itself reads. Required whenever the host is not local. The legacy `OLLAMA_CLOUD_API_KEY` (what the live robot's `.env` already holds) is accepted as a fallback so no credential has to be copied or renamed to deploy; `PX_M5_SPARK_API_KEY` takes precedence over both for the cognition tier. |
| `PX_M5_SPARK_HOST` | Cognition-tier endpoint. Default `https://ollama.com` (Ollama Cloud) — **not** `https://api.ollama.com`, which 403s. Point it at an `http://…:11434` daemon to borrow a LAN model instead; that is the only case where `resident` is legal. |
| `PX_M5_SPARK_MODEL` | Cognition model for reflection, public/Obi chat, publication QA, and the migrated kinds (`research`, `compose`, `blog`, `consolidate`, `self_debug` — #317): an explicit model; `auto` is rejected, and `resident`/`resident-only` are rejected against a hosted host (#308). Default per `.env.example`: `deepseek-v4.1-flash:cloud`. |
| `PX_M5_SPARK_API_KEY` | Overrides `OLLAMA_API_KEY` for the cognition tier only. |
| `PX_M5_SPARK_TIMEOUT_S` | Cognition-tier request timeout, default 60s (a hosted tier needs more headroom than a warm LAN daemon, and a timeout opens the five-minute circuit). |
| `PX_OLLAMA_HOST` / `PX_CHAT_MODEL` | Persona chat and rephrase endpoint/model (GREMLIN/VIXEN, `tool-voice-persona`). Default `https://ollama.com` / `deepseek-v4.1-flash:cloud`. |
| `PX_MIND_BACKEND` | Legacy introspection field; it no longer alters reflection routing. |
| `PX_WANDER_VISION_ENABLED` | `1` = allow autonomous wander to escalate to cognition-tier vision on genuine local ambiguity (off by default — see Wander below) |
| `PX_MODEL_BUDGET_DISABLED` | `1` = bypass every rate limit in `model_session` (was `PX_CLAUDE_BUDGET_DISABLED`) |
| `PX_MODEL_DAILY_CAP` / `PX_MODEL_COOLDOWN_S` | Global daily cap (8) and inter-call cooldown (1800s). Provider-neutral, because they always bounded model calls rather than Claude calls. |
| `PX_MODEL_<KIND>` | Provider-neutral per-kind model override for a migrated kind (`PX_MODEL_RESEARCH`, `PX_MODEL_COMPOSE`, `PX_MODEL_BLOG`, `PX_MODEL_CONSOLIDATE`, `PX_MODEL_SELF_DEBUG`). Falls back to `PX_M5_SPARK_MODEL`. |
| `PX_EVOLVE_DRY` | `1` = skip worktree/PR (queue entry still written with `dry: true`) |
| `PX_POST_QA` | `0` = skip Claude QA gate (testing) |
| `PX_HA_DEBUG` | `1` = verbose HA fetch logging |
| `PX_HOME_LAT` / `PX_HOME_LON` | Home coords for Find Hub at-home detection (defaults: `-43.13567`, `147.11840`) |
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
