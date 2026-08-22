# SPARK Roadmap

SPARK is a persistent embodied agent: one resident brain, local perception and M5 cognition, durable epistemic memory, and deterministic authority around anything that can affect the world.

This roadmap is deliberately about making **this robot** deeper, safer, more legible, and more autonomous. It is not a generic robotics wishlist. GitHub Issues are the canonical work ledger; issue numbers below are the durable links between this summary and implementation evidence.

## Now — deepen the robot we already have

### Companion and operator experience

- [ ] **Separate Obi companion UI from Adrian/admin UI** — child-safe relational interaction on one surface; health, provenance, authority, deployment and diagnostics on another. [#188](https://github.com/adrianwedd/spark/issues/188)
- [ ] **Explain “why did you do/say that?”** — trace a visible action back through awareness, memory/provenance, model proposal, policy verdict and deterministic execution. Build this as part of the admin/companion split rather than exposing chain-of-thought.
- [ ] **Make quiet state attributable and bounded** — every mute has a source, reason and lifetime; temporary transition quiet cannot become an unexplained permanent latch. [#209](https://github.com/adrianwedd/spark/issues/209)
- [ ] **Finish the audio-policy boundary** — every physical audio producer is either gated by the same policy or carries an explicit safety rationale for bypassing it. [#207](https://github.com/adrianwedd/spark/issues/207)
- [ ] **Fail closed on recovered/corrupt state** where an invented default could silently grant permission. [#208](https://github.com/adrianwedd/spark/issues/208)

### Agency without cosplay

- [ ] **Retire legacy pseudo-agency** — remove canned proximity greetings, mood-targeted prompt seeds, “free will” randomness and other theatrical shortcuts superseded by awareness → reflection → policy. [#187](https://github.com/adrianwedd/spark/issues/187)
- [ ] **Keep imagination, but label it honestly** — narrative and inference stay playful without becoming observation or autobiographical fact.
- [ ] **Let repeated grounded experience matter** — extend contextual preferences only where durable evidence supports “this worked here before”, with contradiction, decay and provenance intact.

### Body and reliability

- [ ] **Finish GPIO lease migration** — normal tools should coordinate ownership without terminating `px-alive`; daemon restarts should mean failures, not routine servo arbitration. [#193](https://github.com/adrianwedd/spark/issues/193)
- [ ] **Fix the photo-directory mixed-UID race and historical nesting** so camera capture cannot silently fail depending on which user created `photos/`. [#204](https://github.com/adrianwedd/spark/issues/204)
- [ ] **Make missing perception explicit** — Home Assistant/camera/other modalities report degraded health rather than quietly disappearing from awareness. [#191](https://github.com/adrianwedd/spark/issues/191)
- [ ] **Define perception, retention and disclosure boundaries** for microphone, camera, Frigate, Home Assistant, location/calendar and derived records. [#173](https://github.com/adrianwedd/spark/issues/173)

### Sustainability on the Pi

- [ ] **Reduce wake-listener memory without sacrificing wake accuracy** — current ASR stack remains the largest reducible resident service. [#219](https://github.com/adrianwedd/spark/issues/219)
- [ ] **Reduce swap-driven I/O pressure** — current containment localises failure, but rootfs and swap still compete on one storage device. [#247](https://github.com/adrianwedd/spark/issues/247)
- [ ] **Observe the brcmfmac starvation incident under the new containment regime** rather than declaring the mechanism solved. [#217](https://github.com/adrianwedd/spark/issues/217)
- [ ] **Retire migration-era supervisor lock compatibility** only after the proof gate in the issue passes on every runnable checkout. [#224](https://github.com/adrianwedd/spark/issues/224)

## Next — make persistence physical

- [ ] **Persistent spatial memory** — fuse local range/vision/motion evidence into a map that survives restarts and can answer “where am I / where was that?” without pretending uncertain geometry is fact.
- [ ] **Lightweight simulation / fossil replay** — deterministic CI scenarios for awareness → reflection → policy → action before reaching for a heavyweight Gazebo stack.
- [ ] **Predictive operational health** — learn normal battery, memory, latency and service behaviour from historical evidence and flag meaningful drift before a host-wide failure.
- [ ] **Teach-with-SPARK mode** — Obi learns Python by changing the robot and seeing the physical result; build on the current authority model rather than a browser shell. [#32](https://github.com/adrianwedd/spark/issues/32)
- [ ] **Physical play modes** — face-follow, obstacle courses and custom recorded sounds where they add real shared play, updated to current lease/policy architecture. [#31](https://github.com/adrianwedd/spark/issues/31), [#35](https://github.com/adrianwedd/spark/issues/35), [#26](https://github.com/adrianwedd/spark/issues/26)
- [ ] **Sleep/bedtime presence** — a deliberate bounded night mode that remains present without becoming noisy or coercive. [#34](https://github.com/adrianwedd/spark/issues/34)

## Later — autonomy with somewhere to go

- [ ] **Autonomous docking + energy awareness** — charging becomes part of SPARK's continuing life rather than an operator intervention.
- [ ] **Long-horizon room/landmark memory** — gradually learn traversable paths, recurring places and charging location with uncertainty and correction.
- [ ] **Bounded self-maintenance** — detect degraded sensors/services, explain the problem, perform deterministic safe recovery where authorised, and escalate when it cannot.
- [ ] **More cognition local** — use accelerated/quantised local models where they improve latency, memory pressure or cost without weakening route/trust boundaries.
- [ ] **Portable persistent self** — migrate memory, provenance, preferences and state across replacement hardware without confusing a model process or hardware image for identity.

## Deliberately not a priority

These may become useful if a concrete problem demands them, but they are not roadmap commitments today:

- generic reinforcement-learning “dream buffers” or fleet policy sharing;
- multi-car choreography demos;
- payload auto-detection without a real payload use case;
- a central map/log knowledge platform before one robot has useful persistent spatial memory;
- heavyweight simulation infrastructure when deterministic fossil replay can answer the same question more cheaply.

## Already landed in the current architecture

- [x] One persistent resident Claude brain; no production cold `claude -p` fallback.
- [x] Local M5/Ollama routing for reflection, public/Obi chat and publication QA; resident-mode model borrowing without evicting the operator workload.
- [x] Continuous local perception with sparse semantic vision escalation.
- [x] Durable typed provenance and confidence ceilings for observations, reports, model perception, inference, narrative and verification.
- [x] Deterministic policy/authority/tool boundaries around model proposals.
- [x] Per-service resource containment, PSI/cgroup instrumentation and functional health evidence.
- [x] Persistent-brain lifecycle fixes: restart-safe recycle chronology and stale-holder reaping.
- [x] Live public site framed around persistence rather than “ChatGPT on wheels”.

## Roadmap hygiene

Keep this file short and current. Implementation details belong in Issues, plans and specs. Historical strategy belongs under `docs/historical/` or dated `docs/superpowers/` material and must not silently become current policy.
