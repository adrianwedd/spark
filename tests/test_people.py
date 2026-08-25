"""Tests for pxh.people — the deterministic person-fact writer.

The false-positive corpus below is the load-bearing half. Recall can be widened
later against real family speech; precision cannot be recovered once SPARK has
told a child something they never said. Each rejection case names the trap it
represents, so a future widening that breaks one has to argue with the trap
rather than with a bare assertion.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from pxh import people, provenance

NOW = dt.datetime(2026, 8, 25, 9, 0, tzinfo=dt.timezone.utc)  # Tuesday, Hobart


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))


def _facts(text, role="obi", **kw):
    return people.extract_person_facts(role=role, text=text, msg_id="m1",
                                       ts="2026-08-25T09:00:00Z", now=NOW, **kw)


# ── What is captured ───────────────────────────────────────────────────────

CAPTURE = [
    ("my favourite animal is the cuttlefish", "preference", "favourite:animal"),
    ("my favorite colour is orange", "preference", "favourite:colour"),
    ("I like dinosaurs", "preference", "preference:dinosaurs"),
    ("I love the ocean", "preference", "preference:the ocean"),
    ("I hate broccoli", "preference", "preference:broccoli"),
    ("I don't like mushrooms", "preference", "preference:mushrooms"),
    ("Mia is my best friend", "relationship", "relation:best friend"),
    ("Mr Tan is my teacher", "relationship", "relation:teacher"),
    ("my best friend is Sam", "relationship", "relation:best friend"),
    ("I'm going to the school fair on Saturday", "commitment", None),
    ("we're going to the beach on Saturday", "commitment", None),
    ("I promised Dad I'd feed the cat", "commitment", None),
    ("I'll show you the drawing tomorrow", "commitment", None),
]


@pytest.mark.parametrize("text,kind,topic", CAPTURE)
def test_captures_the_three_fact_kinds(text, kind, topic):
    got = _facts(text)
    assert len(got) == 1, f"{text!r} produced {got}"
    assert got[0]["fact_kind"] == kind
    if topic:
        assert got[0]["topic"] == topic


def test_polarity_distinguishes_like_from_dislike():
    assert _facts("I like broccoli")[0]["polarity"] == "like"
    assert _facts("I hate broccoli")[0]["polarity"] == "dislike"


# ── What must never be captured ────────────────────────────────────────────

REJECT = [
    ("do you like dogs?", "question — a request for SPARK's view, not an assertion"),
    ("what is your favourite animal?", "question wearing the favourite-X grammar"),
    ("who is your best friend", "interrogative without a question mark"),
    ("if I had a dog I would call him Rex", "conditional — a fact about an imagined world"),
    ("I would never eat broccoli", "hypothetical 'would never', not the flat 'I never'"),
    ("maybe I like dinosaurs", "hedge — the speaker has not committed to it"),
    ("I think I like dogs", "hedge verb wrapping a well-formed preference"),
    ("I might go to the fair on Saturday", "modal — an option, not a commitment"),
    ("I'm going to explode", "hyperbole with future-intent grammar"),
    ("I'm going to die of boredom", "hyperbole that also carries a temporal-ish object"),
    ("my friend said her favourite is cats", "reported speech — someone else's claim"),
    ("Dad told me he hates mornings", "reported speech about a third party"),
    ("you like trains", "second person — a claim about SPARK, not the speaker"),
    ("Dad hates mornings", "third person — not the speaker's own stated fact"),
    ("Sam is my friend's brother", "possessive chain — a relation of a relation, not of the speaker"),
    ("go forward", "command to the robot"),
    ("say something about dogs", "request for speech, not a stated fact"),
    ("turn left and then stop", "compound command"),
    ("I'm hungry", "transient bodily state"),
    ("I'm tired today", "transient state with a temporal marker"),
    ("I like this song", "deictic object — no referent survives the moment"),
    ("I love that", "bare deictic object"),
    ("I'll be back", "future intent with no concrete referent"),
    ("I will not go to the fair", "negated intent"),
    ("my friend is really nice", "passing opinion wearing relationship grammar"),
    ("my brother is annoying", "adjective wearing name grammar — an opinion, not a name"),
    ("I wish I liked broccoli", "counterfactual wish"),
    ("I could eat the whole cake", "modal, not an intention"),
    ("", "empty utterance"),
    ("   ", "whitespace-only utterance"),
]


@pytest.mark.parametrize("text,trap", REJECT)
def test_false_positive_corpus_produces_nothing(text, trap):
    assert _facts(text) == [], trap


def test_sparks_own_reply_is_never_a_fact_about_obi():
    assert people.extract_person_facts(role="spark", text="I like dinosaurs",
                                       msg_id="m1", now=NOW) == []


def test_compound_utterance_does_not_swallow_the_rest_of_the_sentence():
    got = _facts("I like dinosaurs, my best friend is Sam")
    assert {f["fact_kind"] for f in got} == {"preference", "relationship"}
    assert all(len(f["text"]) < 40 for f in got)


def test_conjunction_restart_is_a_clause_boundary_but_objects_stay_whole():
    got = _facts("I like dinosaurs but I hate broccoli")
    assert [(f["polarity"], f["topic"]) for f in got] == [
        ("like", "preference:dinosaurs"), ("dislike", "preference:broccoli")]
    # "chips" is not a first-person restart, so the object survives intact.
    assert _facts("I like fish and chips")[0]["topic"] == "preference:fish and chips"


def test_evidence_is_the_matched_clause_never_the_whole_utterance():
    """Pinned by review (2026-08-25): a benign match must not drag unrelated
    private material into the store. The full message stays in its source log,
    recoverable via the evidence message id — data minimisation and provenance
    at the same time."""
    got = _facts("I'm sad about school today, but I really like dinosaurs.")
    assert len(got) == 1
    assert got[0]["topic"] == "preference:dinosaurs"
    people.append_person_facts(got, now=NOW)
    raw = people.people_file().read_text(encoding="utf-8")
    assert "dinosaurs" in raw
    assert "sad" not in raw and "school" not in raw


# ── Record shape and provenance ────────────────────────────────────────────

def test_record_is_a_report_claim_with_traceable_evidence():
    rec = _facts("I like dinosaurs", subject="obi")[0]
    prov = provenance.read_provenance(rec)
    assert prov["kind"] == "report"
    assert prov["confidence"] <= provenance.CONFIDENCE_CEILING["report"]
    assert "obi_chat:m1" in prov["evidence"] or "conversation:m1" in prov["evidence"]
    assert "I like dinosaurs" in prov["evidence"]
    assert rec["subject"] == "obi"
    assert rec["id"] and rec["ts"].endswith("Z")
    assert rec["source"] == "conversation"
    assert "dinosaurs" in rec["tags"]


def test_kind_is_hardcoded_and_not_a_caller_choice():
    """No parameter of the writer can produce anything but `report` — the
    provenance kind is a literal, so no caller and no model can raise it."""
    import inspect
    assert '"report"' in inspect.getsource(people.extract_person_facts)
    params = inspect.signature(people.extract_person_facts).parameters
    assert not any("kind" in p for p in params if p != "channel")
    params = inspect.signature(people.record_person_facts).parameters
    assert not any("kind" in p for p in params)


def test_voice_turn_without_a_message_id_still_gets_a_turn_reference():
    rec = people.extract_person_facts(role="user", text="I like dinosaurs",
                                      channel="voice", now=NOW)[0]
    refs = provenance.read_provenance(rec)["evidence"]
    assert any(r.startswith("voice:turn:") for r in refs), refs


# ── Commitment TTL ─────────────────────────────────────────────────────────

def _expiry(text):
    return _facts(text)[0]["expires_ts"]


def test_preferences_and_relationships_never_expire():
    assert _facts("I like dinosaurs")[0]["expires_ts"] is None
    assert _facts("Mia is my best friend")[0]["expires_ts"] is None


def test_commitment_ttl_is_days_not_weeks():
    horizon = dt.datetime.fromisoformat(
        _expiry("I'm going to feed the cat").replace("Z", "+00:00"))
    assert 0 < (horizon - NOW).days <= people.COMMITMENT_MAX_TTL_DAYS


def test_named_day_tightens_the_ttl_below_the_default():
    tomorrow = dt.datetime.fromisoformat(
        _expiry("I'll show you the drawing tomorrow").replace("Z", "+00:00"))
    default = dt.datetime.fromisoformat(
        _expiry("I'm going to feed the cat").replace("Z", "+00:00"))
    assert tomorrow < default


def test_expired_commitment_is_filtered_at_read_time_but_kept_on_disk():
    people.append_person_facts(_facts("I'm going to the fair tomorrow"), now=NOW)
    later = NOW + dt.timedelta(days=30)
    assert people.read_people(now=later) == []
    assert len(people.load_people()) == 1  # history, not deletion


def test_unparseable_expiry_reads_as_live():
    """Lenient reads: a corrupt line must not silently retire a fact."""
    assert people.is_expired({"expires_ts": "not-a-date"}, now=NOW) is False


# ── Supersession, not duplication ──────────────────────────────────────────

def test_new_statement_supersedes_the_prior_one_on_the_same_topic():
    people.append_person_facts(_facts("my best friend is Sam"), now=NOW)
    people.append_person_facts(_facts("my best friend is Mia"), now=NOW)
    stored = people.read_people(now=NOW)
    assert len(stored) == 2  # both kept
    live = [r for r in stored if not provenance.is_superseded(r)]
    assert [r["text"] for r in live] == ["my best friend is Mia"]


def test_a_correction_flips_polarity_without_deleting_the_old_belief():
    people.append_person_facts(_facts("I like broccoli"), now=NOW)
    people.append_person_facts(_facts("I hate broccoli"), now=NOW)
    live = [r for r in people.read_people(now=NOW)
            if not provenance.is_superseded(r)]
    assert [r["polarity"] for r in live] == ["dislike"]


def test_unrelated_topics_do_not_supersede_each_other():
    people.append_person_facts(_facts("I like dinosaurs"), now=NOW)
    people.append_person_facts(_facts("I like cuttlefish"), now=NOW)
    live = [r for r in people.read_people(now=NOW)
            if not provenance.is_superseded(r)]
    assert len(live) == 2


# ── Store behaviour ────────────────────────────────────────────────────────

def test_store_is_a_separate_file_from_consolidated_memory():
    from pxh import memory
    assert people.people_file("spark") != memory.memories_file("spark")
    assert people.people_file("spark").name == "people-spark.jsonl"


def test_load_skips_malformed_lines():
    f = people.people_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(_facts("I like dinosaurs")[0]) + "\n{broken\n",
                 encoding="utf-8")
    assert len(people.load_people()) == 1


def test_read_can_filter_by_subject():
    people.append_person_facts(_facts("I like dinosaurs", subject="obi"), now=NOW)
    people.append_person_facts(_facts("I like coffee", subject="adrian"), now=NOW)
    assert len(people.read_people(subject="obi", now=NOW)) == 1


# ── Persona firewall ───────────────────────────────────────────────────────

@pytest.mark.parametrize("persona", ["gremlin", "vixen", "GREMLIN"])
def test_personas_never_get_a_person_store(tmp_path, persona):
    assert people.record_person_facts(role="user", text="I like dinosaurs",
                                      persona=persona) == 0
    assert not list(tmp_path.glob("people-*.jsonl"))


def test_empty_persona_is_spark_because_voice_loop_stores_it_that_way():
    assert people.normalize_persona("") == "spark"
    assert people.record_person_facts(role="user", text="I like dinosaurs",
                                      persona="") == 1
    assert people.people_file("spark").exists()


def test_a_persona_slug_cannot_escape_the_state_dir():
    assert people.people_file("../../etc/passwd").name == "people-etcpasswd.jsonl"


# ── Failure posture ────────────────────────────────────────────────────────

def test_writer_never_raises_into_its_caller(monkeypatch, capsys):
    monkeypatch.setattr(people, "append_person_facts",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    assert people.record_person_facts(role="obi", text="I like dinosaurs") == 0
    assert "people" in capsys.readouterr().err


# ── Call-site wiring ───────────────────────────────────────────────────────

def test_voice_loop_records_the_user_turn_for_the_spark_persona(monkeypatch):
    from pxh import voice_loop
    monkeypatch.setenv("PX_CONVERSATION_TURNS", "2")
    voice_loop.record_conversation_turn("spark", "I like dinosaurs", "nice")
    live = people.read_people(now=NOW)
    assert [r["text"] for r in live] == ["I like dinosaurs"]


def test_voice_loop_does_not_record_sparks_own_reply(monkeypatch):
    from pxh import voice_loop
    voice_loop.record_conversation_turn("spark", "hello", "I like dinosaurs")
    assert people.read_people(now=NOW) == []


def test_voice_loop_records_nothing_under_a_persona():
    from pxh import voice_loop
    voice_loop.record_conversation_turn("vixen", "I like dinosaurs", "hi")
    assert people.load_people("spark") == []
    assert people.load_people("vixen") == []


def test_obi_chat_append_records_obi_messages_only(tmp_path, monkeypatch):
    from pxh import api
    monkeypatch.setattr(api, "_public_state_dir", lambda: tmp_path)
    api._append_obi_chat_api({"id": "abc123", "ts": "2026-08-25T09:00:00Z",
                              "role": "obi", "text": "I like dinosaurs"})
    api._append_obi_chat_api({"id": "def456", "ts": "2026-08-25T09:00:01Z",
                              "role": "spark", "text": "I like cuttlefish"})
    live = people.read_people(now=NOW)
    assert [r["text"] for r in live] == ["I like dinosaurs"]
    assert "obi_chat:abc123" in provenance.read_provenance(live[0])["evidence"]


# ── Operator seeding (px-person-seed) ──────────────────────────────────────

def test_seed_record_names_the_operator_never_obi():
    """The core stage-2 requirement: a seed must be structurally incapable of
    rendering as "Obi told me". Attribution lives in the record itself."""
    rec = people.build_seed_record(polarity="like", obj="Dinosaurs",
                                   actor="Adrian")
    assert rec["source"] == people.SEED_SOURCE == "operator_seed"
    assert rec["source_actor"] == "adrian"
    assert rec["topic"] == "preference:dinosaurs"
    assert rec["text"] == "likes dinosaurs"
    prov = provenance.read_provenance(rec)
    assert prov["kind"] == "report"
    assert prov["source"] == "operator_seed"
    assert "operator:adrian" in prov["evidence"]
    assert prov["confidence"] <= provenance.CONFIDENCE_CEILING["report"]
    # No forged conversation history: no message reference of any channel.
    raw = json.dumps(rec)
    assert "conversation" not in raw and "obi_chat" not in raw \
        and "turn:" not in raw


@pytest.mark.parametrize("kwargs,why", [
    (dict(polarity="favourite", obj="dinosaurs", actor="adrian"),
     "unknown polarity"),
    (dict(polarity="like", obj="dinosaurs", actor=""),
     "a seed without a named operator is an anonymous claim"),
    (dict(polarity="like", obj="it", actor="adrian"),
     "deictic/short object has no stable referent"),
    (dict(polarity="like", obj="this song", actor="adrian"),
     "deictic head"),
    (dict(polarity="like", obj="x" * 200, actor="adrian"),
     "over-long object"),
    (dict(polarity="like", obj="dinosaurs", actor="adrian",
          expires_ts="not-a-date"), "unparseable expiry"),
])
def test_seed_rejects_bad_input_loudly(kwargs, why):
    """Opposite failure posture to the conversational writer: the operator is
    at a terminal, so raising is the honest behaviour."""
    with pytest.raises(ValueError):
        people.build_seed_record(**kwargs)


def test_obis_own_words_supersede_an_operator_seed():
    seed = people.build_seed_record(polarity="like", obj="broccoli",
                                    actor="adrian", ts="2026-08-20T00:00:00Z")
    people.append_person_facts([seed], now=NOW)
    later = _facts("I hate broccoli")
    people.append_person_facts(later, now=NOW)
    live = [r for r in people.read_people(now=NOW)
            if not provenance.is_superseded(r)]
    assert [r["polarity"] for r in live] == ["dislike"]
    assert live[0]["source"] == "conversation"


def test_seed_expiry_is_honoured_at_read_time():
    rec = people.build_seed_record(polarity="like", obj="training wheels",
                                   actor="adrian",
                                   expires_ts="2026-08-24T00:00:00Z")
    people.append_person_facts([rec], now=NOW)
    assert people.read_people(now=NOW) == []
    assert people.load_people("spark") != []  # on disk, never deleted


def test_seed_cli_is_dry_by_default_and_writes_only_with_write_flag(tmp_path):
    import os
    import subprocess
    root = Path(people.__file__).resolve().parent.parent.parent
    env = dict(os.environ, PX_STATE_DIR=str(tmp_path),
               PYTHONPATH=str(root / "src"))
    cli = [str(root / "bin" / "px-person-seed"), "--by", "adrian",
           "--like", "dinosaurs", "--like", "cuttlefish"]
    dry = json.loads(subprocess.run(cli, env=env, capture_output=True,
                                    text=True, timeout=60).stdout)
    assert dry["status"] == "ok" and dry["dry"] is True and dry["written"] == 0
    assert [r["topic"] for r in dry["records"]] == [
        "preference:dinosaurs", "preference:cuttlefish"]
    assert not (tmp_path / "people-spark.jsonl").exists()
    wet = json.loads(subprocess.run(cli + ["--write"], env=env,
                                    capture_output=True, text=True,
                                    timeout=60).stdout)
    assert wet["written"] == 2
    stored = people.load_people("spark")
    assert {r["source"] for r in stored} == {"operator_seed"}


def test_seed_cli_refuses_an_empty_seed_set(tmp_path):
    import os
    import subprocess
    root = Path(people.__file__).resolve().parent.parent.parent
    env = dict(os.environ, PX_STATE_DIR=str(tmp_path),
               PYTHONPATH=str(root / "src"))
    out = subprocess.run([str(root / "bin" / "px-person-seed"),
                          "--by", "adrian", "--write"],
                         env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 1
    assert json.loads(out.stdout)["status"] == "error"
    assert not (tmp_path / "people-spark.jsonl").exists()
