# Person Memory — Minimal Design (2026-08-25)

The smallest thing that lets SPARK remember what the people it talks to have
literally told it about themselves, without any possibility of that memory
reaching the public pipeline, the performance personas, or a model's
imagination. Implementation: `src/pxh/people.py`. Structural invariants:
`tests/test_people_invariants.py`. False-positive corpus (the extraction
spec, in executable form): `tests/test_people.py`.

## Scope and non-goals

Three fact kinds, and only three: **stable preferences**, **stated
relationships**, **explicit first-person commitments**. Deliberately out of
scope, now and until a new design supersedes this one: embeddings, vector
stores, episodic memory, spatial memory, LLM-based extraction, additional
fact classes. A missed fact costs one turn of continuity; a fabricated or
leaked one is a robot telling a child something they never said, or telling
the internet something a child said in private. Every trade in this design
is made in that direction.

## Store

`state/people-{persona}.jsonl`, one JSON record per line, append-only,
trimmed to the last 2000 lines. Record shape is compatible with
`memory.py`'s records (`ts`, `subject`, `text`, `tags`, `importance`,
`source`, provenance block) so the existing relevance scorer can read it
unchanged when retrieval lands. Extra fields: `fact_kind`, `topic`,
`polarity`, `expires_ts`.

**The separate file is the privacy firewall.** Reflection reads
`memories-{persona}.jsonl`, and reflection's output flows to
`thoughts-spark.jsonl` → `/api/v1/public/thoughts` → the site feed, the blog
and Bluesky. Person facts live in a file that `mind.py` never opens — the
same allowlist-by-construction discipline as `_REFLECTION_AWARENESS_KEYS`,
enforced by the filesystem and pinned by source-scan tests, not by prompt
prose.

## Writer

Deterministic regex extraction over clauses. **No model anywhere in the
write path** — a fact exists only because a human sentence asserted it.
Provenance kind is the hardcoded literal `report` (confidence ceiling 0.9);
no caller or model can choose a kind. The matcher is biased to rejection:
questions, hedges, conditionals, reported speech, second/third person,
hyperbole, deictic objects and negated intents are refused outright.
`tests/test_people.py`'s rejection corpus is the authority; patterns are
widened only against it, never to raise recall in the abstract.

### Evidence minimisation

Evidence stored with a fact is the **exact matched clause** plus the source
message reference — never the whole utterance. "I'm sad about school today,
but I really like dinosaurs" stores the dinosaur clause and nothing else;
the full message remains only in its source log (`obi_chat.jsonl`, the
conversation buffer), recoverable by id. A store whose stated purpose is
narrowly-scoped person facts must not accumulate unrelated private context
as a side effect of faithful provenance.

### Identity threading

A channel that has real event identity must thread it: obi-chat entries
carry an `id` and that id is the evidence reference (`obi_chat:<id>`).
Voice conversation turns carry no id, so their reference is a content hash
of the utterance (`voice:turn:<sha1-12>`) — an explicit fallback for
id-less channels, not an accepted normal path.

### Who writes

Exactly two call sites: `voice_loop.record_conversation_turn` (the user's
words only, SPARK persona only) and `api._append_obi_chat_api` (role `obi`
only, after the message is durably stored so evidence ids always name an
existing line). The persona gate lives inside `record_person_facts`, not at
the call sites: GREMLIN and VIXEN are refused at the writer, and a
per-persona filename alone would have been two stores, not a firewall.
SPARK's own replies are never facts about Obi. The writer never raises into
its caller.

### Commitments expire; nothing is deleted

Commitments get a days-not-weeks TTL (default 3, ceiling 10, tightened by a
named day, computed in Hobart time). Expiry filters at read time; records
stay on disk. Corrections use `provenance.supersedes` on the same
`(subject, fact_kind, topic)` — both records kept, one surfaced.

## Operator seeding (stage 2)

A small CLI writes operator-known facts through the same canonical
`append_person_facts` writer — never by editing the JSONL directly, never
via an LLM. Seed records are structurally distinguishable from
conversational extraction: `source`/`source_channel` is `operator_seed`,
the evidence names the operator as the asserting actor, and kind remains
`report`. **An operator-seeded fact must never render as "Obi told me"** —
attribution follows the record, and the record says who actually asserted
it. Optional expiry and supersession work exactly as above. Only benign,
stable facts are seeded; nothing sensitive (health, family conflict,
school support, private messages, location).

## Retrieval (stage 5 — not in the writer PR)

Injection into exactly two prompts: the SPARK voice prompt (persona ==
spark) and the obi-chat prompt. Never GREMLIN/VIXEN; never reflection,
public chat, blog, or social. Retrieval returns zero when nothing is
relevant — no recent-padding, ever. Injected lines are compact, preserve
attribution ("Adrian told you that…" vs "Obi told you 2 days ago that…"),
and every injected statement is mechanically traceable to a stored record.

## Enforcement

- `tests/test_people_invariants.py`: source scans pin that `mind.py` has no
  route to the store, no module reads it before retrieval lands, only the
  two named call sites write, and the writer contains no model call.
- `src/pxh/people.py` and `tests/test_people_invariants.py` are blacklisted
  from px-evolve: `mind.py` and `voice_loop.py` are whitelisted evolution
  targets, so the module deciding whether a bridge exists must not be one
  SPARK can propose editing.
- `tests/conftest.py` isolates the store autouse, so test utterances never
  land fabricated facts in the live robot's `state/`.
