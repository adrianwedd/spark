# Epistemic Provenance

**Owns:** `src/pxh/provenance.py` — where every durable claim came from, and
how much it is allowed to be believed.

---

## Invariant

### Every durable claim records its origin

SPARK writes two stores that later re-enter cognition:
`state/notes[-persona].jsonl` and `state/memories-{persona}.jsonl`. Retrieved
memory must be able to distinguish what SPARK **saw**, was **told**, a model
**interpreted**, SPARK **worked out**, or SPARK simply **wrote about itself**.

### Seven kinds, ordered by how much they may be trusted

| Kind | Means | Ceiling | Default | Written today by |
|---|---|---|---|---|
| `observation` | direct/deterministic sensor content | 1.0 | 0.8 | direct sensor writers |
| `verification` | checked against something outside SPARK | 1.0 | 0.9 | *(no writer yet)* |
| `report` | a person or external source asserted it | 0.9 | 0.7 | voice-loop remember, research |
| `model_perception` | a model interpreted sensor evidence | 0.75 | 0.65 | wander's scene descriptions |
| `inference` | SPARK worked it out from other records | 0.6 | 0.5 | *(no writer yet)* |
| `narrative` | SPARK's own generated prose about itself | 0.5 | 0.4 | consolidation, compose, mind |
| `unknown` | provenance was never recorded | 0.3 | 0.2 | every record written pre-#170 |

`inference` and `verification` have **no writer**. That is stated rather than
papered over: nothing in SPARK currently checks a belief against the world, and
pretending otherwise would be the exact failure this module exists to prevent.

> `src/pxh/provenance.py`'s module docstring still opens with the words "Six
> kinds" — a stale count left over from before `model_perception` was added by
> #177. The table beneath it and `provenance.KINDS` are both correct at seven.

### Ceilings are clamped on write *and* on read

A writer may ask for any confidence it likes; what it gets is clamped. Clamping
again on read means generated prose cannot present itself as perception no
matter how the record was produced, edited, or hand-written into the file
afterwards.

**The ordering between the ceilings is the safety property. The exact numbers
are calibration.** Re-tuning a number is ordinary work; reordering two kinds is
a change to the guarantee.

### The model never chooses a kind

Callers set constants. Consolidation allowlists its input fields. There is no
path by which a model labels its own output as `observation`.

### The ceilings deliberately do not live in `spark_config.py`

That module is the self-evolution whitelist's primary target. A system able to
propose raising its own confidence ceilings could talk itself out of this
guarantee. See
[architecture/policy-and-authority](policy-and-authority.md).

### Writes are strict; reads are lenient

Invalid or legacy data stays **readable** as `unknown`, keeping whatever coarse
`source`/`type` string it had — but its epistemic kind is never *inferred* from
that string. A record saying `source: consolidation` might have come from any
of consolidation's inputs; the absence of provenance is a fact about the
record, not a puzzle to solve.

### Correction never deletes

A newer record names an older one in `provenance.supersedes`.
`apply_supersessions()` marks the old record `superseded_by` **on a copy**,
leaving stored history untouched, and `read_provenance()` discounts its
confidence by `SUPERSEDED_CONFIDENCE_FACTOR`. Both records stay on disk, so
SPARK can hold *"I believed X, then saw Y"* rather than silently having always
believed Y.

**Only system code may write `supersedes`.** A model that could supersede its
own records could quietly retire inconvenient ones.

### Retrieval returns topical matches only

Relevance retrieval never pads with recent-but-irrelevant records. Explicit
`mode="recent"` remains available for callers that want recency. A populated
store with no relevant hit does **not** fall back to raw notes.

---

## Why it looks like this

*History, not rule.*

Issue #170. Before this module a record carried at most a coarse `source`
string, so a speculative inner thought distilled by the nightly consolidation
pass was indistinguishable, at retrieval time, from something SPARK had
actually seen or been told. Reflection then cited its own guesses back to
itself as evidence.

`model_perception` (#177) was added because scene descriptions from a vision
model are neither observation nor narrative: they are grounded in real sensor
evidence, but they are an interpretation of it. Filing them as `observation`
overstated them; filing them as `narrative` threw away the grounding.

Related: [architecture/memory-and-learning](memory-and-learning.md), which
consumes these records.
