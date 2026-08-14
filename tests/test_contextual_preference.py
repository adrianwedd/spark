"""Behavioral tests for lived-experience adaptation — issue #172."""
import datetime as dt

import pytest

from pxh import contextual_preference as cp


NOW = dt.datetime(2026, 8, 14, 6, 0, tzinfo=dt.timezone.utc)
OPTIONS = ["quiet_science", "active_movement"]


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
        _experience(index=1, option="quiet_science"), old,
    )

    result = cp.derive_preference(
        [old, replacement], person="obi", context="after_school",
        options=OPTIONS, now=NOW,
    )

    assert result["scores"] == {
        "quiet_science": 0.7,
        "active_movement": 0.175,
    }
    assert result["explanation"]["contributing"][0]["record_id"] == "experience-0"
    assert result["explanation"]["contributing"][0]["superseded_by"] == "experience-1"
