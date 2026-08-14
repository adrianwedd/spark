"""Behavioral tests for lived-experience adaptation — issue #172."""
import datetime as dt
import json
from pathlib import Path

import pytest

from pxh import contextual_preference as cp


NOW = dt.datetime(2026, 8, 14, 6, 0, tzinfo=dt.timezone.utc)
OPTIONS = ["quiet_science", "active_movement"]
FIXTURES = Path(__file__).parent / "fixtures"


def _experience(*, option="active_movement", outcome="positive", kind="report",
                index=0, person="obi", context="after_school", days_old=0,
                evidence=None, confidence=None):
    ts = NOW - dt.timedelta(days=days_old)
    refs = evidence if evidence is not None else [f"interaction:{kind}:{index}"]
    record = cp.make_experience(
        ts=ts.isoformat().replace("+00:00", "Z"),
        person=person,
        context=context,
        option=option,
        outcome=outcome,
        kind=kind,
        source=f"test:{kind}",
        evidence=refs,
        confidence=confidence,
    )
    record["id"] = f"experience-{index}"
    return record


def test_make_experience_requires_grounding_evidence():
    with pytest.raises(ValueError, match="evidence"):
        cp.make_experience(
            ts="2026-08-14T06:00:00Z",
            person="obi",
            context="after_school",
            option="quiet_science",
            outcome="positive",
            kind="report",
            source="voice:obi",
            evidence=[],
        )


@pytest.mark.parametrize(
    "kind", ["narrative", "inference", "unknown", "model_perception"]
)
def test_generated_or_interpreted_records_cannot_activate_preference(kind):
    records = [_experience(kind=kind, index=i) for i in range(3)]

    result = cp.choose_option(
        records,
        person="obi",
        context="after_school",
        options=OPTIONS,
        default="quiet_science",
        now=NOW,
    )

    assert result["chosen"] == "quiet_science"
    assert result["adapted"] is False


def test_three_consistent_reports_activate_and_raise_confidence():
    records = [_experience(index=i) for i in range(3)]

    result = cp.choose_option(
        records, person="obi", context="after_school", options=OPTIONS,
        default="quiet_science", now=NOW,
    )

    assert result["chosen"] == "active_movement"
    assert result["adapted"] is True
    assert result["scores"] == {
        "quiet_science": 0.0,
        "active_movement": 2.1,
    }
    assert result["confidence"] == pytest.approx(0.677419, abs=0.000001)


def test_duplicate_evidence_reference_counts_once():
    records = [
        _experience(index=i, evidence=["interaction:same-event"])
        for i in range(3)
    ]

    result = cp.choose_option(
        records, person="obi", context="after_school", options=OPTIONS,
        default="quiet_science", now=NOW,
    )

    assert result["chosen"] == "quiet_science"
    assert result["adapted"] is False
    assert result["scores"]["active_movement"] == 0.7
    assert result["explanation"]["ignored_by_reason"]["duplicate_evidence"] == 2


@pytest.mark.parametrize(
    ("person", "context"),
    [("adrian", "after_school"), ("obi", "weekend")],
)
def test_exact_person_and_context_scope_prevents_generalization(person, context):
    records = [_experience(index=i) for i in range(3)]

    result = cp.choose_option(
        records, person=person, context=context, options=OPTIONS,
        default="quiet_science", now=NOW,
    )

    assert result["chosen"] == "quiet_science"
    assert result["adapted"] is False
    assert not result["explanation"]["contributing"]


def test_negative_evidence_weakens_then_new_consistent_evidence_reverses():
    original = [_experience(index=i) for i in range(3)]
    contradiction = [
        _experience(index=10 + i, outcome="negative") for i in range(3)
    ]
    revised = [
        _experience(index=20 + i, option="quiet_science") for i in range(3)
    ]

    before = cp.choose_option(
        original, person="obi", context="after_school", options=OPTIONS,
        default="quiet_science", now=NOW,
    )
    weakened = cp.choose_option(
        original + contradiction, person="obi", context="after_school",
        options=OPTIONS, default="quiet_science", now=NOW,
    )
    after = cp.choose_option(
        original + contradiction + revised, person="obi", context="after_school",
        options=OPTIONS, default="active_movement", now=NOW,
    )

    assert before["chosen"] == "active_movement"
    assert weakened["adapted"] is False
    assert weakened["confidence"] < before["confidence"]
    assert after["chosen"] == "quiet_science"
    assert after["adapted"] is True
    assert len(after["explanation"]["contributing"]) == 9


def test_old_evidence_decays_below_activation_margin():
    recent = [_experience(index=i) for i in range(3)]
    old = [_experience(index=i, days_old=180) for i in range(3)]

    recent_result = cp.choose_option(
        recent, person="obi", context="after_school", options=OPTIONS,
        default="quiet_science", now=NOW,
    )
    old_result = cp.choose_option(
        old, person="obi", context="after_school", options=OPTIONS,
        default="quiet_science", now=NOW,
    )

    assert recent_result["adapted"] is True
    assert old_result["scores"]["active_movement"] == 0.525
    assert old_result["adapted"] is False


def test_explanation_cites_each_contributing_record_and_provenance():
    records = [_experience(index=i) for i in range(3)]

    result = cp.choose_option(
        records, person="obi", context="after_school", options=OPTIONS,
        default="quiet_science", now=NOW,
    )

    causes = result["explanation"]["contributing"]
    assert [cause["record_id"] for cause in causes] == [
        "experience-0", "experience-1", "experience-2",
    ]
    assert all(cause["kind"] == "report" for cause in causes)
    assert [cause["evidence"] for cause in causes] == [
        ["interaction:report:0"],
        ["interaction:report:1"],
        ["interaction:report:2"],
    ]
    assert all(cause["signed_weight"] == 0.7 for cause in causes)


def test_choice_rejects_unbounded_option_inputs():
    with pytest.raises(ValueError, match="unique"):
        cp.choose_option([], person="obi", context="after_school",
                         options=["same", "same"], default="same", now=NOW)
    with pytest.raises(ValueError, match="default"):
        cp.choose_option([], person="obi", context="after_school",
                         options=OPTIONS, default="not_offered", now=NOW)


def test_malformed_timestamp_is_inert_and_future_timestamp_has_age_zero():
    malformed = _experience(index=0)
    malformed["ts"] = "not-a-time"
    future = _experience(index=1)
    future["ts"] = "2026-08-15T06:00:00Z"
    other_future = _experience(index=2)
    other_future["ts"] = "2026-08-16T06:00:00Z"

    result = cp.choose_option(
        [malformed, future, other_future], person="obi", context="after_school",
        options=OPTIONS, default="quiet_science", now=NOW,
    )

    assert result["scores"]["active_movement"] == 1.4
    assert result["adapted"] is True
    assert result["explanation"]["ignored_by_reason"]["invalid_timestamp"] == 1


def test_superseded_evidence_uses_existing_provenance_discount():
    from pxh import provenance

    old = _experience(index=0)
    replacement = provenance.mark_supersedes(
        _experience(index=1), old,
    )

    result = cp.derive_preference(
        [old, replacement], person="obi", context="after_school",
        options=OPTIONS, now=NOW,
    )

    assert result["scores"] == {
        "quiet_science": 0.0,
        "active_movement": 0.875,
    }
    assert result["explanation"]["contributing"][0]["record_id"] == "experience-0"
    assert result["explanation"]["contributing"][0]["superseded_by"] == "experience-1"


@pytest.mark.parametrize(
    ("person", "context", "kind"),
    [
        ("adrian", "after_school", "report"),
        ("obi", "weekend", "report"),
        ("obi", "after_school", "narrative"),
    ],
)
def test_out_of_scope_or_ineligible_superseder_cannot_discount_evidence(
    person, context, kind,
):
    from pxh import provenance

    originals = [_experience(index=i, confidence=0.5) for i in range(2)]
    superseder = provenance.mark_supersedes(
        _experience(index=10, person=person, context=context, kind=kind),
        originals[0],
    )

    result = cp.choose_option(
        originals + [superseder], person="obi", context="after_school",
        options=OPTIONS, default="quiet_science", now=NOW,
    )

    assert result["chosen"] == "active_movement"
    assert result["scores"]["active_movement"] == 1.0


def test_superseder_for_another_option_cannot_discount_exact_option_evidence():
    from pxh import provenance

    originals = [_experience(index=i, confidence=0.5) for i in range(2)]
    superseder = provenance.mark_supersedes(
        _experience(index=10, option="quiet_science"), originals[0],
    )

    result = cp.derive_preference(
        originals + [superseder], person="obi", context="after_school",
        options=OPTIONS, now=NOW,
    )

    assert result["scores"]["active_movement"] == 1.0


def test_exact_activation_margin_does_not_activate():
    records = [_experience(index=i, confidence=0.375) for i in range(2)]
    result = cp.choose_option(
        records, person="obi", context="after_school", options=OPTIONS,
        default="quiet_science", now=NOW,
    )
    assert result["scores"]["active_movement"] == 0.75
    assert result["adapted"] is False
    assert result["chosen"] == "quiet_science"


def test_zero_confidence_record_does_not_satisfy_corroboration_minimum():
    records = [
        _experience(index=0, confidence=0.9),
        _experience(index=1, confidence=0.0),
    ]
    result = cp.choose_option(
        records, person="obi", context="after_school", options=OPTIONS,
        default="quiet_science", now=NOW,
    )
    assert result["scores"]["active_movement"] == 0.9
    assert result["adapted"] is False


@pytest.mark.parametrize("superseder_confidence", [0.0])
def test_zero_weight_superseder_cannot_discount_contributing_evidence(
    superseder_confidence,
):
    from pxh import provenance

    originals = [_experience(index=i, confidence=0.5) for i in range(2)]
    superseder = provenance.mark_supersedes(
        _experience(index=10, confidence=superseder_confidence), originals[0],
    )

    result = cp.choose_option(
        originals + [superseder], person="obi", context="after_school",
        options=OPTIONS, default="quiet_science", now=NOW,
    )

    assert result["scores"]["active_movement"] == 1.0
    assert result["chosen"] == "active_movement"


def test_duplicate_evidence_superseder_cannot_discount_original():
    from pxh import provenance

    originals = [_experience(index=i, confidence=0.5) for i in range(2)]
    superseder = provenance.mark_supersedes(
        _experience(index=10, evidence=["interaction:report:0"]), originals[0],
    )

    result = cp.choose_option(
        originals + [superseder], person="obi", context="after_school",
        options=OPTIONS, default="quiet_science", now=NOW,
    )

    assert result["scores"]["active_movement"] == 1.0
    assert result["chosen"] == "active_movement"


def test_append_and_load_preserve_record_and_provenance(tmp_path):
    path = tmp_path / "experiences.jsonl"
    record = _experience(index=1)

    cp.append_experience(record, path=path)
    loaded, diagnostics = cp.load_experiences(path=path)

    assert loaded == [record]
    assert diagnostics == {"total": 1, "valid": 1, "invalid": 0}
    assert loaded[0]["provenance"]["evidence"] == ["interaction:report:1"]


def test_load_keeps_valid_history_and_reports_malformed_lines(tmp_path):
    path = tmp_path / "experiences.jsonl"
    valid = _experience(index=1)
    path.write_text(
        json.dumps(valid) + "\n{not json}\n" + json.dumps({"person": "obi"}) + "\n",
        encoding="utf-8",
    )

    loaded, diagnostics = cp.load_experiences(path=path)

    assert loaded == [valid]
    assert diagnostics == {"total": 3, "valid": 1, "invalid": 2}


def test_load_reports_unicode_read_error_instead_of_looking_empty(tmp_path):
    path = tmp_path / "experiences.jsonl"
    path.write_bytes(b"\xff\xfe\x00")

    loaded, diagnostics = cp.load_experiences(path=path)

    assert loaded == []
    assert diagnostics == {
        "total": 0, "valid": 0, "invalid": 0, "read_error": "UnicodeDecodeError",
    }


def test_append_rejects_unstamped_or_invalid_record(tmp_path):
    path = tmp_path / "experiences.jsonl"
    with pytest.raises(ValueError, match="valid stamped"):
        cp.append_experience(
            {"person": "obi", "context": "after_school"}, path=path,
        )
    assert not path.exists()


def test_contradiction_appends_without_rewriting_prior_bytes(tmp_path):
    path = tmp_path / "experiences.jsonl"
    original = _experience(index=1)
    contradiction = _experience(index=2, outcome="negative")
    cp.append_experience(original, path=path)
    prior_bytes = path.read_bytes()

    cp.append_experience(contradiction, path=path)

    assert path.read_bytes().startswith(prior_bytes)
    loaded, diagnostics = cp.load_experiences(path=path)
    assert loaded == [original, contradiction]
    assert diagnostics["valid"] == 2


def test_experience_file_is_persona_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    assert cp.experience_file("spark") == tmp_path / "preference-experiences-spark.jsonl"
    assert cp.experience_file("vixen") == tmp_path / "preference-experiences-vixen.jsonl"


def _snapshot_result(result):
    return {
        "chosen": result["chosen"],
        "adapted": result["adapted"],
        "confidence": result["confidence"],
        "scores": result["scores"],
        "cited_record_ids": [
            cause["record_id"] for cause in result["explanation"]["contributing"]
        ],
    }


def test_longitudinal_snapshot_proves_history_only_deterministic_divergence():
    controls = {
        "clock": "2026-08-14T06:00:00Z",
        "model_version": "controlled-model@1",
        "sensor_snapshot": {
            "period": "after_school", "location": "indoors", "safe_to_move": True,
        },
        "randomness_seed": 172,
        "reflection_seed": "controlled-reflection-172",
        "code_version": "issue-172-baseline-v1",
        "config_version": "issue-172-baseline-v1",
    }
    history_a = [
        _experience(index=i, option="quiet_science") for i in range(3)
    ]
    history_b = [
        _experience(index=10 + i, option="active_movement") for i in range(3)
    ]
    common = {
        "person": "obi", "context": "after_school", "options": OPTIONS,
        "default": "quiet_science", "now": NOW, "controls": controls,
    }
    replay_a = {**common, "records": history_a}
    replay_b = {**common, "records": history_b}
    assert {k: v for k, v in replay_a.items() if k != "records"} == {
        k: v for k, v in replay_b.items() if k != "records"
    }

    baseline = cp.choose_option([], **common)
    result_a = cp.choose_option(**replay_a)
    result_b = cp.choose_option(**replay_b)
    repeated_b = cp.choose_option(**replay_b)

    assert baseline["chosen"] == "quiet_science" and not baseline["adapted"]
    assert result_a["chosen"] == "quiet_science" and result_a["adapted"]
    assert result_b["chosen"] == "active_movement" and result_b["adapted"]
    assert result_b == repeated_b
    assert baseline["controls"] == result_a["controls"] == result_b["controls"]

    observed = {
        "controls": controls,
        "baseline": _snapshot_result(baseline),
        "history_a": _snapshot_result(result_a),
        "history_b": _snapshot_result(result_b),
    }
    expected = json.loads(
        (FIXTURES / "lived_experience_baseline.json").read_text(encoding="utf-8")
    )
    assert observed == expected
