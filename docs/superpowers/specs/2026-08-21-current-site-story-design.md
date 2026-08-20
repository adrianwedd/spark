# Current SPARK Site Story Design

## Purpose

Rewrite SPARK's public story around the system that exists now: a persistent embodied agent that remains present between conversations. This is an editorial and information-architecture change inside the existing visual system.

## Narrative order

1. Lead with “A robot that stays around.”
2. Explain the difference from a stateless voice product in one short paragraph.
3. Establish three mechanisms: persistence, epistemic provenance, and deterministic authority.
4. Show the cognition path from continuous local perception through M5 and the resident Claude session to deterministic policy.
5. Use the current dashboard as live evidence of ongoing presence.
6. Retain the detailed provenance and learned-habit explanation below the broader architecture.

## Claim boundaries

- There is exactly one resident Claude session: `spark-brain`.
- Production Claude work is resident-only; `claude -p` and the cold-start fallback ladder are forbidden by code and CI.
- Voice turns and semantic scene description use the resident brain.
- Reflection, public chat, Obi chat, post QA, and blog QA use M5/Ollama without tools or filesystem access.
- M5 resident mode borrows a model already proven loaded by Ollama and defers rather than evicting the host workload.
- Continuous perception is local. Sonar, grayscale, Frigate, ambient state, and other deterministic inputs form awareness; Claude vision is sparse/on-demand semantic escalation.
- Durable claims retain typed provenance and confidence ceilings. Semantic intelligence proposes; deterministic code controls authority and execution.
- Per-service resource containment, derived health, and failure instrumentation make local failure visible and keep one service from consuming the host.

The public API does not currently expose brain validation, the loaded M5 model, cold-start counts, or M5-versus-Claude request totals. The site must not imply those are live readings and must not add a public endpoint solely to decorate the dashboard.

## Public surfaces

- Homepage: hero, short explainer, pillars, cognition path, live-dashboard framing, detailed architecture, provenance/habits, FAQ, and surfaced documentation links.
- Feed, thought, and blog landing pages: descriptions that place their content within SPARK's continuous life rather than using “inner life” as the whole proposition.
- Metadata: title, description, Open Graph, Twitter, and homepage schema.
- Worker: preserve dynamic post metadata; no architectural copy belongs there.

## Visual scope

Keep typography, palette, photography, navigation, feed cards, dashboard bands, and responsive breakpoints. Add only small content-supporting layouts for the four-line hero mechanism, three pillars, and cognition flow. Desktop and mobile must remain free of horizontal overflow.

## Verification

- Static story tests pin the required claims and reject the known stale phrases.
- Existing site layout tests plus focused desktop/mobile browser inspection protect composition.
- Repository resident-only, M5, provenance, policy, vision, health, and containment tests provide claim evidence.
