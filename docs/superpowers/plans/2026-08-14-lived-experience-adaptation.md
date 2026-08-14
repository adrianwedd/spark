# Lived-Experience Contextual Preference Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one deterministic, provenance-preserving contextual preference policy whose bounded choices measurably diverge solely because of lived history.

**Architecture:** Store append-only, system-stamped experience records and derive an exact person/context preference at choice time. Only existing high-authority provenance kinds influence the policy; scores decay with age, contradiction revises them, and every result carries record-level explanations plus frozen replay controls.

**Tech Stack:** Python 3, JSONL, `filelock`, existing `pxh.provenance`, pytest

**Spec:** `docs/superpowers/specs/2026-08-14-lived-experience-adaptation-design.md`

## Global Constraints

- Do not add a provenance kind or change any existing confidence ceiling.
- Scope every preference to an exact person, context, and caller-offered option.
- `narrative`, `inference`, `unknown`, and `model_perception` have zero policy weight.
- Require non-empty provenance evidence and deduplicate repeated evidence references.
- Use a 90-day half-life, two-positive-experience minimum, and `0.75` activation margin.
- Keep history append-only and expose every contributing record in explanations.
- Freeze clock, model/version, sensor snapshot, randomness, reflection seed, code, and config in longitudinal replay; history is the only differing input.

---

### Task 1: Contextual preference projection

**Files:**
- Create: `src/pxh/contextual_preference.py`
- Create: `tests/test_contextual_preference.py`

**Interfaces:**
- Consumes: `pxh.provenance.stamp()`, `read_provenance()`, and `apply_supersessions()`
- Produces: `make_experience(...) -> dict`, `derive_preference(records, *, person, context, options, now) -> dict`, and `choose_option(records, *, person, context, options, default, now, controls=None) -> dict`

- [ ] **Step 1: Write failing schema and epistemic-boundary tests**

```python
def test_make_experience_requires_grounding_evidence():
    with pytest.raises(ValueError, match="evidence"):
        cp.make_experience(ts=NOW, person="obi", context="after_school",
                           option="quiet_science", outcome="positive",
                           kind="report", source="voice:obi", evidence=[])

@pytest.mark.parametrize("kind", ["narrative", "inference", "unknown", "model_perception"])
def test_generated_or_interpreted_records_cannot_activate_preference(kind):
    records = [_experience(option="active_movement", kind=kind, index=i)
               for i in range(3)]
    result = cp.choose_option(records, person="obi", context="after_school",
                              options=OPTIONS, default="quiet_science", now=NOW)
    assert result["chosen"] == "quiet_science"
    assert result["adapted"] is False
```

- [ ] **Step 2: Run schema tests and verify RED**

Run: `PX_BYPASS_SUDO=1 LOG_DIR=logs_test .venv/bin/python -m pytest tests/test_contextual_preference.py -v`

Expected: collection fails because `pxh.contextual_preference` does not exist.

- [ ] **Step 3: Implement strict records and deterministic scoring**

Implement bounded string validation, provenance stamping, timestamp parsing, evidence-reference deduplication, exact scope filtering, eligibility filtering, signed confidence with 90-day decay, the two-positive minimum, the `0.75` margin, stable per-option scores, and record-level explanation entries. `choose_option` must reject an empty/duplicate option list or a default not in options and may return only one of the supplied options.

- [ ] **Step 4: Add focused behavior tests one at a time, observing RED before each implementation increment**

```python
def test_three_consistent_reports_activate_and_raise_confidence(): ...
def test_duplicate_evidence_reference_counts_once(): ...
def test_exact_person_and_context_scope_prevents_generalization(): ...
def test_negative_evidence_weakens_then_reverses_without_deleting_history(): ...
def test_old_evidence_decays_below_recent_evidence(): ...
def test_explanation_cites_each_contributing_record_and_provenance(): ...
def test_choice_is_bounded_to_caller_options_and_default(): ...
def test_malformed_and_future_records_fail_closed(): ...
def test_superseded_records_use_existing_provenance_discount(): ...
```

Literal expected scores and decisions must be hand-derived; tests may use record builders only for setup, never to calculate expectations.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run: `PX_BYPASS_SUDO=1 LOG_DIR=logs_test .venv/bin/python -m pytest tests/test_contextual_preference.py -v`

Expected: all focused tests pass with no warnings.

- [ ] **Step 6: Commit the projection**

```bash
git add src/pxh/contextual_preference.py tests/test_contextual_preference.py
git commit -m "feat: derive scoped preferences from lived evidence (#172)"
```

### Task 2: Append-only persistence

**Files:**
- Modify: `src/pxh/contextual_preference.py`
- Modify: `tests/test_contextual_preference.py`

**Interfaces:**
- Consumes: validated records from `make_experience()`
- Produces: `experience_file(persona="spark") -> Path`, `append_experience(record, *, path=None) -> None`, and `load_experiences(*, path=None, persona="spark") -> tuple[list[dict], dict]`

- [ ] **Step 1: Write failing round-trip and corruption tests**

```python
def test_append_and_load_preserve_record_and_provenance(tmp_path): ...
def test_load_keeps_valid_history_and_reports_malformed_lines(tmp_path): ...
def test_append_rejects_unstamped_or_invalid_record(tmp_path): ...
def test_contradiction_appends_without_rewriting_prior_bytes(tmp_path): ...
```

- [ ] **Step 2: Run persistence tests and verify RED**

Run: `PX_BYPASS_SUDO=1 LOG_DIR=logs_test .venv/bin/python -m pytest tests/test_contextual_preference.py -k 'append or load or contradiction_appends' -v`

Expected: failures name the missing persistence functions.

- [ ] **Step 3: Implement locked append and tolerant load**

Use `FileLock(str(path) + ".lock")`, create only the parent directory, append one compact JSON object plus newline, validate before writing, and return diagnostics containing total/valid/invalid counts. Loading must not rewrite or delete any line.

- [ ] **Step 4: Run the focused file and verify GREEN**

Run: `PX_BYPASS_SUDO=1 LOG_DIR=logs_test .venv/bin/python -m pytest tests/test_contextual_preference.py -v`

Expected: all tests pass.

- [ ] **Step 5: Commit persistence**

```bash
git add src/pxh/contextual_preference.py tests/test_contextual_preference.py
git commit -m "feat: persist preference experiences append-only (#172)"
```

### Task 3: Controlled longitudinal snapshot

**Files:**
- Modify: `tests/test_contextual_preference.py`
- Create: `tests/fixtures/lived_experience_baseline.json`

**Interfaces:**
- Consumes: `choose_option()` and literal experience histories
- Produces: checked-in baseline, A-history, and B-history result snapshot under identical replay controls

- [ ] **Step 1: Write the failing longitudinal test with frozen controls**

Build literal A and B histories with stable record IDs and provenance timestamps. Assert the only unequal input is `history`, baseline selects `quiet_science` without adaptation, A selects `quiet_science` with adaptation, B selects `active_movement` with adaptation, a second run is identical, and the normalized output exactly equals the checked-in JSON fixture.

- [ ] **Step 2: Run and verify RED**

Run: `PX_BYPASS_SUDO=1 LOG_DIR=logs_test .venv/bin/python -m pytest tests/test_contextual_preference.py -k longitudinal -v`

Expected: failure because the declared snapshot does not yet exist or output lacks replay controls.

- [ ] **Step 3: Add controls passthrough and the hand-reviewed snapshot**

The snapshot must include `clock`, `model_version`, `sensor_snapshot`, `randomness_seed`, `reflection_seed`, `code_version`, and `config_version`, plus baseline/A/B choices, confidence, scores, and cited IDs. Do not generate expectations with production scoring helpers.

- [ ] **Step 4: Run twice and verify GREEN/reproducibility**

Run twice: `PX_BYPASS_SUDO=1 LOG_DIR=logs_test .venv/bin/python -m pytest tests/test_contextual_preference.py -k longitudinal -v`

Expected: both runs pass identically.

- [ ] **Step 5: Commit the regression snapshot**

```bash
git add tests/test_contextual_preference.py tests/fixtures/lived_experience_baseline.json src/pxh/contextual_preference.py
git commit -m "test: prove history-only behavioral divergence (#172)"
```

### Task 4: Operator contract and full verification

**Files:**
- Modify: `docs/TOOLS.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: the implemented record, derivation, persistence, and replay APIs
- Produces: concise operational guidance defining what counts—and does not count—as development

- [ ] **Step 1: Document usage and epistemic limits**

Document exact scoping, eligible provenance kinds, decay/contradiction behavior, append-only reversibility, explanation fields, state filename, and the controlled longitudinal command. State explicitly that recall, randomness, prompt/config/model changes, narrative, and uncorroborated model perception are not development evidence.

- [ ] **Step 2: Run focused and provenance regression tests**

Run: `PX_BYPASS_SUDO=1 LOG_DIR=logs_test .venv/bin/python -m pytest tests/test_contextual_preference.py tests/test_provenance.py -v`

Expected: all pass without warnings.

- [ ] **Step 3: Run full non-live suite**

Run: `PX_BYPASS_SUDO=1 LOG_DIR=logs_test .venv/bin/python -m pytest -m 'not live'`

Expected: all tests pass.

- [ ] **Step 4: Check repository hygiene**

Run: `git diff --check && git status --short && git log --oneline origin/master..HEAD`

Expected: no whitespace errors; only intended files are changed/committed.

- [ ] **Step 5: Commit documentation**

```bash
git add docs/TOOLS.md README.md
git commit -m "docs: explain measurable lived development (#172)"
```

