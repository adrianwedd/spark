# Memory and Learning

**Owns:** what SPARK keeps between turns and between days —
`src/pxh/memory.py`, `src/pxh/intention.py`,
`src/pxh/contextual_preference.py`, and the conversation buffer.

Where those records get their trust from is
[architecture/provenance](provenance.md).

---

## Invariant

### Four stores, four different lifetimes

| Store | File | Lifetime |
|---|---|---|
| Conversation buffer | `state/conversation-{persona}.jsonl` | rolling window, `PX_CONVERSATION_TURNS` (default 10) |
| Notes | `state/notes[-persona].jsonl` | append-only, durable |
| Consolidated memories | `state/memories-{persona}.jsonl` | append-only, distilled nightly |
| Active goal | `state/intention-{persona}.json` | one at a time, 7-day expiry |
| Contextual experience | persona-scoped experience store | append-only, never rewritten |

**Every store is persona-scoped** so GREMLIN, VIXEN and SPARK histories never
bleed into each other. A shared store would let a jailbroken persona's output
re-enter SPARK's cognition as if SPARK had thought it.

### The conversation buffer is short-term memory, and it is separate from state

Each turn appends to the buffer and is injected into the next prompt as a
"Recent conversation" section. This is what gives SPARK continuity across turns
without depending solely on file-injected session state. SPARK's own utterance
is the action's `params.text`, falling back to `(tool_name)` for non-speech
actions.

### Consolidation is nightly, capped, and metered

A Haiku pass between 02:00–06:00 Hobart, at most 2 attempts per day
(`state/consolidation_meta.json`), distils the last 24h of thoughts into
`state/memories-{persona}.jsonl`.

**Consolidation allowlists its input fields.** It does not copy arbitrary
thought keys forward, which is what stops a location-bearing field surviving
into a store that reflection reads. See
[architecture/privacy](privacy.md).

### Retrieval is by relevance, and never pads

Reflection retrieves the top-3 relevant memories by keyword/tag overlap. A
populated store with no relevant hit returns nothing rather than falling back
to recency or to raw notes — see
[architecture/provenance](provenance.md).

### Goals are singular and expire

`intention.py` holds one active goal per persona, with a 7-day expiry.
`set_goal` archives any previous active goal rather than overwriting it. A
robot with five simultaneous goals has none.

### Lived-experience adaptation is append-only and narrow

`contextual_preference.py` records one system-attributed experience per line
and **never updates an earlier line**. Corruption is reported without
rewriting the file. Adaptation is deliberately bounded to choosing between
options SPARK already had, not to inventing new behaviour.

`load_experiences()` reports corrupt lines rather than silently skipping them,
because a store that quietly drops what it cannot parse is a store whose size
tells you nothing.

---

## Why it looks like this

*History, not rule.*

Persona scoping came after cross-persona bleed: SPARK retrieved a GREMLIN note
and reflected on it in SPARK's voice.

The retrieval-never-pads rule came from an observed fixation. SPARK's thoughts
circled Obi far more than the topic seeds explained, and the cause turned out
to be retrieval bias rather than the seeds — the memory query was built almost
entirely from human-presence signals, so nearly every query matched
human-presence memories, and padding with recent records made it worse.
