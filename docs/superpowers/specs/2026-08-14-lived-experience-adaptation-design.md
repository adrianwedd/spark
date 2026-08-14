# Lived-Experience Contextual Preference Design

**Issue:** #172

## Goal

Make two SPARK instances with identical code, configuration, clock, model/version,
sensor snapshot, randomness, and reflection seed make one predictable, bounded
choice differently because their lived histories differ, while retaining a complete
explanation of the evidence that caused the divergence.

## Scope

This change adds one mechanism only: learned preference between explicitly offered
options for one named person in one named context. It is not a personality model,
prompt mutation system, memory-recall feature, or general policy learner. A
preference learned for `obi` in `after_school` cannot affect another person, context,
or an option not explicitly offered by the caller.

## Chosen approach

SPARK stores append-only contextual experience records and derives a preference
view from them at decision time. Each record contains a stable ID, timestamp,
person, context, option, outcome (`positive` or `negative`), and a provenance block
created and read through `pxh.provenance`. The derived view is never persisted as a
new source of truth. Contradictory evidence therefore revises the current view while
all earlier evidence remains available.

This uses the existing provenance ontology unchanged. It does not add an epistemic
kind or raise any kind's confidence ceiling. The preference is a deterministic
projection over evidence, not a new claim that can recursively support itself.

### Rejected alternatives

1. A mutable per-person preference profile would be simple to read, but updating a
   scalar would obscure which experiences moved it and make reversibility depend on
   extra audit machinery.
2. An LLM-written relationship summary would be flexible, but model prose could
   become behavioral authority and controlled replay would depend on sampling.
3. Prompt-only retrieval of relevant memories would be recall, not a measurable
   adaptation policy, and could not isolate history from model randomness.

## Experience record contract

Records live in `state/preference-experiences-<persona>.jsonl`. The implementation
accepts an explicit path as well, so tests and replay tools never touch live state.
The writer validates and bounds every string and requires a non-empty evidence
reference. System code, not model output, supplies provenance kind, source,
confidence, and evidence. Corrupt or legacy lines remain readable at the store
boundary but cannot influence a preference unless they satisfy the full record and
provenance contract.

Eligible behavioral evidence kinds are `observation`, `report`, and
`verification`. `narrative`, `inference`, `unknown`, and `model_perception` carry
zero policy weight. This deliberately stricter rule means narrative or interpreted
sensor prose cannot create a high-confidence preference alone. A future mechanism
may define kind-appropriate corroboration for model perception, but #172 does not.

Repeated records sharing the same provenance evidence reference count once. This
prevents a duplicated event from masquerading as repeated lived experience. Each
contributing record remains independently addressable by its stable record ID.

## Derivation and choice

For an exact `(person, context, option)`, each eligible record contributes:

`provenance confidence × 0.5 ** (age_days / 90) × outcome sign`

where positive is `+1`, negative is `-1`, future timestamps are treated as age zero,
and malformed timestamps contribute nothing. This fixed 90-day half-life makes old
evidence weaken without deleting it.

For offered options, the option with the largest net score is preferred only when:

- it has at least two independent positive contributing experiences;
- its net score exceeds the runner-up by a fixed activation margin of `0.75`; and
- there is no exact score tie.

Otherwise the caller's declared default is returned. Preference confidence is
`margin / (margin + 1)`, bounded below `1.0`. Repeated consistent evidence increases
the margin and confidence; negative or competing evidence reduces it; age reduces
all old contributions. Choice ordering and explanation ordering are stable.

The result contains the chosen option, whether adaptation activated, confidence,
the default, exact person/context, per-option scores, and an explanation listing
every contributing record ID with kind, source, evidence references, age, signed
weight, option, and outcome. Ignored records are summarized by reason without
letting their text influence the choice.

## Controlled replay and baseline snapshots

The replay input explicitly freezes:

- UTC clock: `2026-08-14T06:00:00Z`
- model/version: `controlled-model@1`
- sensor snapshot: `after_school, indoors, safe_to_move`
- randomness seed: `172`
- reflection seed: `controlled-reflection-172`
- code/config: represented by a shared baseline version string

These controls are copied into the result for auditability but do not affect
preference scoring. They must be byte-identical between comparison instances.

The predeclared longitudinal divergence is:

- baseline/no history chooses the declared `quiet_science` default;
- history A has three independent positive Obi/after-school reports for
  `quiet_science` and chooses `quiet_science` with adaptation active;
- history B has three independent positive Obi/after-school reports for
  `active_movement` and chooses `active_movement` with adaptation active;
- B's choice cites exactly B's three record IDs;
- B's history does not affect Adrian or a `weekend` context;
- later contradictory lived evidence reduces and can reverse B's preference while
  retaining every original record.

A checked-in JSON snapshot records these expected outputs. Tests reconstruct both
histories from literal fixtures, compare the controlled results to the snapshot,
and rerun to prove deterministic reproduction.

## Persistence, errors, and safety

JSONL appends use the repository's file-lock pattern. A malformed write raises a
validation error; a malformed stored line is ignored and reported in diagnostics.
No choice can trigger motion or another side effect: this module selects only from
the caller-supplied bounded option list. The caller remains responsible for its
existing safety gates before executing an option.

No historical record is deleted, superseded, or rewritten by preference derivation.
Provenance supersession remains honored: records discounted by the existing
provenance system carry the discounted confidence in the derived view. A
superseder can affect the view only when it is itself eligible evidence for the
same exact person, context, and option; an out-of-scope, cross-option, or
ineligible record cannot indirectly change a preference.

## Files and verification

- Add `src/pxh/contextual_preference.py` for validated persistence, deterministic
  derivation, bounded choice, and explanations.
- Add `tests/test_contextual_preference.py` for TDD unit, safety, epistemic,
  scoping, decay, contradiction, persistence, and replay tests.
- Add `tests/fixtures/lived_experience_baseline.json` for the predeclared snapshot.
- Update operator/developer documentation with the record and replay contract.
- Run the focused test file first, then the complete pytest suite with the mandated
  non-privileged environment.
