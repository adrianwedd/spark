"""Narrow, provenance-backed adaptation from lived contextual experience."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from collections.abc import Iterable, Sequence
from pathlib import Path

from filelock import FileLock

from pxh import provenance


ELIGIBLE_KINDS = frozenset({"observation", "report", "verification"})
OUTCOMES = frozenset({"positive", "negative"})
MAX_FIELD_LENGTH = 100
HALF_LIFE_DAYS = 90.0
ACTIVATION_MARGIN = 0.75
MIN_POSITIVE_EXPERIENCES = 2
LOCK_TIMEOUT_S = 10
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


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


def experience_file(persona: str = "spark") -> Path:
    """Return the persona-scoped append-only experience store."""
    name = str(persona or "spark").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError("persona may contain only letters, numbers, _ and -")
    state_dir = Path(os.environ.get("PX_STATE_DIR", PROJECT_ROOT / "state"))
    return state_dir / f"preference-experiences-{name}.jsonl"


def _is_valid_record(record: object) -> bool:
    if not isinstance(record, dict) or not record.get("id"):
        return False
    try:
        _timestamp(record.get("ts"))
        for name in ("person", "context", "option"):
            _field(name, record.get(name))
    except (TypeError, ValueError):
        return False
    if record.get("outcome") not in OUTCOMES:
        return False
    block = provenance.read_provenance(record)
    return bool(not block["legacy"] and block["source"] != "unrecorded"
                and block["evidence"])


def append_experience(record: dict, *, path: Path | None = None,
                      persona: str = "spark") -> None:
    """Append one intact experience; never updates an earlier line."""
    if not _is_valid_record(record):
        raise ValueError("experience must be a valid stamped record")
    target = Path(path) if path is not None else experience_file(persona)
    target.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(target) + ".lock", timeout=LOCK_TIMEOUT_S):
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":"), sort_keys=True))
            handle.write("\n")


def load_experiences(*, path: Path | None = None,
                     persona: str = "spark") -> tuple[list[dict], dict]:
    """Load valid experience lines and report corruption without rewriting."""
    target = Path(path) if path is not None else experience_file(persona)
    diagnostics = {"total": 0, "valid": 0, "invalid": 0}
    if not target.exists():
        return [], diagnostics
    try:
        with FileLock(str(target) + ".lock", timeout=LOCK_TIMEOUT_S):
            lines = target.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        diagnostics["read_error"] = type(exc).__name__
        return [], diagnostics
    records: list[dict] = []
    for line in lines:
        diagnostics["total"] += 1
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            diagnostics["invalid"] += 1
            continue
        if not _is_valid_record(record):
            diagnostics["invalid"] += 1
            continue
        records.append(record)
        diagnostics["valid"] += 1
    return records, diagnostics


def _ignore(counts: dict[str, int], reason: str) -> None:
    counts[reason] = counts.get(reason, 0) + 1


def _scoped_supersessions(records: Sequence[dict], *, person: str,
                          context: str, options: Sequence[str]) -> list[dict]:
    """Apply only corrections authorized for the same policy scope and option."""
    originals = {
        str(record.get("id")): record for record in records
        if isinstance(record, dict) and record.get("id")
    }
    scoped: list[dict] = []
    seen_policy_evidence: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        copy = dict(record)
        raw = record.get("provenance")
        if isinstance(raw, dict):
            block = dict(raw)
            copy["provenance"] = block
            replacement_p = provenance.read_provenance(record)
            replacement_authorized = (
                record.get("person") == person
                and record.get("context") == context
                and record.get("option") in options
                and record.get("outcome") in OUTCOMES
                and replacement_p["kind"] in ELIGIBLE_KINDS
                and bool(replacement_p["evidence"])
                and replacement_p["confidence"] > 0.0
                and not any(
                    ref in seen_policy_evidence for ref in replacement_p["evidence"]
                )
            )
            try:
                _timestamp(record.get("ts"))
            except (TypeError, ValueError):
                replacement_authorized = False
            if replacement_authorized:
                seen_policy_evidence.update(replacement_p["evidence"])
            allowed: list[str] = []
            if replacement_authorized:
                for old_id in replacement_p["supersedes"]:
                    old = originals.get(old_id)
                    if (old and old.get("person") == person
                            and old.get("context") == context
                            and old.get("option") == record.get("option")):
                        allowed.append(old_id)
            block["supersedes"] = allowed
        scoped.append(copy)
    return provenance.apply_supersessions(scoped)


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

    for record in _scoped_supersessions(
        records, person=person, context=context, options=offered,
    ):
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
        if outcome == "positive" and weight > 0.0:
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
        and margin > ACTIVATION_MARGIN
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
