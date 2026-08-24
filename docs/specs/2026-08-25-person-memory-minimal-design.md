# The smallest change that makes SPARK remember Obi — design

Status: design. Companion to `docs/audits/2026-08-25-spark-redteam-peer-review.md`
(§4). No code in this document; it names exact existing symbols to reuse and
exact call sites to touch.

## Thesis

SPARK already has every primitive it needs to remember a person. It has a
deterministic, free, provenance-aware, supersession-aware retrieval engine
(`memory.retrieve_memories`), a provenance system with a `report` kind meaning
*"someone told me this"* at a 0.9 ceiling (`provenance.py`), and a conversation
event stream that already contains the facts (`state/obi_chat.jsonl`, the voice
conversation buffer). What it lacks is **three small pieces of plumbing**:

1. a **deterministic writer** that turns explicit first-person statements in
   conversation into `report`-kind facts (no LLM, no guessing);
2. a **physically separate people store** those facts land in — separate so they
   can never ride the reflection → public-thoughts → Bluesky path;
3. a **retrieval injection** into the two SPARK-identity channels (voice-as-SPARK,
   obi-chat) and nowhere else.

Everything below is roughly a page of new code plus two one-line call-site edits,
gated behind one genuine bug fix. It is deliberately **not** a memory platform.

The design also refuses a tempting wrong turn: `contextual_preference.py` is *not*
the person-fact store. It is a preference-*derivation* engine keyed on
`(person, context, option, outcome)` — it models **choices SPARK makes and how
they turned out**, not **facts a person stated**. It is the right tool for B9's
outcome-learning loop and the wrong tool for "Obi likes dinosaurs." Reusing it
for recall would force every fact into an option/outcome shape it doesn't have.
Keep it out of this change (see *What NOT to build*).

---

## Precondition (hard boundary): fix the message_obi redaction leak first

Per the commission's part 7, and independently a real bug
(peer-review §5.0): `display_text = "[private message to Obi]"` is computed as a
**local variable** in the reflection function (`mind.py:2897`) and used only for
the thoughts-file persist. `expression()` — a *different* function — re-reads the
**raw** thought at `mind.py:3173` (`text = thought.get("thought","")`) and writes
it verbatim into session history (`mind.py:3560-3572`), from which it reaches the
voice prompt (incl. GREMLIN/VIXEN), the reflection prompt via awareness digestion
(`mind.py:2046-2061` → `_REFLECTION_AWARENESS_KEYS`), and `GET /api/v1/session`.

**Fix:** make the redaction a property of the *record*, not one call site. At
`mind.py:2897` store the placeholder onto the thought
(`thought["display_text"] = display_text`), and at every shared/public sink read
`thought.get("display_text") or thought.get("thought","")` — the history write
(`mind.py:3173`), the awareness digest (`mind.py:2046-2061`), and the
`/api/v1/session` history projection. Pin with a test that a `message_obi`
thought's raw text appears in **no** downstream surface.

This is a precondition because the memory work adds *more* conversation-derived
text flowing toward prompts; the redaction discipline ("private context never
becomes retrievable/injectable") must hold before, not after.

---

## 1. What facts about a person are worth retaining

The retention rule is **assertion, not inference**: SPARK stores only what a
person *literally said about themselves*, quoted, with the source utterance as
evidence. That single rule makes the store a `report` store by construction and
makes the exclusions fall out for free.

**Retain** (all as verbatim or near-verbatim `report` facts, subject-tagged):
- **Stable preferences** — "I like X", "my favourite Y is Z", "I don't like W".
- **Names / relationships** — "my friend Sam", "my mum Laura", "my brother".
- **Recurring interests** — topics the person raises repeatedly (frequency is
  computed at read time from repeated report facts, never asserted on write).
- **Commitments / promises** — "I'll show you tomorrow", "we're going to the
  beach on Saturday" (these carry a natural expiry — see contamination §4).
- **Recent meaningful events** — "I lost a tooth", "it was my birthday".
- **Interaction preferences** — "talk quietly", "call me Obi not Obadiah".

**Exclude — enforced by the extractor, not by prompt prose:**
- **Transient chatter** — greetings, acknowledgements, questions, commands to the
  robot. The extractor only fires on first-person *assertions*, so "turn left" or
  "what do you see" produce nothing.
- **Speculative / model-guessed facts** — there is no model in the write path, so
  the model cannot guess. A fact exists only if a human sentence asserted it.
  This is the strongest possible form of the "no model-guessed facts" exclusion.
- **Inferences** — "Obi seems tired" is `inference` kind (ceiling 0.6) and is
  *not written by this path at all*; only `report` facts are.
- **Sensitive private details** — anything sourced from a `message_obi` DM or any
  redacted/private context is out of scope by the precondition above; the
  extractor reads only the non-private conversation surfaces.

The confidence ceiling does the epistemics: `report` caps at 0.9
(`provenance.CONFIDENCE_CEILING["report"]`), so a stated fact is never presented
as ground truth, and "people misremember and speak loosely" is already the
documented rationale.

---

## 2. The smallest write path

**Deterministic extraction, zero LLM.** A new pure module — call it
`src/pxh/people.py` (~80 lines) — exposes one function:

```
extract_person_facts(*, role, text, ts, msg_id, subject) -> list[dict]
```

It splits `text` into sentences, matches a small allowlist of first-person
assertion patterns (`I (like|love|hate|don't like|prefer) …`, `my (favourite|
favorite|friend|mum|dad|brother|sister|birthday) …`, `I('m| am) going to …`,
`call me …`, `I (lost|got|made|found) …`), and for each hit emits a record via
`provenance.stamp(record, "report", source, evidence=[…])`:

```
{ "ts": ts, "subject": subject, "text": <verbatim clause>,
  "tags": <lowercased content tokens>, "importance": 0.5,
  "source": "conversation", provenance: {kind: "report", …} }
```

- **kind is fixed to `report`** in code (never model-chosen), source
  `"conversation"`, evidence `["obi_chat.jsonl", f"msg:{msg_id}", verbatim quote]`
  so every fact is traceable to the exact utterance.
- **text is the person's own words**, lightly normalised (strip filler, keep the
  clause). No paraphrase — a paraphrase is an inference.
- `subject` is `"obi"` for obi-chat and for voice turns while the SPARK persona is
  active; the field lets the store hold facts about several people without a
  schema change.

**Storage reuses `memory.py`'s primitives but a *different file*.** Add
`people.people_file(persona) -> state/people-{persona}.jsonl` and reuse
`memory.append_memories`-style append (FileLock, `MEMORIES_LIMIT` trim). The
records are shape-compatible with `memory`'s, so retrieval reuses
`memory.score_memory` / `_tokenize` verbatim.

**Two call sites, one line each:**
- `api._append_obi_chat_api` (`api.py:1322`) — after appending an `obi`-role
  message, call `extract_person_facts(...)` and append any hits. (Only the
  `obi` role; SPARK's own replies are not facts about Obi.)
- `voice_loop.record_conversation_turn` (`voice_loop.py:341`) — when
  `persona == "spark"`, extract from the **user** utterance only.

No new daemon, no new LLM session, no consolidation change. Consolidation stays
exactly as it is (`narrative`-only, `memory.consolidate`) — person facts
deliberately do **not** go through it, because consolidation writes SPARK's prose
about its own thoughts and would (a) launder a `report` into `narrative` and
(b) feed the public path.

---

## 3. The smallest retrieval path

Compared:

| Option | Cost | Verdict |
|---|---|---|
| **Inject top-N relevant person facts into voice/obi-chat prompts** | ~2 lines/site, reuses `retrieve_memories` | **Chosen.** Free, deterministic, already proven, provenance-preserving. |
| Semantic tool-recall (brain round-trip) | new tool + a brain turn per query | Rejected — adds a Claude round trip and latency to every turn; heavier than the whole rest of this design. |
| Contextual-preference lookup (`choose_option`) | reuses dead engine | Rejected — wrong shape (choices, not facts); see thesis. |
| Recent episodic (`mode="recent"`) | trivial | Rejected — no relevance filter, injects irrelevant facts; fails the "no irrelevant injection" metric by construction. |

**Chosen path:** in both SPARK-identity channels, before building the prompt,
call `people.retrieve_person_facts(user_text, subject="obi", n=3)` — a thin
wrapper over the existing relevance-mode scorer (non-zero score only; **never
pads** — that non-padding property already exists in `retrieve_memories` and is
the reason recency-alone can't manufacture a false hit). Render each fact with
its provenance framing:

```
What I remember Obi has told me (he said these — I may be misremembering):
- "I like dinosaurs" (Obi told me, 2 days ago)
- "my friend's name is Sam" (Obi told me, last week)
```

Injection points, already located:
- **Voice:** append one `context_sections` entry in
  `voice_loop.build_model_prompt` (`voice_loop.py:625`), **only when
  `_active_persona == "spark"`** — GREMLIN/VIXEN never receive Obi's facts
  (persona firewall + performance-character boundary).
- **Obi-chat:** prepend a memory block to `prompt` in `api.post_obi_chat`
  (`api.py:1608`), before `history_block`. This runs on M5 with no tools
  (`_call_claude_public(kind="obi_chat")`), so injected text is inert data.

"Visible continuity" is created by exactly this: on Day 3, Obi asks a question
whose tokens overlap a Day-1 fact, the scorer surfaces it, and SPARK answers from
it — with the "he told me" framing that keeps it honest.

---

## 4. Preventing contamination

Every retrieved fact already carries, via `provenance.stamp` + the record fields,
the five required properties — this design adds none, it just refuses to drop any:

| Property | Where it lives |
|---|---|
| source / provenance | `provenance.kind="report"`, `source="conversation"`, `evidence=[msg id, verbatim]` |
| confidence | `provenance.confidence` (≤0.9 report ceiling, clamped on read too) |
| timestamp | record `ts` + `provenance.recorded_at` |
| subject / person | new `subject` field |
| report vs inference | the kind itself — and *only* `report` is ever written here; `inference`/`narrative` never enter this store |

Three structural firewalls, in order of importance:

1. **Physical file separation.** Person facts live in `state/people-{persona}.jsonl`,
   which **reflection never reads** (reflection reads `memories-{persona}.jsonl`
   via `mind.py:2570`). So a stated fact cannot reach the reflection prompt →
   `thoughts-spark.jsonl` → `/api/v1/public/thoughts` / blog / Bluesky path. This
   is the same allowlist-by-construction discipline the codebase already uses for
   `_REFLECTION_AWARENESS_KEYS` — a new store stays out of public cognition
   *because it is a different file*, not because a filter remembered to exclude it.
2. **Provenance framing at injection.** Facts are rendered as *"Obi told me"*,
   never as SPARK's own knowledge, using `provenance.describe()`'s
   "someone told me this" label. The model cannot promote a `report` to a
   certainty because the ceiling is clamped on read and the framing says so.
3. **Expiry for commitments.** Facts matched as commitments/events
   (`I'm going to …`, `tomorrow …`) get a short TTL in the tag set and are
   filtered out of retrieval past their horizon, so a stale promise isn't
   surfaced as current. Corrections use the existing `provenance.supersedes`
   mechanism ("call me Obi" supersedes "call me Obadiah") with no deletion.

The redaction precondition (above) closes the fourth path: private DM content
never becomes a conversation event the extractor can see.

---

## 5. How we measure success — a deterministic + lived test

**Lived test (the acceptance gate):**
- **Day 1:** via `POST /api/v1/obi-chat`, Obi states three benign facts:
  "I like dinosaurs", "my best friend is Sam", "I'm learning the drums".
- **Day 3:** Obi asks, *without restating them*: "what animal should we look for
  outside?", "who should I invite over?", "what am I getting better at?".
- **Measure:**
  - **correct recall** — each answer references the matching Day-1 fact;
  - **no fabrication** — no fact SPARK was never told (assert the reply's claims
    trace to a stored `evidence` msg id);
  - **no irrelevant injection** — a Day-3 question about the weather retrieves
    *zero* person facts (relevance-mode returns empty, no padding);
  - **provenance preserved** — the stored records are `report`/0.9 with the
    verbatim evidence and `subject="obi"`;
  - **useful continuity** — a human reads the Day-3 transcript as SPARK
    remembering Obi, not restating a database.

**Deterministic test (CI, `-m "not live"`):** with `isolated_project` and a frozen
clock, drive `extract_person_facts` over the three Day-1 utterances, assert the
three `report` records land in `people-spark.jsonl` with correct provenance;
then `retrieve_person_facts` for each Day-3 query and assert the right fact scores
non-zero while an unrelated query scores zero; assert a `message_obi`-sourced
string is never extractable; assert the injected prompt block contains the
"Obi told me" framing and the timestamp.

---

## 6. Coupling to consolidation health — no silent amnesia

Consolidation is failing **right now** in production
(`state/consolidation_meta.json`: `done:false`, 2 attempts, nothing new in
`memories-spark.jsonl` since 2026-08-23), and it fails **invisibly**: there is no
consolidation health component (`health.py:44-59` `KNOWN_COMPONENTS` omits it) and
`memory.maybe_consolidate` records nothing to health on failure.

Two small additions make amnesia loud:
1. **Add a `px-mind-consolidation` health component.** In `_consolidation_tick`
   (`mind.py:3583-3589`), call `health.record_success/record_failure` on the
   `maybe_consolidate` result. A permanently-failing nightly pass then reads as
   `failing`/`stale` instead of green, with a per-component `STALE_AFTER_S` sized
   to "once daily".
2. **Surface staleness where a human looks.** The dashboard already reads health;
   add a line "long-term memory last formed <date>" driven off the component. If
   consolidation is dead, the person-memory feature still works (it is a separate,
   live write path), but the *self*-memory story is visibly broken rather than
   silently amnesic — which is the honest state today.

Note the separation this buys: person facts (live, deterministic) and self-memory
(LLM consolidation, currently dead) fail independently. The child's experience of
"SPARK remembers me" no longer depends on the fragile nightly Claude pass at all.

---

## Output summary

### Minimal architecture
```
conversation event ─(deterministic extract, report-kind)─▶ state/people-{persona}.jsonl
        │                                                          │
   obi_chat.jsonl                                        retrieve_person_facts
   voice user turn                                     (relevance, non-zero only)
                                                                   │
                              ┌────────────────────────────────────┤
                    voice prompt (persona==spark)          obi-chat prompt (M5, no tools)
                        "Obi told me …"                        "Obi told me …"

   [firewall] people-*.jsonl is NEVER read by reflection → stays out of public thoughts/blog/social
```

### Exact existing modules to reuse
- `pxh.provenance` — `stamp`, `read_provenance`, `describe`, `CONFIDENCE_CEILING`,
  `supersedes`/`apply_supersessions` (report kind, ceilings, correction).
- `pxh.memory` — `_tokenize`, `score_memory`, the relevance/non-padding retrieval
  logic, `append_memories`-style FileLock append + `MEMORIES_LIMIT` trim.
- `pxh.health` — `record_success`/`record_failure`/`read_health` for the
  consolidation component.
- Call sites: `api._append_obi_chat_api` (`api.py:1322`),
  `api.post_obi_chat` (`api.py:1608`), `voice_loop.record_conversation_turn`
  (`voice_loop.py:341`), `voice_loop.build_model_prompt` (`voice_loop.py:625`).

### Code paths to delete / avoid
- **Avoid** routing person facts through `memory.consolidate` — it launders
  `report` → `narrative` and feeds the public path. Keep them in a separate file.
- **Avoid** `contextual_preference.py` for recall — wrong shape (see thesis). It
  is either wired up *separately* for B9 outcome-learning or deleted; it is not
  part of this change.
- **Avoid** `retrieve_memories(mode="recent")` in the channels — no relevance
  filter, injects irrelevant facts.
- **Fix (not delete)** the `display_text` redaction so it is a record property,
  not a local (`mind.py:2897/3173/2046-2061` + `/api/v1/session`).

### Implementation issues (file these)
1. **Fix message_obi redaction leak** (precondition): persist `display_text` on
   the thought; read it at every shared/public sink; pin with a no-leak test.
2. **Add `pxh.people`**: deterministic `extract_person_facts` (report-kind,
   verbatim, subject-tagged) + `people_file` + `retrieve_person_facts` reusing
   `memory`'s scorer; unit tests for extraction precision and exclusions.
3. **Wire two write call sites** (obi-chat `obi` role; voice `spark`-persona user
   turn) and **two retrieval injections** (obi-chat prompt; voice prompt,
   spark-only) with provenance framing.
4. **Add `px-mind-consolidation` health component** + dashboard "memory last
   formed" line; diagnose the live `done:false` failure separately.
5. **Lived Day1/Day3 acceptance test** + deterministic CI test per §5.

### Falsification tests (this design is wrong if…)
- **Keyword retrieval is too weak:** Day-3 questions that a human sees as
  obviously related retrieve *zero* facts because they share no tokens with the
  stored verbatim (e.g. "what animal…" vs "I like dinosaurs"). If this fails
  often, the cheap deterministic retrieval is insufficient and embeddings are
  justified — but only *then*, proven by this failure, not assumed.
- **Deterministic extraction is too brittle:** real family speech rarely matches
  the assertion patterns, so the store stays near-empty after a week of use. If
  so, a *single* narrow LLM extraction pass (still writing `report` with the
  utterance as evidence) is the next step — not a platform.
- **Separation costs continuity:** because person facts never reach reflection,
  SPARK never *spontaneously* brings Obi up unprompted; if the family wants that,
  a deliberate, redaction-safe, coordinate-free bridge into reflection is a
  later, separate decision — not this change.
- **The store fills with noise:** if extraction captures commands/chatter despite
  the assertion filter, precision is too low; tighten patterns before widening.

### What NOT to build
- **No embeddings / vector store** until keyword retrieval is *measured*
  insufficient by the falsification test above.
- **No episodic/spatial memory subsystem, no "memory platform."**
- **No new LLM pipeline** for extraction; deterministic first, one narrow pass
  only if falsified.
- **No repurposing of `contextual_preference.py`** for recall.
- **No bridge from person facts into reflection / public thoughts / blog /
  Bluesky** — the firewall is the point.
- **No merging into `memories-spark.jsonl`** — physical file separation is the
  contamination guarantee.
