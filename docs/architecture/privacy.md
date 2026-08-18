# Privacy

**Owns:** what must never leave the robot, and the mechanisms that enforce it.
Location data, private messages to Obi, and untrusted chat text.

Everything here concerns a real child and a real household. These are not
hygiene rules.

---

## Invariant

### Location never reaches reflection, and therefore never reaches anything public

SPARK knows where people are (Google Find Hub trackers via `state/findmyhub.json`,
Home Assistant presence). That knowledge is available in **direct conversation
only** — *"where's dad?"* — and never in a thought.

This matters because thoughts are not private: `state/thoughts-spark.jsonl`
feeds `/api/v1/public/thoughts`, the public site feed, and Bluesky.

### The mechanism is an allowlist, not a denylist

`mind._REFLECTION_AWARENESS_KEYS` names the awareness keys **permitted** into
the reflection prompt's JSON dump. Everything else is dropped.

Deliberately absent, and each absence is load-bearing:

- `findmyhub` — raw tracker coordinates
- `ha_presence` — per-person latitude/longitude
- `health` — noise, not privacy, but still excluded

Presence reaches the prompt only through the coordinate-free *"who's home"*
prose.

**A new awareness key stays out of the prompt until someone adds it to the
allowlist. That default is the entire point.** Pinned by
`test_reflection_prompt_excludes_all_location_coordinates` and
`test_reflection_awareness_json_is_allowlisted`.

### Private messages to Obi are redacted before they are durable

The `message_obi` action lets SPARK initiate a direct message to Obi via the
dashboard. Thoughts carrying `action=message_obi` are written to
`state/thoughts-spark.jsonl` as the literal string `[private message to Obi]`.

The redaction happens **before the write**, not at the read side. A read-side
filter would mean the private text existed on disk in a file that several
public endpoints and `px-post` read, and any one of them forgetting the filter
would leak it.

Private audio for the same action uses the announce relay's `priv/` namespace
with a 3-minute TTL, against 7 days for public audio.

### User-supplied chat text is sanitised before storage or interpolation

`api._sanitize_chat_text()` strips `<`, `>`, newlines, carriage returns, and
NUL from all user-supplied chat text — applied to both public chat history and
obi-chat messages — **before** it is stored or interpolated into a prompt.

Sanitising before storage rather than before display means a later reader that
forgets to sanitise cannot resurrect the problem.

### Text SPARK did not write goes to the unprivileged session

`post_qa`, `public_chat`, and `obi_chat` are classified in `brain._IO_KINDS`
and route to `spark-io`, which runs outside the repository with one tool. See
[architecture/policy-and-authority](policy-and-authority.md) and
[architecture/resident-brain](resident-brain.md).

---

## Why it looks like this

*History, not rule.*

The allowlist replaced a denylist, and the denylist leaked. It read
`if k != "health"` — which passed everything else through, including
`findmyhub` tracker coordinates and `ha_presence` per-person latitude and
longitude. The house was published to five metres, in every reflection, twice
over, into a feed that goes to Bluesky.

The bug was not that someone forgot to add `findmyhub` to the denylist. The bug
was that a denylist makes *forgetting* the failure mode. An allowlist makes
*forgetting* mean the data is simply absent, which is the direction you want to
fail in when the data is a child's location.
