"""Facts a person literally stated about themselves — the writer half of the
person-memory design (`docs/specs/2026-08-25-person-memory-minimal-design.md`).

Store: ``state/people-{persona}.jsonl``, one record per line, shape-compatible
with ``memory.py``'s records so the existing relevance scorer can read it
unchanged when retrieval lands (step 4; this module deliberately ships without
an injection path).

**A separate file is the contamination firewall, and it is the whole point.**
Reflection reads ``memories-{persona}.jsonl`` and its output flows to
``thoughts-spark.jsonl`` → ``/api/v1/public/thoughts`` → the site feed, the blog
and Bluesky. A person fact that reached reflection would reach all of those. It
cannot, because reflection never opens this file — the same
allowlist-by-construction discipline as ``mind._REFLECTION_AWARENESS_KEYS``,
except here the allowlist is the filesystem. Pinned by
``tests/test_people_invariants.py``; do not add a bridge.

**There is no model in the write path.** Extraction is regex over sentences, so
a fact exists only if a human sentence asserted it — the strongest available
form of "no model-guessed facts". The provenance kind is hardcoded ``report``
(ceiling 0.9): SPARK was told this, it did not see it and did not work it out.

**Evidence is the matched clause, never the whole utterance.** Faithful
provenance does not require copying the message: "I'm sad about school today,
but I really like dinosaurs" must put *only* the dinosaur clause into this
store, or a benign match drags unrelated private material into a file whose
whole reason to exist is narrow scope. The record carries the exact asserted
span plus the source message id; the full original stays in its source log
(``obi_chat.jsonl``, the conversation buffer), recoverable by that id. Voice
turns have no ids, so their reference is a content hash of the utterance —
a deliberate fallback, not the normal path: a channel that has real event
identity (obi-chat does) must thread it.

**The matcher is biased to rejection, on purpose.** It captures three kinds and
nothing else — stable preferences, stated relationships, and explicit
first-person commitments. Better to remember too little than to confidently
fossilise chatter: a missed fact costs one turn of continuity, a fabricated one
is a robot telling a child something they never said. Questions, hypotheticals,
reported speech, second/third-person statements, commands and deictic
("this song") objects are refused outright. ``tests/test_people.py`` carries the
false-positive corpus; widen the patterns only against it.

**Commitments carry an aggressive TTL** because a stale promise recalled as
current is worse than forgetting it. Expiry is filtered at *read* time by
``read_people()``; nothing is ever deleted, matching ``provenance``'s
supersession-not-deletion posture. Corrections use ``provenance.supersedes``:
a new statement of the same ``(subject, fact_kind, topic)`` supersedes the last
one rather than duplicating it, so "my best friend is Sam" then "my best friend
is Mia" holds both records and surfaces one.

**GREMLIN and VIXEN never write here.** ``record_person_facts`` is a no-op for
any persona other than SPARK (an empty persona string *is* SPARK — see
``voice_loop``). The performance characters do not get Obi's facts, and a
per-persona filename alone would not have stopped them acquiring their own.

Reporting never raises into its caller, for ``health.py``'s reason: a memory
writer must not be able to kill the voice loop or the API request it hangs off.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from filelock import FileLock

from pxh import memory, provenance
from pxh.state import atomic_write
from pxh.time import utc_timestamp

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
HOBART_TZ = ZoneInfo("Australia/Hobart")

FACT_KINDS = ("preference", "relationship", "commitment")
WRITER_PERSONAS = ("spark",)
DEFAULT_SUBJECT = "obi"
SPEAKER_ROLES = ("obi", "user")

PEOPLE_LIMIT = 2000
LOCK_TIMEOUT_S = 10
MAX_FACT_CHARS = 120
IMPORTANCE = 0.5

# Days, not weeks. A commitment recalled after its horizon is a robot insisting
# on a plan that already happened, so the default is short and the ceiling for
# an explicitly dated one is still inside a fortnight.
COMMITMENT_TTL_DAYS = 3
COMMITMENT_MAX_TTL_DAYS = 10

# --- rejection ------------------------------------------------------------
# Any sentence containing one of these is dropped whole, before matching.
# Hedges and conditionals ("if I had a dog", "I would never") are not assertions;
# reported speech ("my friend said her favourite is cats") is someone else's
# claim wearing a first-person sentence; interrogatives are requests, not facts.
_REJECT_SENTENCE = re.compile(
    r"\b(?:if|would|wouldn't|could|should|might|may|maybe|perhaps|unless|"
    r"suppose|imagine|pretend|wish|wishing|whenever|almost|nearly|"
    r"said|say|says|saying|told|tells|telling|think|thinks|thought|reckon|"
    r"reckons|ask|asks|asked|wonder|wondering|guess|"
    r"what|when|where|which|who|whose|why|how|do you|did you|are you|"
    r"can you|will you|have you)\b")

# Deictic objects have no referent outside the moment ("I like this song"), so
# the fact would be unretrievable and, worse, wrong the next time it matched.
_DEICTIC_HEAD = re.compile(r"^(?:this|that|these|those|it|them|him|her|you|your)\b")

# A commitment needs something concrete to be about. "I'm going to explode" is
# a mood; hyperbole that survives the referent test is refused by name.
_COMMIT_REFERENT = re.compile(r"\b(?:the|a|an|my|our|his|her|their)\s+[a-z]")
_HYPERBOLE = re.compile(
    r"\b(?:explode|exploding|die|dying|kill|murder|scream|screaming|faint|melt|"
    r"burst|vomit|throw up|be sick|cry forever|lose my mind|never speak)\b")
_NEGATED_INTENT = re.compile(r"^(?:not|never|no)\b")

_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
             "friday": 4, "saturday": 5, "sunday": 6}
_TEMPORAL = re.compile(
    r"\b(?:today|tonight|tomorrow|this (?:week|weekend|afternoon|evening|arvo)|"
    r"next (?:week|weekend)|" + "|".join(_WEEKDAYS) + r")\b")

_RELATIONS = (r"best friend|friend|mum|mom|mother|dad|father|brother|sister|"
              r"teacher|grandma|grandpa|nan|pop|cousin|coach")

# A relationship fact names a person. "my friend is really nice" is a passing
# opinion wearing the same grammar, so the other half of the clause has to look
# like a name — at most three words, none of them a description.
_NAME_LIKE = re.compile(r"^[a-z][a-z'-]*(?: [a-z][a-z'-]*){0,2}$")
_NOT_A_NAME = re.compile(
    r"\b(?:really|very|so|quite|pretty|too|the|a|an|nice|kind|cool|funny|good|"
    r"great|best|mean|old|young|new|coming|here|there|away|back|sick|happy|sad|"
    r"busy|angry|tired|late|early|right|wrong|annoying|silly|weird|boring|"
    r"stupid|dumb|gross|loud|crazy|scary|naughty|rude|smelly|awesome|amazing|"
    r"terrible|horrible)\b")

# --- capture --------------------------------------------------------------
_PREF_LIKE = re.compile(
    r"^i (?:really |quite |always )?(?:like|love|adore|enjoy|prefer) (?P<obj>.+)$")
_PREF_DISLIKE = re.compile(
    r"^i (?:really |quite |always )?(?:hate|dislike|don't like|do not like|"
    r"don't enjoy|can't stand|cannot stand) (?P<obj>.+)$")
_PREF_FAV = re.compile(
    r"^my (?:favourite|favorite) (?P<topic>[a-z ]{2,30}?) is (?P<obj>.+)$")
_REL_MINE = re.compile(rf"^my (?P<rel>{_RELATIONS}) is (?P<obj>.+)$")
_REL_THEIRS = re.compile(rf"^(?P<obj>[a-z][a-z'. -]{{1,30}}?) is my (?P<rel>{_RELATIONS})$")
_COMMIT_GOING = re.compile(r"^(?:i|we)(?:'m| am|'re| are) going to (?P<obj>.+)$")
_COMMIT_WILL = re.compile(r"^(?:i|we)(?:'ll| will) (?P<obj>.+)$")
_COMMIT_PROMISED = re.compile(
    r"^i promised [a-z' -]{1,20} (?:i'd|i would|i will|to) (?P<obj>.+)$")


def _state_dir() -> Path:
    return Path(os.environ.get("PX_STATE_DIR", PROJECT_ROOT / "state"))


def normalize_persona(persona: str | None) -> str:
    """An empty persona is SPARK — `voice_loop` stores "" for the default."""
    slug = re.sub(r"[^a-z0-9_-]", "", (persona or "").lower().strip())
    return slug or "spark"


def people_file(persona: str = "spark") -> Path:
    return _state_dir() / f"people-{normalize_persona(persona)}.jsonl"


# A conjunction followed by a first-person restart is a clause boundary even
# without punctuation ("I like dinosaurs but I hate broccoli"), while "fish and
# chips" stays whole because "chips" is not a restart. Leading conjunctions are
# shed so "…, but I really like dinosaurs" still hits the ^i anchor.
_CONJ_SPLIT = re.compile(r"\s+(?:but|and|so|then)\s+(?=(?:i|we|my)\b)",
                         re.IGNORECASE)
_LEAD_CONJ = re.compile(r"^(?:but|and|so|then|because|cause|cos)\s+",
                        re.IGNORECASE)


def _clauses(text: str) -> list[str]:
    """Sentence/clause split. Commas are cut points too: a compound utterance
    would otherwise hand a pattern an object that swallows the rest of it."""
    out: list[str] = []
    for chunk in re.split(r"[.!?;\n,]+", str(text or "")):
        for piece in _CONJ_SPLIT.split(chunk):
            piece = _LEAD_CONJ.sub("", piece).strip()
            if piece:
                out.append(piece)
    return out


def _norm(value: str) -> str:
    text = str(value or "").replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", text).strip(" \t'\"-")


def _commitment_expiry(obj: str, now: dt.datetime) -> str:
    """Expiry for a commitment, derived from an explicit day when one is named.

    Hobart, never UTC: "Saturday" means the family's Saturday. The horizon is
    the end of the named day plus a day of grace, capped — an undated promise
    gets the short default rather than an open-ended one.
    """
    local = now.astimezone(HOBART_TZ)
    days = COMMITMENT_TTL_DAYS
    if re.search(r"\b(?:today|tonight|this (?:afternoon|evening|arvo))\b", obj):
        days = 1
    elif "tomorrow" in obj:
        days = 2
    elif re.search(r"\bnext (?:week|weekend)\b", obj):
        days = COMMITMENT_MAX_TTL_DAYS
    else:
        for name, index in _WEEKDAYS.items():
            if re.search(rf"\b{name}\b", obj):
                ahead = (index - local.weekday()) % 7 or 7
                days = min(ahead + 1, COMMITMENT_MAX_TTL_DAYS)
                break
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    horizon = midnight + dt.timedelta(days=days)
    return horizon.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _match(low: str) -> tuple[str, str, str] | None:
    """Return (fact_kind, topic, polarity) for a lowercased clause, else None.

    Every branch that cannot prove what it matched returns None. This function
    is the whole precision budget of the feature.
    """
    if _REJECT_SENTENCE.search(low):
        return None
    m = _PREF_FAV.match(low)
    if m:
        return ("preference", f"favourite:{_norm(m.group('topic'))}", "like")
    for pattern, polarity in ((_PREF_LIKE, "like"), (_PREF_DISLIKE, "dislike")):
        m = pattern.match(low)
        if m:
            obj = _norm(m.group("obj"))
            if _DEICTIC_HEAD.match(obj) or len(obj) < 3:
                return None
            return ("preference", f"preference:{obj}", polarity)
    for pattern in (_REL_MINE, _REL_THEIRS):
        m = pattern.match(low)
        if m:
            obj = _norm(m.group("obj"))
            if not _NAME_LIKE.match(obj) or _NOT_A_NAME.search(obj):
                return None
            return ("relationship", f"relation:{_norm(m.group('rel'))}", "")
    for pattern in (_COMMIT_GOING, _COMMIT_WILL, _COMMIT_PROMISED):
        m = pattern.match(low)
        if m:
            obj = _norm(m.group("obj"))
            if _NEGATED_INTENT.match(obj) or _HYPERBOLE.search(obj):
                return None
            if not (_COMMIT_REFERENT.search(obj) or _TEMPORAL.search(obj)):
                return None
            return ("commitment", f"commitment:{obj}", "")
    return None


def extract_person_facts(*, role: str, text: str, subject: str = DEFAULT_SUBJECT,
                         ts: str | None = None, msg_id: str | None = None,
                         channel: str = "conversation",
                         now: dt.datetime | None = None) -> list[dict]:
    """Deterministically extract stated facts from one person's utterance.

    Strict about who is speaking: SPARK's own replies are not facts about Obi,
    so a role outside `SPEAKER_ROLES` yields nothing.
    """
    if str(role or "").lower().strip() not in SPEAKER_ROLES:
        return []
    verbatim = _norm(text)
    if not verbatim:
        return []
    when = now or dt.datetime.now(dt.timezone.utc)
    stamp_ts = ts or utc_timestamp()
    ref = msg_id or f"turn:{hashlib.sha1(verbatim.encode('utf-8')).hexdigest()[:12]}"
    out: list[dict] = []
    seen: set[str] = set()
    for clause in _clauses(verbatim):
        low = clause.lower()
        hit = _match(low)
        if hit is None:
            continue
        fact_kind, topic, polarity = hit
        if topic in seen or len(clause) > MAX_FACT_CHARS:
            continue
        seen.add(topic)
        record = {
            "ts": stamp_ts,
            "subject": str(subject or DEFAULT_SUBJECT).lower().strip(),
            "fact_kind": fact_kind,
            "topic": topic,
            "polarity": polarity,
            "text": clause,
            "tags": sorted(memory._tokenize(clause)),
            "importance": IMPORTANCE,
            "source": "conversation",
            "expires_ts": _commitment_expiry(low, when) if fact_kind == "commitment" else None,
        }
        # The kind is a literal here and nowhere else: no caller and no model
        # gets to choose it, which is what makes this a `report` store by
        # construction rather than by convention. Evidence is the matched
        # clause, not `verbatim`: the rest of the utterance is not this fact's
        # business, and the id already names the full source message.
        provenance.stamp(record, "report", "conversation",
                         evidence=[f"{channel}:{ref}", clause])
        out.append(record)
    return out


def load_people(persona: str = "spark") -> list[dict]:
    f = people_file(persona)
    if not f.exists():
        return []
    out: list[dict] = []
    try:
        for line in f.read_text(encoding="utf-8").strip().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and rec.get("text"):
                out.append(rec)
    except (OSError, UnicodeDecodeError):
        return []
    return out


def is_expired(record: dict, now: dt.datetime | None = None) -> bool:
    """Lenient, like `provenance.read_provenance`: an unparseable expiry is not
    an expiry. A corrupt line must not silently retire a fact."""
    raw = (record or {}).get("expires_ts")
    if not raw:
        return False
    try:
        when = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return (now or dt.datetime.now(dt.timezone.utc)) >= when


def read_people(persona: str = "spark", subject: str | None = None,
                now: dt.datetime | None = None) -> list[dict]:
    """Live facts: expiry filtered at read time, supersession annotated.

    Expired commitments are excluded, never deleted — the record stays on disk
    so "you said you were going to the fair" remains answerable, it just stops
    being surfaced as current. Superseded records are returned carrying
    `superseded_by` so a caller can show the correction; retrieval drops them.
    """
    records = provenance.apply_supersessions(load_people(persona))
    want = str(subject).lower().strip() if subject else None
    return [r for r in records
            if not is_expired(r, now)
            and (want is None or str(r.get("subject", "")).lower() == want)]


def append_person_facts(records: list[dict], persona: str = "spark",
                        now: dt.datetime | None = None) -> list[dict]:
    """Append, marking each new fact as superseding the last live one on the
    same (subject, fact_kind, topic). Correction without deletion."""
    if not records:
        return []
    live = read_people(persona, now=now)
    latest: dict[tuple, dict] = {}
    for rec in live:
        if not provenance.is_superseded(rec):
            latest[(rec.get("subject"), rec.get("fact_kind"), rec.get("topic"))] = rec
    for rec in records:
        prior = latest.get((rec.get("subject"), rec.get("fact_kind"), rec.get("topic")))
        if prior:
            provenance.mark_supersedes(rec, prior)
    f = people_file(persona)
    f.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(f) + ".lock", timeout=LOCK_TIMEOUT_S):
        with f.open("a", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        try:
            lines = f.read_text(encoding="utf-8").strip().splitlines()
            if len(lines) > PEOPLE_LIMIT:
                atomic_write(f, "\n".join(lines[-PEOPLE_LIMIT:]) + "\n")
        except OSError:
            pass
    return records


def record_person_facts(*, role: str, text: str, persona: str = "spark",
                        subject: str = DEFAULT_SUBJECT, ts: str | None = None,
                        msg_id: str | None = None,
                        channel: str = "conversation") -> int:
    """The one-line call-site hook. Never raises; returns how many facts landed.

    GREMLIN and VIXEN are refused here rather than at the filename: a
    per-persona file would have given the performance characters their own
    person store, which is not a firewall, it is two stores.
    """
    try:
        if normalize_persona(persona) not in WRITER_PERSONAS:
            return 0
        facts = extract_person_facts(role=role, text=text, subject=subject,
                                     ts=ts, msg_id=msg_id, channel=channel)
        append_person_facts(facts, persona="spark")
        return len(facts)
    except Exception as exc:  # broad: a memory writer must not kill its caller
        print(f"[people] extraction failed: {exc}", file=sys.stderr)
        return 0


# --- operator seeding ------------------------------------------------------
SEED_SOURCE = "operator_seed"


def build_seed_record(*, polarity: str, obj: str, actor: str,
                      subject: str = DEFAULT_SUBJECT,
                      expires_ts: str | None = None,
                      ts: str | None = None) -> dict:
    """One operator-asserted preference record, for `bin/px-person-seed`.

    Same shape the extractor produces, so retrieval and supersession treat
    both uniformly — with one deliberate difference: **the record says the
    operator asserted it, never that Obi did.** ``source`` and the provenance
    source are ``operator_seed`` and ``source_actor`` names who typed it, so
    a renderer that says "Obi told me" about a seed has to ignore the record,
    not misread it. Evidence is the operator's assertion, which forges no
    conversation history: there is no message id because there was no message.

    Preferences only. The seed path exists so known stable interests can be
    present before the first conversation; relationships and commitments are
    exactly the kinds that should only enter through Obi's own words.

    Unlike the conversational writer this *does* raise on bad input — an
    operator at a terminal is the one caller who should see the error.
    """
    if polarity not in ("like", "dislike"):
        raise ValueError(f"polarity must be like|dislike, got {polarity!r}")
    topic_obj = _norm(obj).lower()
    if not (3 <= len(topic_obj) <= MAX_FACT_CHARS):
        raise ValueError(f"seed object must be 3-{MAX_FACT_CHARS} chars: {obj!r}")
    if _DEICTIC_HEAD.match(topic_obj):
        raise ValueError(f"seed object has no stable referent: {obj!r}")
    actor_slug = re.sub(r"[^a-z0-9_-]", "", str(actor or "").lower().strip())
    if not actor_slug:
        raise ValueError("a seed must name its operator (--by)")
    if expires_ts:
        dt.datetime.fromisoformat(str(expires_ts).replace("Z", "+00:00"))
    text = f"{'likes' if polarity == 'like' else 'dislikes'} {topic_obj}"
    record = {
        "ts": ts or utc_timestamp(),
        "subject": str(subject or DEFAULT_SUBJECT).lower().strip(),
        "fact_kind": "preference",
        "topic": f"preference:{topic_obj}",
        "polarity": polarity,
        "text": text,
        "tags": sorted(memory._tokenize(text)),
        "importance": IMPORTANCE,
        "source": SEED_SOURCE,
        "source_actor": actor_slug,
        "expires_ts": expires_ts or None,
    }
    provenance.stamp(record, "report", SEED_SOURCE,
                     evidence=[f"operator:{actor_slug}", text])
    return record


# --- retrieval -------------------------------------------------------------
# Exactly two prompts may include this context: the SPARK voice prompt and the
# obi-chat prompt. Never GREMLIN/VIXEN, never reflection/public chat/blog/
# social — pinned by `tests/test_people_invariants.py`, which allowlists the
# call sites the way it already allowlists the writers.

RETRIEVAL_LIMIT = 3


def _age_phrase(ts: str, now: dt.datetime) -> str:
    try:
        when = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    days = (now.astimezone(HOBART_TZ).date() - when.astimezone(HOBART_TZ).date()).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def render_person_fact(record: dict, now: dt.datetime | None = None) -> str:
    """One compact line, attribution first. An operator seed renders as the
    operator's claim ("adrian told you that obi likes dinosaurs"), never as
    something the subject said — the record's `source` decides, so no caller
    can misattribute a seed by accident."""
    when = now or dt.datetime.now(dt.timezone.utc)
    subject = str(record.get("subject") or DEFAULT_SUBJECT).capitalize()
    text = str(record.get("text") or "").strip()
    age = _age_phrase(str(record.get("ts") or ""), when)
    if record.get("source") == SEED_SOURCE:
        actor = str(record.get("source_actor") or "someone").capitalize()
        return f"{actor} told you that {subject} {text}."
    aged = f" ({age})" if age else ""
    return f'{subject} told you{aged}: "{text}"'


def person_context(query: str, persona: str = "spark",
                   subject: str = DEFAULT_SUBJECT,
                   limit: int = RETRIEVAL_LIMIT,
                   now: dt.datetime | None = None) -> str:
    """Relevant live person facts for a prompt, or "" — never padding.

    Relevance is tag overlap with the query, the same tokenizer the records
    were tagged with at write time. No overlap means no injection: an empty
    string is the correct answer to an unrelated question, because padded
    "context" is how a fact store turns into a script. Superseded and expired
    records never surface. Only SPARK gets this context; any other persona
    gets "" regardless of what the caller intended. Never raises — this sits
    inside the voice loop and an API request handler.
    """
    try:
        if normalize_persona(persona) not in WRITER_PERSONAS:
            return ""
        q = memory._tokenize(query)
        if not q:
            return ""
        when = now or dt.datetime.now(dt.timezone.utc)
        scored = []
        for rec in read_people("spark", subject=subject, now=when):
            if provenance.is_superseded(rec):
                continue
            score = len(q & set(rec.get("tags") or ()))
            if score > 0:
                scored.append((score, str(rec.get("ts") or ""), rec))
        scored.sort(key=lambda item: item[1], reverse=True)  # newest first…
        scored.sort(key=lambda item: item[0], reverse=True)  # …then best match
        top = [rec for _, _, rec in scored[:max(1, limit)]]
        return "\n".join(render_person_fact(rec, when) for rec in top)
    except Exception as exc:  # broad for the writer's reason: never kill a caller
        print(f"[people] retrieval failed: {exc}", file=sys.stderr)
        return ""
