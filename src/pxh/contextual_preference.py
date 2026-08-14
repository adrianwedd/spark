"""Narrow, provenance-backed adaptation from lived contextual experience."""
from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence

from pxh import provenance


ELIGIBLE_KINDS = frozenset({"observation", "report", "verification"})
OUTCOMES = frozenset({"positive", "negative"})
MAX_FIELD_LENGTH = 100
HALF_LIFE_DAYS = 90.0
ACTIVATION_MARGIN = 0.75
MIN_POSITIVE_EXPERIENCES = 2


def _field(name: str, value: object) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} must be non-empty")
    if len(text) > MAX_FIELD_LENGTH:
        raise ValueError(f"{name} exceeds {MAX_FIELD_LENGTH} characters")
    return text


def _timestamp(value: object) -> dt.datetime:
    text = _field("ts", value)
    parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("ts must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def make_experience(*, ts: str, person: str, context: str, option: str,
                    outcome: str, kind: str, source: str,
                    evidence: Iterable[str] | None,
                    confidence: object = None) -> dict:
    """Create one system-attributed contextual experience record."""
    refs = [str(ref).strip() for ref in (evidence or []) if str(ref).strip()]
    if not refs:
        raise ValueError("experience requires provenance evidence")
    outcome_value = _field("outcome", outcome)
    if outcome_value not in OUTCOMES:
        raise ValueError(f"outcome must be one of {sorted(OUTCOMES)}")
    record = {
        "ts": _timestamp(ts).isoformat().replace("+00:00", "Z"),
        "person": _field("person", person),
        "context": _field("context", context),
        "option": _field("option", option),
        "outcome": outcome_value,
    }
    return provenance.stamp(
        record, kind, _field("source", source), evidence=refs,
        confidence=confidence,
    )


def _ignore(counts: dict[str, int], reason: str) -> None:
    counts[reason] = counts.get(reason, 0) + 1


def derive_preference(records: Sequence[dict], *, person: str, context: str,
                      options: Sequence[str], now: dt.datetime) -> dict:
    """Project append-only evidence into one exact-scope preference view."""
    offered = list(options)
    if not offered or len(set(offered)) != len(offered):
        raise ValueError("options must be non-empty and unique")
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")
    current = now.astimezone(dt.timezone.utc)
    scores = {option: 0.0 for option in offered}
    positives = {option: 0 for option in offered}
    contributing: list[dict] = []
    ignored: dict[str, int] = {}
    seen_evidence: set[str] = set()

    for record in provenance.apply_supersessions(list(records)):
        if (record.get("person") != person or record.get("context") != context
                or record.get("option") not in scores):
            _ignore(ignored, "out_of_scope")
            continue
        outcome = record.get("outcome")
        if outcome not in OUTCOMES or not record.get("id"):
            _ignore(ignored, "invalid_record")
            continue
        block = provenance.read_provenance(record)
        if block["kind"] not in ELIGIBLE_KINDS:
            _ignore(ignored, "ineligible_provenance")
            continue
        refs = list(block["evidence"])
        if not refs:
            _ignore(ignored, "missing_evidence")
            continue
        if any(ref in seen_evidence for ref in refs):
            _ignore(ignored, "duplicate_evidence")
            continue
        try:
            observed_at = _timestamp(record.get("ts"))
        except (TypeError, ValueError):
            _ignore(ignored, "invalid_timestamp")
            continue
        seen_evidence.update(refs)
        age_days = max(0.0, (current - observed_at).total_seconds() / 86400.0)
        decay = 0.5 ** (age_days / HALF_LIFE_DAYS)
        sign = 1.0 if outcome == "positive" else -1.0
        weight = block["confidence"] * decay * sign
        option = record["option"]
        scores[option] += weight
        if outcome == "positive":
            positives[option] += 1
        cause = {
            "record_id": str(record["id"]),
            "option": option,
            "outcome": outcome,
            "kind": block["kind"],
            "source": block["source"],
            "evidence": refs,
            "age_days": round(age_days, 6),
            "signed_weight": round(weight, 6),
        }
        if record.get("superseded_by"):
            cause["superseded_by"] = record["superseded_by"]
        contributing.append(cause)

    rounded_scores = {option: round(score, 6) for option, score in scores.items()}
    ranked = sorted(enumerate(offered), key=lambda item: (-scores[item[1]], item[0]))
    winner = ranked[0][1]
    runner_score = scores[ranked[1][1]] if len(ranked) > 1 else 0.0
    margin = max(0.0, scores[winner] - runner_score)
    activated = (
        positives[winner] >= MIN_POSITIVE_EXPERIENCES
        and margin >= ACTIVATION_MARGIN
        and (len(ranked) == 1 or scores[winner] != runner_score)
    )
    confidence = margin / (margin + 1.0) if margin else 0.0
    return {
        "preferred": winner if activated else None,
        "activated": activated,
        "confidence": round(confidence, 6),
        "scores": rounded_scores,
        "explanation": {
            "contributing": contributing,
            "eligible_count": len(contributing),
            "positive_counts": positives,
            "ignored_by_reason": dict(sorted(ignored.items())),
            "activation_margin": ACTIVATION_MARGIN,
        },
    }


def choose_option(records: Sequence[dict], *, person: str, context: str,
                  options: Sequence[str], default: str, now: dt.datetime,
                  controls: dict | None = None) -> dict:
    """Choose only among caller-offered options; generated evidence is inert."""
    offered = list(options)
    if not offered or len(set(offered)) != len(offered):
        raise ValueError("options must be non-empty and unique")
    if default not in offered:
        raise ValueError("default must be one of options")
    derived = derive_preference(
        records, person=person, context=context, options=offered, now=now,
    )
    chosen = derived["preferred"] if derived["activated"] else default
    return {
        "chosen": chosen,
        "adapted": derived["activated"],
        "confidence": derived["confidence"],
        "default": default,
        "person": person,
        "context": context,
        "scores": derived["scores"],
        "explanation": derived["explanation"],
        "controls": dict(controls or {}),
    }
