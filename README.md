# SPARK

**A robot that stays around.**

SPARK is a persistent embodied agent built on a SunFounder PiCar-X. It keeps one resident brain, perceives continuously through local sensors, remembers where durable claims came from, uses local M5/Ollama cognition for routine work, and puts deterministic policy between model proposals and anything that can affect the world.

Built by Adrian and Obi together.

- Public site: https://spark.wedd.au
- Current roadmap: [`docs/ROADMAP.md`](docs/ROADMAP.md)
- Architecture: [`docs/architecture/`](docs/architecture/)
- Operations: [`docs/operations/`](docs/operations/)
- Agent/operator constitution: [`CLAUDE.md`](CLAUDE.md)

## What SPARK is

SPARK is not a fresh chatbot invocation attached to wheels. The production system keeps existing between conversations:

```text
continuous local perception
        ↓
M5 / Ollama cognition for routine, background and untrusted text
        ↓
one resident Claude brain when richer conversation or semantic vision is needed
        ↓
deterministic policy, authority and tool validation
        ↓
voice · movement · memory · perception · publishing
```

The important boundary is architectural, not prompt-based: **semantic intelligence proposes; deterministic machinery constrains.** Model output cannot grant itself motion authority, rewrite epistemic provenance, bypass quiet/night policy, or promote untrusted public text into the privileged brain.

## Current cognition routes

| Work | Route | Failure behaviour |
|---|---|---|
| Direct SPARK voice turn | resident `spark-brain` | bounded retry / deterministic local acknowledgement; no second model |
| Semantic scene description | resident `spark-brain` | fail/defer |
| Reflection | M5/Ollama | defer; never Claude fallback |
| Public chat | M5/Ollama, no tools | fast unavailable response |
| Obi dashboard chat | M5/Ollama, no tools | fast unavailable response |
| Post/blog QA | deterministic checks + M5 | defer publication |

Production forbids cold `claude -p` paths. CI enforces the resident-only invariant.

### M5 resident-mode borrowing

SPARK can use whatever Ollama model is already resident on M5 rather than evicting Adrian's workload. `PX_M5_SPARK_MODEL=resident` consults `/api/ps`, never `/api/tags`; if nothing is resident it may use the explicitly configured fallback. M5 work is non-queuing and circuit-broken so background cognition backs off instead of turning contention into a cascade.

## Knowing versus guessing

Durable records retain epistemic provenance. An `observation`, human `report`, `model_perception`, `inference`, first-person `narrative`, and `verification` are different kinds of evidence with different confidence ceilings.

A camera/model guess does not become a sensor observation because the prose sounds confident. Learned contextual preferences likewise require grounded repeated evidence and retain contradiction/history rather than silently rewriting the past.

See [`docs/architecture/provenance.md`](docs/architecture/provenance.md).

## The body

SPARK runs as a set of long-lived services rather than one giant agent process. Important pieces include:

- `px-brain` — supervises the single resident Claude session.
- `px-mind` — awareness → reflection → expression loop; routine reflection is M5-local.
- `px-wake-listen` — always-on microphone/wake/STT path using `arecord`.
- `px-alive` — persistent hardware presence and GPIO coordination.
- `px-api-server` — REST API and dashboard.
- `px-frigate-stream` — local camera stream for Frigate perception.
- `px-post` / `px-blog` — publication pipelines with privacy and M5-local QA gates.
- `px-battery-poll` — battery and shutdown protection.
- `px-tts-glados` — optional GLaDOS TTS service for non-SPARK personas.

Each major daemon has functional health evidence and a systemd resource envelope. A service should fail inside its own cgroup before it can push the whole Pi into a host-wide resource failure.

## Perception

Continuous perception stays local wherever possible:

- sonar and grayscale for immediate physical state;
- Frigate/Hailo for local object/person detection;
- ambient audio state;
- Home Assistant and other household context where available.

Claude vision is sparse semantic escalation, not routine surveillance. Autonomous vision is off by default and tightly budgeted when enabled; explicit “what can you see?” requests remain available.

## SPARK and Obi

SPARK was built as a non-coercive companion for a neurodivergent child, not as an authority figure. The interaction model emphasises connection before direction, declarative language, transition support, quiet/silence when overloaded, and interest-led engagement.

The child-facing relationship does **not** imply child-facing system authority. Public/Obi text is routed through a no-tools local model boundary and cannot obtain privileged `spark-brain` or shell/tool authority through prompt injection.

## Safety and authority

Load-bearing constraints live in code:

- explicit motion gates and parameter validation;
- tokenised GPIO leases and deterministic ownership;
- audio sink policy for quiet/night/on-call state;
- fail-closed reads where policy evidence is unavailable;
- route allowlists for resident Claude;
- no production cold-Claude fallback;
- privacy filtering before public disclosure;
- human review remains the authority for code changes/self-evolution.

Prompts/personas shape cognition and style. They are not enforcement.

## Development

```bash
source .venv/bin/activate
python -m pytest tests/test_some_area.py -q
```

Use targeted tests locally. GitHub CI runs the full non-live suite and is the repository gate. Do **not** run the full suite on the production Pi merely as a pre-commit ritual.

For coding agents, read [`CLAUDE.md`](CLAUDE.md) first. Its execution default is: **act, verify, continue** once a goal is authorised, while preserving physical safety, privacy/authority boundaries, unrelated dirty work, and exact-path staging.

## Documentation map

- [`CLAUDE.md`](CLAUDE.md) — current engineering constitution and operational invariants.
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — current product/technical trajectory; Issues are the work ledger.
- [`docs/architecture/`](docs/architecture/) — canonical architecture and provenance explanations.
- [`docs/operations/`](docs/operations/) — containment, deployment and operator evidence.
- [`docs/cost-invariants.md`](docs/cost-invariants.md) — how SPARK avoids hidden paid inference paths.
- [`docs/SCRIPTS.md`](docs/SCRIPTS.md) — script/module inventory (still being completed under #216).
- [`docs/superpowers/`](docs/superpowers/) — dated plans/specs: useful history and evidence, not automatically current architecture.
- [`docs/historical/`](docs/historical/) — explicit fossils.

## Roadmap in one sentence

Deepen one persistent robot before building a generic robotics platform: companion/admin surfaces, explainable action traces, cleaner agency, lease-complete embodiment, explicit perception health/privacy, persistent spatial memory, lighter local cognition, simulation/fossil replay, and eventually autonomous docking.

See [`docs/ROADMAP.md`](docs/ROADMAP.md) for the issue-linked version.
