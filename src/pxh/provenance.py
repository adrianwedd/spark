"""Epistemic provenance for SPARK's durable claims — issue #170.

SPARK writes two kinds of durable state that later re-enter cognition:
`state/notes[-persona].jsonl` and `state/memories-{persona}.jsonl`. Before this
module, a record carried at most a coarse `source` string, so a speculative
inner thought distilled by the nightly consolidation pass was indistinguishable,
at retrieval time, from something SPARK had actually seen or been told. This
module attaches the minimum metadata needed to answer, of any durable claim,
*where did this come from?*

Five kinds, derived from what the existing writers actually produce:

| kind           | means                                     | who writes it today            |
|----------------|-------------------------------------------|--------------------------------|
| `observation`  | SPARK perceived it via its own sensors    | wander's scene descriptions    |
| `report`       | a person or external source asserted it   | voice-loop remember, research  |
| `inference`    | SPARK worked it out from other records    | (available; no writer yet)     |
| `narrative`    | SPARK's own generated prose about itself  | consolidation, compose, mind   |
| `verification` | checked against something outside SPARK   | (available; no writer yet)     |
| `unknown`      | provenance was never recorded             | every record written pre-#170  |

`inference` and `verification` have no writer yet. That is stated rather than
papered over: nothing in SPARK currently checks a belief against the world, and
pretending otherwise would be the exact failure this module exists to prevent.

**Confidence is capped by kind.** A writer may ask for any confidence it likes;
what it gets is clamped to that kind's ceiling, on write *and* again on read.
Generated prose therefore cannot present itself as perception no matter how the
claim was produced, edited, or hand-written into the file afterwards. The
ordering between the ceilings is the safety property; the exact numbers are
calibration.

These constants deliberately do **not** live in `spark_config.py`. That module
is the self-evolution whitelist's primary target, and a system able to propose
raising its own confidence ceilings could talk itself out of this guarantee.

**Legacy records stay readable and stay honest.** A record with no `provenance`
key reads as `unknown` at `unknown`'s low ceiling, keeping whatever coarse
`source`/`type` string it had so it can still say something about its origin —
but its epistemic kind is never inferred from that string. A record that says
`source: consolidation` might have come from any of consolidation's inputs; the
absence of provenance is a fact about the record, not a puzzle to solve.

**Correction never deletes.** A newer record can name an older one in
`provenance.supersedes`. `apply_supersessions()` then marks the old record
`superseded_by` — on a copy, leaving the stored history untouched — and
`read_provenance()` discounts its confidence. Both records remain on disk and
readable, so SPARK can hold "I believed X, then saw Y" rather than silently
having always believed Y. Only system code may write `supersedes`: a model that
could supersede its own records could quietly retire inconvenient ones.
"""
from __future__ import annotations

import uuid

from pxh.time import utc_timestamp

KINDS = ("observation", "report", "inference", "narrative", "verification",
         "unknown")

# Ceilings, not suggestions: clamped on write and again on read.
CONFIDENCE_CEILING = {
    "observation": 1.0,
    "verification": 1.0,
    "report": 0.9,        # people misremember and speak loosely
    "inference": 0.6,
    "narrative": 0.5,     # SPARK's own prose about SPARK
    "unknown": 0.3,       # legacy — never silently trusted
}

DEFAULT_CONFIDENCE = {
    "observation": 0.8,   # a camera plus a vision model is not ground truth
    "verification": 0.9,
    "report": 0.7,
    "inference": 0.5,
    "narrative": 0.4,
    "unknown": 0.2,
}

# Phrases land verbatim in the reflection prompt, so they read as SPARK's own
# sense of where a memory came from rather than as schema.
KIND_LABELS = {
    "observation": "I saw this myself",
    "report": "someone told me this",
    "inference": "I worked this out",
    "narrative": "my own reflection, unverified",
    "verification": "checked against something outside me",
    "unknown": "source unknown, an older record",
}

MAX_EVIDENCE_REFS = 8
MAX_REF_LEN = 200
# A superseded claim is kept and discounted, not deleted — SPARK should be able
# to remember having been wrong.
SUPERSEDED_CONFIDENCE_FACTOR = 0.25


def _clamp_confidence(kind: str, value: object) -> float:
    ceiling = CONFIDENCE_CEILING[kind]
    if value is None:
        return DEFAULT_CONFIDENCE[kind]
    try:
        num = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_CONFIDENCE[kind]
    if num != num:  # NaN
        return DEFAULT_CONFIDENCE[kind]
    return max(0.0, min(ceiling, num))


def _clean_refs(evidence: object) -> list[str]:
    if not isinstance(evidence, (list, tuple)):
        return []
    out: list[str] = []
    for ref in evidence:
        if not ref:
            continue
        text = str(ref).strip()[:MAX_REF_LEN]
        if text:
            out.append(text)
        if len(out) >= MAX_EVIDENCE_REFS:
            break
    return out


def make_provenance(kind: str, source: str, evidence: object = None,
                    confidence: object = None,
                    recorded_at: str | None = None) -> dict:
    """Build a provenance block. Strict: our writers pass literals, so a bad
    kind or a missing source is a bug in the caller, not data to tolerate."""
    if kind not in KINDS:
        raise ValueError(f"unknown provenance kind {kind!r}; expected one of {KINDS}")
    src = str(source or "").strip()
    if not src:
        raise ValueError("provenance requires a non-empty source")
    return {
        "kind": kind,
        "source": src[:MAX_REF_LEN],
        "evidence": _clean_refs(evidence),
        "confidence": _clamp_confidence(kind, confidence),
        "recorded_at": recorded_at or utc_timestamp(),
        "supersedes": [],
        "legacy": False,
    }


def stamp(record: dict, kind: str, source: str, evidence: object = None,
          confidence: object = None) -> dict:
    """Attach provenance and a stable id to a record, in place. Returns it."""
    record["provenance"] = make_provenance(kind, source, evidence, confidence)
    record.setdefault("id", uuid.uuid4().hex[:16])
    return record


def _legacy_provenance(record: object) -> dict:
    """Provenance for a record written before #170.

    The coarse `source`/`type` string is kept — it is still a partial answer to
    "where from" — but it is never promoted into a kind. A stored string is not
    evidence of how the claim was produced.
    """
    coarse = ""
    if isinstance(record, dict):
        for key in ("source", "type", "tag"):
            value = record.get(key)
            if value:
                coarse = str(value)[:MAX_REF_LEN]
                break
    recorded_at = ""
    if isinstance(record, dict):
        recorded_at = str(record.get("ts") or "")[:MAX_REF_LEN]
    return {
        "kind": "unknown",
        "source": f"{coarse} (legacy, provenance not recorded)" if coarse
                  else "legacy, provenance not recorded",
        "evidence": [],
        "confidence": DEFAULT_CONFIDENCE["unknown"],
        "recorded_at": recorded_at,
        "supersedes": [],
        "legacy": True,
    }


def read_provenance(record: object) -> dict:
    """Resolve any durable record to a provenance block. Never raises.

    Lenient where `make_provenance` is strict: this reads whatever is on disk,
    including records written by older code, corrupted lines, and values a
    human edited in by hand. Anything it cannot vouch for degrades to
    `unknown`; nothing here can promote a record.
    """
    if not isinstance(record, dict):
        return _legacy_provenance(record)
    raw = record.get("provenance")
    if not isinstance(raw, dict):
        return _legacy_provenance(record)
    kind = raw.get("kind")
    if kind not in KINDS:
        kind = "unknown"
    source = str(raw.get("source") or "").strip()[:MAX_REF_LEN]
    confidence = _clamp_confidence(kind, raw.get("confidence"))
    # Derived at read time, like health.py's status: a discount that lived in
    # the stored record could be lost by any writer that rewrote the line.
    if record.get("superseded_by"):
        confidence *= SUPERSEDED_CONFIDENCE_FACTOR
    return {
        "kind": kind,
        "source": source or "unrecorded",
        "evidence": _clean_refs(raw.get("evidence")),
        "confidence": confidence,
        "recorded_at": str(raw.get("recorded_at") or record.get("ts") or ""),
        "supersedes": _clean_refs(raw.get("supersedes")),
        "legacy": False,
    }


def describe(record: object) -> str:
    """One short phrase answering "where did this come from?"."""
    p = read_provenance(record)
    label = KIND_LABELS.get(p["kind"], KIND_LABELS["unknown"])
    if isinstance(record, dict) and record.get("superseded_by"):
        return f"{label}; since superseded"
    return label


# --- supersession ----------------------------------------------------------

def mark_supersedes(new_record: dict, old_record: dict) -> dict:
    """Record that `new_record` replaces `old_record`. System code only."""
    old_id = (old_record or {}).get("id")
    if not old_id:
        return new_record
    block = new_record.get("provenance")
    if not isinstance(block, dict):
        return new_record
    refs = list(block.get("supersedes") or [])
    if old_id not in refs:
        refs.append(old_id)
    block["supersedes"] = refs[:MAX_EVIDENCE_REFS]
    return new_record


def apply_supersessions(records: list[dict]) -> list[dict]:
    """Return copies with `superseded_by` filled in. History is never removed.

    Shallow copies: the caller gets a view it can annotate without writing the
    derived state back into the store.
    """
    replaced_by: dict[str, str] = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        block = rec.get("provenance")
        if not isinstance(block, dict):
            continue
        for old_id in _clean_refs(block.get("supersedes")):
            replaced_by[old_id] = str(rec.get("id") or "")
    out: list[dict] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        copy = dict(rec)
        replacement = replaced_by.get(str(copy.get("id") or "\x00"))
        if replacement:
            copy["superseded_by"] = replacement
        out.append(copy)
    return out


def is_superseded(record: object) -> bool:
    return bool(isinstance(record, dict) and record.get("superseded_by"))
