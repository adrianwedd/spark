# CLAUDE.md — SPARK's Constitution

This file is the **rules and the map**. It holds the cross-cutting invariants
you must not break, and it says where current truth for each subsystem lives.

It deliberately contains **no** subsystem detail, no incident history, and no
tuning constants. Those go stale, and a constitution that goes stale stops
being read. If you need to know how something works, follow a link — the
linked doc is the authority, and this file is not.

---

## What this is

A SunFounder PiCar-X running on a Raspberry Pi, built by Adrian and Obi
together — *with* Obi, not for him. **SPARK** is its default persona: a warm,
non-coercive companion for a neurodivergent child, with a three-layer cognitive
architecture, a voice loop, and two jailbroken alternate personas.

Two consequences follow from that sentence, and most of the rules below are
downstream of them:

1. **This checkout is the running robot.** Daemons are reading these files
   while you edit them, and `master` deploys on merge.
2. **A real child is the user.** Quiet mode is a dysregulation protocol, not a
   preference. Location data is a child's location. Silence is sometimes the
   correct output.

---

## The invariants

### 1. Failure-First

Observe and reproduce before you theorise. Preserve the evidence — logs, the
failing state file, the exact command — before you change anything, because
the fix destroys the reproduction.

**Verify the actual failing path**, not a path that resembles it. A green unit
test for a function does not prove the subprocess that calls it under `sudo`
works. Most bugs on this robot have been silent successes: `aplay` exits 0 and
plays nothing, `claude` runs as the wrong user and returns a fallback string,
a poll loop times out and reports success. **If a component reports success,
that is a claim to be checked, not evidence.**

### 2. `master` is code truth; GitHub Issues are work identity

Branch off `master`. A merge to `master` deploys to the robot and publishes the
public site. A change's identity is its issue number — not its branch, not a
plan document, not a memory file.
→ [docs/git-workflow.md](docs/git-workflow.md)

### 3. Never blanket-stage

`git add -A`, `git add .`, `git add -u`, `git commit -a` are **forbidden**.
Stage exact owned paths, then read `git diff --cached` before committing.

**Preserve unrelated dirty work.** The tree routinely carries someone else's
half-finished change or a daemon's runtime output. If it overlaps what you must
edit, commit it first as its own labelled commit — never absorb it.
→ [docs/git-workflow.md](docs/git-workflow.md)

### 4. Semantic intelligence proposes; deterministic code constrains

An LLM chooses what SPARK should do. Code decides whether it may. Policy,
privacy, authority, and execution limits are implemented as pure functions and
chokepoints, never as instructions to a model.

Corollary: **a safety property that can only be enforced by asking a model
nicely is not enforced.**
→ [docs/architecture/policy-and-authority.md](docs/architecture/policy-and-authority.md)

### 5. Prompts and personas are cognition and style — never enforcement

`voice_loop.py`'s persona swap **replaces** the system prompt rather than
supplementing it. Anything protective that lives only in prose vanishes the
moment GREMLIN or VIXEN is active.

Never fix a safety bug by editing a prompt. Never document a safety property as
though a prompt were its mechanism.
→ [docs/architecture/policy-and-authority.md](docs/architecture/policy-and-authority.md)

### 6. Unknown fails closed; authority is least-privilege

When trust or authority cannot be established, the answer is no.

- A session that cannot be read is treated as quiet mode, not as "quiet mode
  off". Unknown resolves the same way as known-restrictive.
- An unclassified request kind routes to the **unprivileged** session. The
  default must make forgetting safe.
- Guard with an **allowlist**, not a denylist. Forgetting to add a key should
  mean the data is absent, not exposed.

The one deliberate exception is documented and narrow: an unreadable
`awareness.json` fails *open*, because it is written by a daemon that is
routinely down and failing closed there would mute SPARK indefinitely.
→ [docs/architecture/policy-and-authority.md](docs/architecture/policy-and-authority.md),
[docs/architecture/privacy.md](docs/architecture/privacy.md)

### 7. Runtime state is not source code

`state/` is the robot's living state and is almost entirely untracked. **Never
commit a runtime state file.** Data rewritten every loop belongs on tmpfs, not
the SD card. Durable writes go through `atomic_write()`.
→ [docs/operations/state-and-runtime.md](docs/operations/state-and-runtime.md)

### 8. Tests are hermetic by default

The suite runs on the robot it controls. Unless explicitly marked `live`, a
test must not read or write live state, make a billed call, reach the network,
or touch hardware. A test that samples the live robot is not flaky — it is
wrong, and it changes the thing it measures.
→ [docs/testing.md](docs/testing.md)

### 9. Targeted green is not repository green

Running `-k` on what you touched proves your change, not the repository. Run
the full suite before claiming done.

And a green suite proves nothing about live hardware, the resident tmux
sessions, trust boundaries, or anything behind `sudo`. **Those require explicit
live proof** — a real run, with its output quoted. Never infer them.
→ [docs/testing.md](docs/testing.md)

### 10. Historical documents are evidence, not truth

`docs/superpowers/specs/`, `docs/superpowers/plans/`, `docs/historical/`, PR
bodies, and old session notes record what was decided and why, on the date in
the filename. None of it is maintained. **Do not implement from them and do not
cite them as current behaviour.**
→ [docs/superpowers/README.md](docs/superpowers/README.md)

---

## Where current truth lives

| Topic | Doc |
|---|---|
| System map, layering, dependency direction | [architecture/overview](docs/architecture/overview.md) |
| Resident Claude sessions, mailbox, readiness | [architecture/resident-brain](docs/architecture/resident-brain.md) |
| Behavioural policy, trust boundaries, evolution limits | [architecture/policy-and-authority](docs/architecture/policy-and-authority.md) |
| Where a claim came from, and how far to trust it | [architecture/provenance](docs/architecture/provenance.md) |
| Location, private messages, chat sanitisation | [architecture/privacy](docs/architecture/privacy.md) |
| Memories, goals, lived-experience adaptation | [architecture/memory-and-learning](docs/architecture/memory-and-learning.md) |
| GPIO exclusivity, px-alive, leases | [hardware/gpio-and-alive](docs/hardware/gpio-and-alive.md) |
| Cliff guard, exploration safety | [hardware/wander-safety](docs/hardware/wander-safety.md) |
| Capture, playback, TTS, the mic | [hardware/audio-and-mic](docs/hardware/audio-and-mic.md) |
| Battery, charge detection, emergency shutdown | [hardware/power](docs/hardware/power.md) |
| State classes, atomic writes, locks, tmpfs | [operations/state-and-runtime](docs/operations/state-and-runtime.md) |
| Daemon health reporting | [operations/health](docs/operations/health.md) |
| LLM tiers, budgets, metering, spend | [operations/llm-routing](docs/operations/llm-routing.md) |
| Services, install, API, site, relay | [operations/deployment](docs/operations/deployment.md) |
| Isolation, live tests, structural tripwires | [testing](docs/testing.md) |
| Branching, staging, commits | [git-workflow](docs/git-workflow.md) |

Per-script and per-module reference: [docs/SCRIPTS.md](docs/SCRIPTS.md).
Written for Obi: [docs/how-sparks-brain-works.md](docs/how-sparks-brain-works.md).

---

## Read this before touching that

| If you are changing… | Read first |
|---|---|
| anything that can produce sound | [policy-and-authority](docs/architecture/policy-and-authority.md) — a new audio producer fails the suite until it is classified |
| a system prompt or persona | [policy-and-authority](docs/architecture/policy-and-authority.md) §prompts, and [resident-brain](docs/architecture/resident-brain.md) §editing |
| anything reading `awareness` | [privacy](docs/architecture/privacy.md) — a new key stays out of the reflection prompt until allowlisted |
| a durable write to notes/memories | [provenance](docs/architecture/provenance.md) — kinds and ceilings |
| anything that opens `Picarx` | [gpio-and-alive](docs/hardware/gpio-and-alive.md) — yield or lease first |
| the cliff guard | [wander-safety](docs/hardware/wander-safety.md) — every layer earned its place |
| microphone capture | [audio-and-mic](docs/hardware/audio-and-mic.md) — never PyAudio |
| charge detection thresholds | [power](docs/hardware/power.md) — re-tune only against a measured trace |
| a file written every loop | [state-and-runtime](docs/operations/state-and-runtime.md) — tmpfs, not the SD card |
| `conftest.py` or an autouse fixture | [testing](docs/testing.md) — each one exists because a run changed the robot |
| a `bin/tool-*` | the six-step checklist below |

### Adding a tool

1. `bin/tool-<name>` — bash + embedded Python heredoc, following the neighbours
2. `ALLOWED_TOOLS` **and** `TOOL_COMMANDS` in `src/pxh/voice_loop.py`
3. a `validate_action()` branch that hard-validates params into env vars
4. `docs/prompts/claude-voice-system.md` and the codex version
5. `docs/prompts/persona-gremlin.md` and `persona-vixen.md`
6. a dry-run test in `tests/test_tools.py` using `isolated_project`

Every tool must emit a **single JSON object** to stdout, honour `PX_DRY=1`, and
report errors as `{"status": "error", "error": "..."}`.

---

## Working here

```bash
source .venv/bin/activate                 # development and tests
cp state/session.template.json state/session.json   # first use only
python -m pytest                          # the real gate; run it in full
python -m pytest -m "not live"            # skip hardware
bin/px-brain-status                       # before touching the resident sessions
bin/px-diagnostics --no-motion --short    # quick health check
```

**`bin/` scripts run under `/usr/bin/python3`**, not the venv — `picarx` and
`robot_hat` live in system site-packages. `bin/px-wake-listen` is the exception.

**Safety defaults you must know:** `PX_DRY=1` skips motion and audio, and
**the default is live when it is unset.** `confirm_motion_allowed: false` in
session state blocks motion tools regardless of dry mode. `PX_BYPASS_SUDO=1` is
for tests only.

Full environment variable list: `bin/px-env` and `.env.example`. Note that
`.env` is loaded by `bin/px-mind` and some daemons, **not** by `bin/px-env` —
a tool run standalone may silently lack its secrets.
