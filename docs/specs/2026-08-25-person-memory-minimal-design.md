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

`bin/px-person-seed` (dry-run by default; `--write` to append) writes
operator-known facts through the same canonical `append_person_facts` writer — never by editing the JSONL directly, never
via an LLM. Seed records are structurally distinguishable from
conversational extraction: `source`/`source_channel` is `operator_seed`,
the evidence names the operator as the asserting actor, and kind remains
`report`. **An operator-seeded fact must never render as "Obi told me"** —
attribution follows the record, and the record says who actually asserted
it. Optional expiry and supersession work exactly as above. Only benign,
stable facts are seeded; nothing sensitive (health, family conflict,
school support, private messages, location).

## Retrieval

`people.person_context()` injects into exactly two prompts:
`voice_loop.build_model_prompt` (SPARK persona only) and `api.post_obi_chat`.
Never GREMLIN/VIXEN; never reflection, public chat, blog, or social — the
reader allowlist and the two exact call sites are pinned by
`test_people_invariants.py`. Relevance is tag overlap with the query (same
tokenizer the records were tagged with); no overlap returns "" — no
recent-padding, ever. Rendered lines are compact, carry attribution decided
by the record's `source` ("Adrian told you that Obi likes dinosaurs." for a
seed vs 'Obi told you (2 days ago): "…"' for a direct report), and quote the
stored fact text verbatim so every injected line is mechanically traceable
to its record.

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


## Explicit personal menu commands (#33, 24 October 2026)

The later #33 decision supersedes its old tag-in-notes proposal. An explicit
human add/remove command may now be represented as a structured **preference**
in the same private person store: subject, menu item, energy/context, namespaced
stable topic and positive/negative polarity. No fourth fact class or parallel
notes convention is introduced. `record_person_facts(..., menu=...)` validates
the proposed fields against an anchored, bounded actual human command; ordinary
conversation matching still rejects commands. The model cannot supply the raw
request through action parameters. Voice/private-chat callers bind that request,
and the executor discards an inherited request before applying trusted overrides.

The source/provenance is `menu_command`, kind `report`, with the existing confidence
ceiling. Rendering says the person requested a menu change; it does not assert an
observed therapeutic benefit or invent that the person “likes” it. Add/remove
supersession is decided under the same append lock. Expiry/read filtering and
retention remain the existing store's rules. Removal appends a negative record;
no record is deleted by the feature. Re-adding explicitly supersedes that removal.

The menu tool reads only SPARK's scoped current entries/exclusions, merges them
with unchanged defaults and prefers personal entries. Adult/unknown personas
are refused before any private read or child action. No bridge to reflection,
public chat, social, blog or MCP resources is added. Old note-tag experiments,
if any exist, are left untouched and are not automatically imported.

A direct CLI write needs an explicit matching `PX_DOPAMINE_REQUEST`; generic tool
calls without that source are refused. Dry tool previews skip all writes and
voice/emote children, read session snapshots without healing or locks, skip dotenv/log-directory
startup and disable bytecode writes. This promise concerns the tool; normal
caller-level conversation logging is separate. Both actions remain under the
existing audio classification because successful live changes acknowledge via
the established voice sink; no effect-table override or policy bypass is added.

Offline tests use synthetic private stores and stubbed children. No real personal
entry, microphone/audio, GPIO or hardware action is used for source validation.
Adrian/Obi own personal entries and actual-use acceptance after deployment.

An unreadable, corrupt, recovered or invalid session cannot grant personal-menu
access. Live operations are refused; a dry preview uses only generic defaults
and does not repair the session. A valid explicit default persona remains SPARK,
matching the normal session template. The existing subject=obi convention is
preserved; this feature does not identify a physical speaker.

A missing persona field is unavailable context; explicit null/empty remains the
normal default. Menu selection reads one strict snapshot for positive entries
and exclusions. Malformed nonblank rows, damaged menu metadata or unreadable
history refuse suggestions and mutations instead of reviving a removed item.
No automatic repair/deletion occurs; preserve the ledger before operator repair.
General legacy retrieval keeps its existing lenient behaviour; mutations validate
the history they will supersede.
