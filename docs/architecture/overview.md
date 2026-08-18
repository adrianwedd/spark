# Architecture Overview

**Owns:** the map. What the parts are, which direction they depend on, and
where each one is documented in full. No subsystem detail lives here — this
page exists so you can find the page that has it.

---

## Invariant

### One robot, four concentric layers

```
  hardware          picarx / robot_hat / GPIO / I2C / ALSA
     ^
  daemons           px-alive, px-wake-listen, px-battery-poll, px-mind,
                    px-brain, px-post, px-api-server, px-blog, px-evolve
     ^
  library           src/pxh/  — the logic; importable and testable
     ^
  surfaces          bin/px-*  (humans), bin/tool-*  (LLM dispatchers),
                    REST API, MCP server, site
```

**Logic lives in `src/pxh/`, never in a `bin/` script.** `bin/` scripts are
thin: they source `bin/px-env`, resolve privileges, and call into the library
or a tool. The rule exists so behaviour can be tested in-process; a rule
implemented inside a bash heredoc can only be tested by spawning a subprocess,
and subprocess tests are the slowest and flakiest part of the suite.

`src/pxh/wander.py` is the worked example: `bin/px-wander` is a thin wrapper
(privilege elevation, calibration guard) around an importable engine.

### The three cognitive layers

SPARK's autonomy is `px-mind` (`src/pxh/mind.py`), and it is three layers with
different costs and different failure postures:

| Layer | Cadence | LLM? | Writes |
|---|---|---|---|
| 1 — Awareness | every 60s | no | `state/awareness.json` |
| 2 — Reflection | on transition, or every 5 min idle | yes | `state/thoughts.jsonl` |
| 3 — Expression | gated by cooldown + suppressors | no | dispatches a `bin/tool-*` |

Layer 1 must never call an LLM: it is the layer that still works when the
network is down, and everything above it reads its snapshot.

Layer 3 is **budgeted and suppressed**, not free-running:
`EXPRESSION_COOLDOWN_S` (1800s) is the minimum gap between spontaneous
utterances. `greet_arrival` bypasses that budget on a real arrival, with its
own 120s anti-flap. Expression is suppressed entirely during school, quiet
time and bedtime (all calendar-driven), and during night silence — see
[policy-and-authority](policy-and-authority.md).

Layer 3 never touches GPIO directly. Every physical act routes through a
`bin/tool-*`, which is what makes the policy sink in
[policy-and-authority](policy-and-authority.md) a real chokepoint rather
than an advisory one.

### Dependency direction is one-way

`policy.py` imports neither `mind.py` nor `voice_loop.py`. Both dispatchers
import `policy.py`. The same holds for `provenance.py`, `health.py`,
`state.py`, and `runtime_paths.py` — leaf modules that many callers share.

A new module that needs to import a dispatcher is a design smell: it means the
rule you are writing belongs in the dispatcher, or the shared part belongs in a
new leaf.

---

## Where each subsystem is documented

| Subsystem | Doc |
|---|---|
| Resident Claude session (the brain) | [architecture/resident-brain](resident-brain.md) |
| Behavioural policy, trust boundaries, self-evolution limits | [architecture/policy-and-authority](policy-and-authority.md) |
| Epistemic provenance of durable claims | [architecture/provenance](provenance.md) |
| Location, private messages, chat sanitisation | [architecture/privacy](privacy.md) |
| Memories, goals, lived-experience adaptation | [architecture/memory-and-learning](memory-and-learning.md) |
| GPIO exclusivity, px-alive, leases | [hardware/gpio-and-alive](../hardware/gpio-and-alive.md) |
| Cliff guard and wander safety | [hardware/wander-safety](../hardware/wander-safety.md) |
| Capture, playback, TTS | [hardware/audio-and-mic](../hardware/audio-and-mic.md) |
| Battery, charge detection, emergency shutdown | [hardware/power](../hardware/power.md) |
| Session state, atomic writes, tmpfs runtime | [operations/state-and-runtime](../operations/state-and-runtime.md) |
| Daemon health reporting | [operations/health](../operations/health.md) |
| LLM tiers, budgets, metering | [operations/llm-routing](../operations/llm-routing.md) |
| Services, install, tunnel | [operations/deployment](../operations/deployment.md) |
| Test isolation and hermeticity | [testing](../testing.md) |

Per-script and per-module reference — every file in `bin/` and `src/pxh/` —
lives in [docs/SCRIPTS.md](../SCRIPTS.md). This page does not duplicate it.

A plain-language explanation written for Obi is
[docs/how-sparks-brain-works.md](../how-sparks-brain-works.md).

---

## Why it looks like this

*History, not rule.*

The layering was not designed up front. `px-mind` began as a single loop that
called an LLM on every tick; splitting awareness out was forced by network
outages, during which the robot went completely blind rather than merely
quiet. The "layer 1 never calls an LLM" rule is that outage written down.

The `bin/` → `src/pxh/` extraction happened subsystem by subsystem, and
`wander.py` was the one that made the case: the cliff guard could not be
regression-tested at all while it lived inside a bash heredoc, and it was the
part of the robot most likely to drive itself off a table.
