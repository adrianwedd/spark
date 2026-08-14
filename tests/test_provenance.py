"""Tests for pxh.provenance — epistemic typing of durable claims (issue #170).

The invariant under test: a durable record must be able to answer "where did
this come from?", and SPARK's own generated prose must never be able to
present itself as something it perceived.
"""
import pytest

from pxh import provenance as prov


# --- vocabulary ------------------------------------------------------------

def test_kinds_cover_the_five_distinctions_plus_unknown():
    assert set(prov.KINDS) == {
        "observation", "report", "inference", "narrative", "verification",
        "unknown",
    }


def test_every_kind_has_a_ceiling_a_default_and_a_label():
    for kind in prov.KINDS:
        assert kind in prov.CONFIDENCE_CEILING
        assert kind in prov.DEFAULT_CONFIDENCE
        assert prov.KIND_LABELS[kind]
        assert prov.DEFAULT_CONFIDENCE[kind] <= prov.CONFIDENCE_CEILING[kind]


def test_generated_kinds_are_capped_below_perceived_kinds():
    """The ordering is the safety property, not the exact numbers."""
    assert prov.CONFIDENCE_CEILING["narrative"] < prov.CONFIDENCE_CEILING["report"]
    assert prov.CONFIDENCE_CEILING["inference"] < prov.CONFIDENCE_CEILING["report"]
    assert prov.CONFIDENCE_CEILING["report"] <= prov.CONFIDENCE_CEILING["observation"]
    assert prov.CONFIDENCE_CEILING["unknown"] < prov.CONFIDENCE_CEILING["narrative"]


# --- make_provenance -------------------------------------------------------

def test_make_provenance_records_kind_source_and_evidence():
    p = prov.make_provenance("observation", "vision:describe-scene",
                             evidence=["photo:2026-08-14T01:02:03Z"])
    assert p["kind"] == "observation"
    assert p["source"] == "vision:describe-scene"
    assert p["evidence"] == ["photo:2026-08-14T01:02:03Z"]
    assert p["recorded_at"]
    assert p["legacy"] is False


def test_make_provenance_defaults_confidence_per_kind():
    assert (prov.make_provenance("narrative", "consolidation")["confidence"]
            == prov.DEFAULT_CONFIDENCE["narrative"])


def test_speculative_reflection_cannot_claim_observation_confidence():
    """#170's central test: narrative is capped, however confident the writer."""
    p = prov.make_provenance("narrative", "consolidation", confidence=1.0)
    assert p["confidence"] == prov.CONFIDENCE_CEILING["narrative"]
    assert p["confidence"] < 1.0


def test_make_provenance_rejects_an_unknown_kind():
    """Strict on write: our own writers pass literals, so a typo is a bug."""
    with pytest.raises(ValueError):
        prov.make_provenance("hunch", "somewhere")


def test_make_provenance_requires_a_source():
    with pytest.raises(ValueError):
        prov.make_provenance("observation", "")


def test_make_provenance_clamps_negative_confidence():
    assert prov.make_provenance("report", "obi", confidence=-3.0)["confidence"] == 0.0


def test_make_provenance_falls_back_to_default_on_junk_confidence():
    p = prov.make_provenance("report", "obi", confidence="very")
    assert p["confidence"] == prov.DEFAULT_CONFIDENCE["report"]


def test_make_provenance_bounds_evidence_list():
    p = prov.make_provenance("narrative", "consolidation",
                             evidence=[f"thought:{i}" for i in range(50)])
    assert len(p["evidence"]) == prov.MAX_EVIDENCE_REFS


def test_make_provenance_drops_empty_evidence_entries():
    p = prov.make_provenance("narrative", "consolidation", evidence=["", None, "x"])
    assert p["evidence"] == ["x"]


# --- read_provenance: legacy records ---------------------------------------

def test_legacy_record_reads_as_unknown_provenance():
    p = prov.read_provenance({"ts": "2026-01-01T00:00:00Z", "note": "an old note"})
    assert p["kind"] == "unknown"
    assert p["legacy"] is True


def test_legacy_record_is_not_silently_given_high_confidence():
    p = prov.read_provenance({"ts": "2026-01-01T00:00:00Z", "text": "old memory",
                              "source": "consolidation"})
    assert p["confidence"] <= prov.CONFIDENCE_CEILING["unknown"]


def test_legacy_record_keeps_its_coarse_source_string():
    """`source: consolidation` still answers part of "where from" — honestly,
    without upgrading the record's epistemic kind."""
    p = prov.read_provenance({"ts": "2026-01-01T00:00:00Z", "text": "old",
                              "source": "consolidation"})
    assert "consolidation" in p["source"]
    assert p["kind"] == "unknown"


def test_legacy_record_with_no_source_at_all_is_still_readable():
    p = prov.read_provenance({"note": "bare"})
    assert p["kind"] == "unknown"
    assert p["source"]


# --- read_provenance: stamped records --------------------------------------

def test_stamped_record_round_trips_through_read():
    rec = prov.stamp({"text": "Obi told me he likes lego"}, "report", "voice:obi")
    p = prov.read_provenance(rec)
    assert p["kind"] == "report"
    assert p["source"] == "voice:obi"
    assert p["legacy"] is False


def test_stamp_does_not_disturb_the_rest_of_the_record():
    rec = prov.stamp({"ts": "t", "text": "x", "tags": ["a"], "importance": 0.9},
                     "narrative", "consolidation")
    assert rec["ts"] == "t" and rec["text"] == "x"
    assert rec["tags"] == ["a"] and rec["importance"] == 0.9


# --- read_provenance: hostile / corrupt stored data ------------------------

def test_stored_kind_outside_the_vocabulary_reads_as_unknown():
    """Lenient on read: a bad stored value degrades, it does not raise."""
    p = prov.read_provenance({"provenance": {"kind": "certain", "source": "x"}})
    assert p["kind"] == "unknown"


def test_stored_confidence_above_the_kind_ceiling_is_clamped_on_read():
    """A record written (or hand-edited) claiming narrative at 0.99 must not
    be believed at 0.99 just because it is already on disk."""
    p = prov.read_provenance(
        {"provenance": {"kind": "narrative", "source": "consolidation",
                        "confidence": 0.99}})
    assert p["confidence"] == prov.CONFIDENCE_CEILING["narrative"]


def test_non_dict_provenance_value_reads_as_legacy():
    p = prov.read_provenance({"text": "x", "provenance": "narrative"})
    assert p["kind"] == "unknown"
    assert p["legacy"] is True


def test_read_provenance_never_raises_on_junk_input():
    for junk in (None, "a string", 42, [], {"provenance": {"kind": None}}):
        assert prov.read_provenance(junk)["kind"] in prov.KINDS


# --- describe: the "where did this come from?" answer ----------------------

def test_describe_distinguishes_the_five_kinds():
    seen = {prov.describe(prov.stamp({}, k, "src")) for k in prov.KINDS
            if k != "unknown"}
    assert len(seen) == 5, "each kind must read differently in a prompt"


def test_describe_marks_narrative_as_spark_s_own_unverified_prose():
    text = prov.describe(prov.stamp({}, "narrative", "consolidation"))
    assert "own" in text.lower()


def test_describe_of_a_legacy_record_says_the_source_is_unknown():
    assert "unknown" in prov.describe({"note": "old"}).lower()


def test_describe_never_raises_on_junk():
    assert prov.describe(None)


# --- supersession: correction without deletion -----------------------------

def test_stamped_records_get_a_stable_id():
    rec = prov.stamp({"ts": "2026-08-14T00:00:00Z", "text": "x"},
                     "narrative", "consolidation")
    assert rec["id"]
    assert prov.stamp({"ts": "2026-08-14T00:00:00Z", "text": "x"},
                      "narrative", "consolidation")["id"] != rec["id"]


def test_supersede_marks_the_old_record_without_removing_it():
    old = prov.stamp({"ts": "1", "text": "the hallway is empty"},
                     "inference", "mind:reflection")
    new = prov.stamp({"ts": "2", "text": "Obi was in the hallway"},
                     "observation", "vision:describe-scene")
    records = prov.apply_supersessions([old, prov.mark_supersedes(new, old)])
    assert len(records) == 2, "history is never deleted"
    assert records[0]["text"] == "the hallway is empty"
    assert prov.is_superseded(records[0]) is True
    assert prov.is_superseded(records[1]) is False


def test_superseded_record_reports_what_replaced_it():
    old = prov.stamp({"ts": "1", "text": "old belief"}, "inference", "mind")
    new = prov.mark_supersedes(
        prov.stamp({"ts": "2", "text": "new belief"}, "observation", "vision"), old)
    records = prov.apply_supersessions([old, new])
    assert records[0]["superseded_by"] == new["id"]


def test_supersession_downgrades_confidence_without_editing_the_claim():
    old = prov.stamp({"ts": "1", "text": "old belief"}, "report", "adrian")
    new = prov.mark_supersedes(prov.stamp({"ts": "2", "text": "new"}, "observation",
                                          "vision"), old)
    records = prov.apply_supersessions([old, new])
    assert records[0]["text"] == "old belief"
    assert (prov.read_provenance(records[0])["confidence"]
            < prov.read_provenance(old)["confidence"])


def test_apply_supersessions_ignores_dangling_references():
    orphan = prov.stamp({"ts": "2", "text": "new"}, "observation", "vision")
    orphan["provenance"]["supersedes"] = ["no-such-id"]
    records = prov.apply_supersessions([orphan])
    assert len(records) == 1
    assert prov.is_superseded(records[0]) is False


def test_apply_supersessions_tolerates_legacy_records():
    records = prov.apply_supersessions([{"ts": "1", "note": "legacy"}])
    assert records[0]["note"] == "legacy"
    assert prov.is_superseded(records[0]) is False
