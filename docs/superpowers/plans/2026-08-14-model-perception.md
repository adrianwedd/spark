# Model-Mediated Perception Provenance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make newly persisted vision-derived exploration notes carry bounded, evidence-linked `model_perception` provenance without reclassifying existing records.

**Architecture:** Extend the shared provenance boundary with a modality-independent kind whose evidence invariant is strict on write and fail-safe on read. Change `px-wander` so it durably writes a stable exploration observation before promoting the model interpretation, then make recall and operator documentation preserve the distinction between grounded interpretation and verified fact.

**Tech Stack:** Python 3.14, Bash-embedded Python tools, JSONL state, `filelock`, pytest.

**Spec:** `docs/superpowers/specs/2026-08-14-model-perception-design.md`

## Global Constraints

- `model_perception` defaults to `0.65` and is capped at `0.75` on write and read.
- Every `model_perception` requires at least one non-empty evidence reference.
- Evidence identifies grounding provenance; it does not prove semantic correctness or elevate confidence beyond the `model_perception` ceiling.
- Persist the exploration event before the durable claim; any capture, interpretation, event-write, or evidence-construction failure prevents promotion.
- The model cannot choose provenance kind, confidence, or evidence.
- Existing records are never retroactively reinterpreted; old exploration entries without `observation_id` remain readable.
- `exploration.jsonl` retains event `type: "observation"`.
- Raw JPEG retention remains optional.
- #178 is already merged and precedes this branch; #172 remains outside this issue.

---

### Task 1: Add the `model_perception` provenance contract

**Files:**
- Modify: `src/pxh/provenance.py`
- Test: `tests/test_provenance.py`

**Interfaces:**
- Consumes: existing `make_provenance(kind, source, evidence=None, confidence=None, recorded_at=None)`, `read_provenance(record)`, and `describe(record)` APIs.
- Produces: accepted kind `model_perception`, default `0.65`, ceiling `0.75`, label `a model interpreted this from my sensors`, strict evidence validation on write, and safe degradation to `unknown` on read.

- [x] **Step 1: Write failing ontology and write-invariant tests**

  Add literal assertions that `model_perception` is in `KINDS`, its default and cap are exactly `0.65` and `0.75`, the full ceiling ordering is `unknown < narrative < inference < model_perception < report < observation == verification`, confidence `1.0` clamps to `0.75`, and `make_provenance("model_perception", "claude_vision:exploration")` raises `ValueError` for omitted, empty, or whitespace-only evidence.

- [x] **Step 2: Run the focused tests and verify RED**

  Run: `source /Users/adrian/repos/spark/.venv/bin/activate && python -m pytest tests/test_provenance.py -q`

  Expected: failures because the kind/constants and mandatory evidence rule do not exist.

- [x] **Step 3: Implement the minimal strict write contract**

  Add the kind to `KINDS`, add the approved confidence values and prompt label, update the module inventory so direct/deterministic sensor claims remain `observation`, and in `make_provenance()` clean evidence once then raise `ValueError("model_perception provenance requires evidence")` when the cleaned list is empty.

- [x] **Step 4: Write failing lenient-read tests**

  Cover missing evidence, non-sequence evidence, and entries that clean to empty. Assert each stored `model_perception` reads as `unknown`, uses the unknown ceiling/default, and does not raise. Also assert valid grounded records remain `model_perception` at no more than `0.75`; this is the guard that evidence is grounding, not verification.

- [x] **Step 5: Run the focused tests and verify RED**

  Run the same focused command; expect only the new read-side cases to fail.

- [x] **Step 6: Implement fail-safe read degradation and verify GREEN**

  In `read_provenance()`, clean evidence before clamping; if the stored kind is `model_perception` and cleaned evidence is empty, resolve it as `unknown`. Return the cleaned evidence and confidence for the resolved kind. Run `python -m pytest tests/test_provenance.py -q` and expect all tests to pass.

- [x] **Step 7: Commit the independently green provenance boundary**

  Run: `git add src/pxh/provenance.py tests/test_provenance.py && git commit -m "feat(provenance): type grounded model perception (#177)"`

### Task 2: Enforce evidence-before-claim ordering in exploration

**Files:**
- Modify: `bin/px-wander`
- Test: `tests/test_exploration.py`

**Interfaces:**
- Consumes: `pxh.provenance.stamp(record, "model_perception", "claude_vision:exploration", evidence=[ref])`.
- Produces: `_write_observation(entry: dict) -> bool`; `_auto_remember(text: str, evidence_ref: str) -> bool`; stable references formatted `exploration:<explore_id>:observation:<observation_id>`.

- [x] **Step 1: Write failing helper tests for stable evidence and refusal paths**

  Test that `_write_observation()` returns `True` after an appended event containing its caller-supplied `observation_id`, returns `False` when `Path.open` raises, and that `_auto_remember(text, evidence_ref)` writes a stable note ID plus `model_perception` provenance with source `claude_vision:exploration`, confidence `0.65`, and exactly the supplied reference. Assert empty evidence returns `False` and writes no file.

- [x] **Step 2: Run the helper tests and verify RED**

  Run: `source /Users/adrian/repos/spark/.venv/bin/activate && python -m pytest tests/test_exploration.py -q`

  Expected: signature/return/provenance assertions fail against the legacy helpers.

- [x] **Step 3: Implement minimal helper contracts**

  Import `uuid` and `pxh.provenance`; return `True` only after the observation append completes and `False` on exception. Require a non-empty evidence reference in `_auto_remember`, stamp the note using only system-constructed constants, append it, return `True`, and return `False` on validation/write failure.

- [x] **Step 4: Run helper tests and verify GREEN**

  Run the focused exploration suite and expect all tests to pass.

- [x] **Step 5: Write a failing orchestration test for mandatory ordering**

  Drive one dry exploration iteration with controlled sonar, scene description, and time dependencies. Record side effects and assert the observation append occurs before note append. Parameterize event persistence success/failure and assert failure produces no durable note while exploration remains safe. Use an adversarial description containing provenance-like JSON text and assert it remains only note text—it cannot override kind, confidence, or evidence.

- [x] **Step 6: Run the orchestration test and verify RED**

  Run: `python -m pytest tests/test_exploration.py::test_interesting_vision_persists_evidence_before_note tests/test_exploration.py::test_failed_observation_write_prevents_note_promotion -q`

  Expected: failure because the current caller promotes regardless of event-write outcome and has no observation ID.

- [x] **Step 7: Implement evidence-first orchestration**

  For every vision observation create `observation_id = uuid.uuid4().hex[:16]`, include it in `obs_entry`, call `_write_observation(obs_entry)`, append to the in-memory operational list, and only for interesting non-fallback interpretations whose event write succeeded construct `exploration:<explore_id>:observation:<observation_id>` and call `_auto_remember`. Do not promote failed vision or failed event persistence.

- [x] **Step 8: Verify exploration GREEN and commit**

  Run: `python -m pytest tests/test_exploration.py -q`

  Then: `git add bin/px-wander tests/test_exploration.py && git commit -m "feat(exploration): ground vision notes in durable events (#177)"`

### Task 3: Render model interpretation honestly in recall

**Files:**
- Modify: `bin/tool-recall`
- Test: `tests/test_tools.py`

**Interfaces:**
- Consumes: `provenance.read_provenance(record)["kind"]`.
- Produces: single lead `I remember my vision system interpreting` and mixed-item lead `my vision system interpreted` for trusted source `claude_vision:*`; generic perception-system wording for other sources.

- [x] **Step 1: Write failing single and mixed recall tests**

  Extend `_seed_note` to accept evidence and source. Seed valid `model_perception` records and assert the exact interpretation lead-ins appear, while `seeing`/`saw` do not. Add a non-vision source case asserting generic `perception system` wording. Keep existing wording assertions for every other kind.

- [x] **Step 2: Run focused recall tests and verify RED**

  Run: `source /Users/adrian/repos/spark/.venv/bin/activate && python -m pytest tests/test_tools.py -k recall -q`

  Expected: new model-perception cases fail because no lead-in exists.

- [x] **Step 3: Implement source-aware interpretation wording**

  Add a small trusted-code helper that chooses vision versus generic perception wording from the stored provenance source. Use it only when the resolved kind is `model_perception`; preserve all existing dictionary wording for other kinds.

- [x] **Step 4: Verify recall GREEN and commit**

  Run the focused recall tests, then `git add bin/tool-recall tests/test_tools.py && git commit -m "feat(recall): qualify model-mediated perception (#177)"`.

### Task 4: Document the operator contract and verify the feature

**Files:**
- Modify: `README.md`
- Modify: `docs/TOOLS.md`
- Modify: `docs/SCRIPTS.md`
- Modify: `docs/superpowers/plans/2026-08-14-model-perception.md`

**Interfaces:**
- Consumes: implemented provenance and exploration behavior.
- Produces: operator-facing explanation that evidence traces the producing perception event but does not verify the model's semantics.

- [x] **Step 1: Update documentation**

  Document the direct-observation/model-interpretation boundary, evidence-before-claim ordering, optional JPEG retention, confidence cap, recall language, and the explicit rule: an exploration evidence reference proves which perception event produced a claim, not that the description is semantically correct. Note future durable Frigate-derived claims must use `model_perception`.

- [x] **Step 2: Run focused regression suites**

  Run: `source /Users/adrian/repos/spark/.venv/bin/activate && PX_BYPASS_SUDO=1 LOG_DIR=logs_test python -m pytest tests/test_provenance.py tests/test_memory.py tests/test_tools.py tests/test_exploration.py tests/test_mind.py tests/test_mind_utils.py tests/test_voice_loop.py -q`

  Expected: pass with only established skips.

- [x] **Step 3: Run the repository-wide verifier**

  Run: `source /Users/adrian/repos/spark/.venv/bin/activate && PX_BYPASS_SUDO=1 LOG_DIR=logs_test python -m pytest`

  Record the outcome distinctly as pass, assertion failure, timeout, or environment/setup failure per #178.

- [x] **Step 4: Review the diff against the spec and mutation-check tests**

  Confirm a mutation of any ceiling, evidence validation, write ordering, promotion guard, provenance source, or recall branch fails at least one test. Confirm no historical migration, raw-image retention, Frigate promotion, or #172 behavior entered the diff.

- [x] **Step 5: Commit documentation and plan completion**

  Run: `git add README.md docs/TOOLS.md docs/SCRIPTS.md docs/superpowers/plans/2026-08-14-model-perception.md && git commit -m "docs(epistemics): explain grounded interpretation (#177)"`

- [x] **Step 6: Perform final branch review and integration readiness check**

  Compare `git diff origin/master...HEAD`, confirm the worktree is clean, confirm #178 remains an ancestor with `git merge-base --is-ancestor f3c230b6 HEAD`, and prepare #177 for clean integration without merging or implementing #172.
