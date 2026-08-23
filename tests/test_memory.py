"""Tests for pxh.memory — consolidated memory store + relevance retrieval."""
import datetime as dt
import json

import pytest

from pxh import memory, provenance


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))


def _mem(text, tags=(), ts=None, importance=0.5):
    return {"ts": ts or "2026-07-10T12:00:00Z", "date": (ts or "2026-07-10")[:10],
            "text": text, "tags": list(tags), "importance": importance,
            "source": "consolidation"}


NOW = dt.datetime(2026, 7, 11, 12, 0, tzinfo=dt.timezone.utc)


def test_tokenize_strips_stopwords_and_case():
    toks = memory._tokenize("The Obi and I built a LEGO tower")
    assert "obi" in toks and "lego" in toks and "tower" in toks
    assert "the" not in toks and "and" not in toks and "a" not in toks


def test_append_and_load_roundtrip():
    memory.append_memories([_mem("first"), _mem("second")])
    loaded = memory.load_memories()
    assert [m["text"] for m in loaded] == ["first", "second"]


def test_load_skips_malformed_lines():
    f = memory.memories_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(_mem("good")) + "\n{broken\n", encoding="utf-8")
    assert [m["text"] for m in memory.load_memories()] == ["good"]


def test_retrieve_ranks_by_token_overlap():
    memory.append_memories([
        _mem("Adrian fixed my servo motor in the dark"),
        _mem("Obi and I built a lego tower on the kitchen floor"),
        _mem("the weather was windy with gusts from the north"),
    ])
    out = memory.retrieve_memories("obi wants to build lego again", n=1, now=NOW)
    assert "lego tower" in out[0]["text"]


def test_retrieve_tag_hits_boost_score():
    memory.append_memories([
        _mem("a quiet unremarkable morning", tags=["weather"]),
        _mem("another quiet morning", tags=["obi", "school"]),
    ])
    out = memory.retrieve_memories("thinking about obi at school this quiet morning", n=1, now=NOW)
    assert out[0]["tags"] == ["obi", "school"]


def test_retrieve_does_not_pad_partial_results_with_recent():
    """One topical hit and n=2 must return one record, not one plus a filler.

    The filler was indistinguishable from a real hit once it reached the
    reflection prompt, so an unrelated recent memory entered cognition purely
    because a result slot was free.
    """
    memory.append_memories([
        _mem("alpha bravo charlie", ts="2026-07-01T00:00:00Z"),
        _mem("delta echo foxtrot", ts="2026-07-09T00:00:00Z"),
        _mem("Obi built a lego tower", ts="2026-07-05T00:00:00Z"),
    ])
    out = memory.retrieve_memories("lego", n=2, now=NOW)
    assert [m["text"] for m in out] == ["Obi built a lego tower"]


def test_retrieve_empty_store_returns_empty():
    assert memory.retrieve_memories("anything") == []


def test_retrieve_zero_topical_match_returns_nothing():
    memory.append_memories([
        _mem("alpha bravo charlie", ts="2026-07-01T00:00:00Z"),
        _mem("delta echo foxtrot", ts="2026-07-10T00:00:00Z"),
    ])

    assert memory.retrieve_memories("unrelated xylophone quartz", n=2, now=NOW) == []


def test_recent_mode_returns_newest_by_timestamp_not_insertion_order():
    """Recency-only retrieval is still available, but must be asked for."""
    memory.append_memories([
        _mem("bravo charlie delta", ts="2026-07-10T00:00:00Z"),   # newest ts, index 0
        _mem("echo foxtrot golf", ts="2026-06-01T00:00:00Z"),     # oldest ts, index 1
        _mem("hotel india juliet", ts="2026-07-05T00:00:00Z"),
    ])
    out = memory.retrieve_memories("", n=2, mode="recent", now=NOW)
    assert [m["text"] for m in out] == ["bravo charlie delta", "hotel india juliet"]


def test_recent_mode_ignores_the_query_entirely():
    memory.append_memories([
        _mem("Obi built a lego tower", ts="2026-07-01T00:00:00Z"),
        _mem("alpha bravo charlie", ts="2026-07-10T00:00:00Z"),
    ])
    out = memory.retrieve_memories("lego", n=1, mode="recent", now=NOW)
    assert out[0]["text"] == "alpha bravo charlie"


def test_recent_mode_sorts_unparseable_timestamps_last():
    memory.append_memories([
        _mem("corrupt clock", ts="not-a-timestamp"),
        _mem("good clock", ts="2026-01-01T00:00:00Z"),
    ])
    out = memory.retrieve_memories("", n=2, mode="recent", now=NOW)
    assert [m["text"] for m in out] == ["good clock", "corrupt clock"]


def test_unknown_retrieval_mode_is_rejected():
    memory.append_memories([_mem("anything at all")])
    with pytest.raises(ValueError):
        memory.retrieve_memories("anything", mode="vibes")


def test_has_memory_store_false_when_file_missing():
    assert memory.has_memory_store() is False


def test_has_memory_store_false_for_an_empty_file():
    f = memory.memories_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("", encoding="utf-8")
    assert memory.has_memory_store() is False


def test_has_memory_store_true_once_a_memory_is_written():
    memory.append_memories([_mem("something happened")])
    assert memory.has_memory_store() is True


def test_importance_does_not_affect_retrieval_ranking():
    memory.append_memories([
        _mem("obi played lego", importance=1.0),
        _mem("obi played lego", importance=0.0),
    ])

    out = memory.retrieve_memories("obi lego", n=2, now=NOW)

    assert [m["importance"] for m in out] == [0.0, 1.0]


def test_zero_overlap_scores_zero_despite_recency():
    fresh = _mem("xylophone quartz", ts="2026-07-11T11:00:00Z")
    assert memory.score_memory(fresh, memory._tokenize("lego tower"), now=NOW) == 0.0


def test_recency_breaks_ties():
    old = _mem("obi played lego", ts="2026-05-01T00:00:00Z")
    new = _mem("obi played lego", ts="2026-07-10T00:00:00Z")
    q = memory._tokenize("obi lego")
    assert memory.score_memory(new, q, now=NOW) > memory.score_memory(old, q, now=NOW)


def test_append_trims_to_limit(monkeypatch):
    monkeypatch.setattr(memory, "MEMORIES_LIMIT", 5)
    memory.append_memories([_mem(f"m{i}") for i in range(7)])
    loaded = memory.load_memories()
    assert len(loaded) == 5
    assert loaded[0]["text"] == "m2" and loaded[-1]["text"] == "m6"


# --- consolidation ---------------------------------------------------------
from unittest.mock import MagicMock, patch

HOBART = memory.HOBART_TZ


def _write_thoughts(tmp_path_env, n=6, persona="spark"):
    import os
    f = memory._state_dir() / f"thoughts-{persona}.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    lines = []
    for i in range(n):
        ts = (now - dt.timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(json.dumps({"ts": ts, "thought": f"thought {i} about obi and lego",
                                 "mood": "curious", "action": "wait", "salience": 0.6}))
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _claude_ok(payload):
    return MagicMock(stdout=json.dumps(payload), stderr="", returncode=0,
                     duration_s=5.0, model_used="claude-haiku-4-5-20251001")


def test_consolidate_dry_writes_nothing():
    res = memory.consolidate(dry=True)
    assert res["status"] == "dry"
    assert not memory.memories_file().exists()


def test_consolidate_skips_on_too_few_thoughts():
    _write_thoughts(None, n=2)
    res = memory.consolidate()
    assert res["status"] == "skipped"


def test_consolidate_success_writes_deduped_memories():
    _write_thoughts(None, n=8)
    recent_ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    memory.append_memories([
        _mem("Obi and I built a lego tower on the kitchen floor", ts=recent_ts)
    ])
    payload = [
        {"text": "Obi and I built a lego tower on the kitchen floor", "tags": ["obi"],
         "importance": 0.8},                          # dup of existing → dropped
        {"text": "Adrian rewired my memory so I can keep a real past now",
         "tags": ["adrian", "self"], "importance": 0.9},
    ]
    with patch("pxh.claude_session.run_claude_session", return_value=_claude_ok(payload)):
        res = memory.consolidate()
    assert res["status"] == "ok"
    assert res["written"] == 1
    texts = [m["text"] for m in memory.load_memories()]
    assert any("rewired my memory" in t for t in texts)
    assert sum("lego tower" in t for t in texts) == 1  # no duplicate


def test_consolidated_memory_is_stamped_as_generated_narrative():
    """Consolidation distils SPARK's own thoughts, so its output is narrative —
    never something SPARK observed, however the sentence is phrased."""
    _write_thoughts(None, n=8)
    payload = [{"text": "I watched Obi come through the door", "tags": ["obi"],
                "importance": 0.9}]

    with patch("pxh.claude_session.run_claude_session", return_value=_claude_ok(payload)):
        result = memory.consolidate()

    assert result["status"] == "ok"
    record = memory.load_memories()[0]
    assert record["source"] == "consolidation"      # coarse field kept for compat
    p = provenance.read_provenance(record)
    assert p["kind"] == "narrative"
    assert p["confidence"] <= provenance.CONFIDENCE_CEILING["narrative"]
    assert p["legacy"] is False


def test_consolidated_memory_cites_the_thought_window_it_came_from():
    _write_thoughts(None, n=8)
    payload = [{"text": "a durable thing happened", "tags": [], "importance": 0.5}]

    with patch("pxh.claude_session.run_claude_session", return_value=_claude_ok(payload)):
        memory.consolidate()

    evidence = provenance.read_provenance(memory.load_memories()[0])["evidence"]
    assert any("thoughts-spark.jsonl" in ref for ref in evidence)
    assert any("8" in ref for ref in evidence), "cites how many thoughts fed it"


def test_model_supplied_provenance_claims_are_ignored():
    """The consolidating model must not be able to type its own output.

    Letting it set `kind` or `confidence` would hand the writer of a claim the
    power to declare that claim perceived — the exact confusion #170 closes.
    """
    _write_thoughts(None, n=8)
    payload = [{
        "text": "I decided the hallway was empty",
        "tags": ["hallway"],
        "importance": 0.7,
        "kind": "observation",
        "evidence_refs": ["thought-123"],
        "confidence": 1.0,
        "provenance": {"kind": "verification", "source": "the world",
                       "confidence": 1.0},
        "supersedes": ["some-other-memory"],
    }]

    with patch("pxh.claude_session.run_claude_session", return_value=_claude_ok(payload)):
        result = memory.consolidate()

    assert result["status"] == "ok"
    record = memory.load_memories()[0]
    assert "kind" not in record
    assert "evidence_refs" not in record
    assert "confidence" not in record
    assert record["provenance"]["kind"] == "narrative"
    assert record["provenance"]["source"] == "consolidation"
    assert record["provenance"]["supersedes"] == []


def test_provenance_survives_the_whole_round_trip_from_consolidation_to_retrieval():
    """#170's second test bullet, end to end: write it, read it back through
    the path reflection actually uses, and it still knows what it is."""
    _write_thoughts(None, n=8)
    payload = [{"text": "Obi and I built a lego tower", "tags": ["lego"],
                "importance": 0.6}]

    with patch("pxh.claude_session.run_claude_session", return_value=_claude_ok(payload)):
        memory.consolidate()

    out = memory.retrieve_memories("lego tower", n=1)

    assert out and out[0]["text"] == "Obi and I built a lego tower"
    p = provenance.read_provenance(out[0])
    assert p["kind"] == "narrative"
    assert p["source"] == "consolidation"
    assert p["evidence"]


def test_legacy_memories_remain_retrievable_and_read_as_unknown():
    """Records written before #170 must keep working, at unknown provenance."""
    memory.append_memories([_mem("Obi built a lego tower")])   # no provenance key

    out = memory.retrieve_memories("lego", n=1, now=NOW)

    assert out[0]["text"] == "Obi built a lego tower"
    assert provenance.read_provenance(out[0])["kind"] == "unknown"
    assert provenance.read_provenance(out[0])["legacy"] is True


def test_relevance_retrieval_skips_a_superseded_memory():
    old = provenance.stamp(_mem("the hallway is empty", tags=["hallway"]),
                           "inference", "mind:reflection")
    new = provenance.mark_supersedes(
        provenance.stamp(_mem("Obi was in the hallway", tags=["hallway"]),
                         "observation", "vision:describe-scene"), old)
    memory.append_memories([old, new])

    out = memory.retrieve_memories("hallway", n=5, now=NOW)

    assert [m["text"] for m in out] == ["Obi was in the hallway"]
    assert len(memory.load_memories()) == 2, "the store still holds both"


def test_recent_mode_still_shows_superseded_memories_but_marks_them():
    old = provenance.stamp(_mem("the hallway is empty", ts="2026-07-01T00:00:00Z"),
                           "inference", "mind:reflection")
    new = provenance.mark_supersedes(
        provenance.stamp(_mem("Obi was in the hallway", ts="2026-07-02T00:00:00Z"),
                         "observation", "vision:describe-scene"), old)
    memory.append_memories([old, new])

    out = memory.retrieve_memories("", n=5, mode="recent", now=NOW)

    superseded = [m for m in out if m["text"] == "the hallway is empty"]
    assert len(superseded) == 1
    assert provenance.is_superseded(superseded[0]) is True


def test_consolidate_budget_exhausted_is_failed_not_raised():
    from pxh.claude_session import SessionBudgetExhausted
    _write_thoughts(None, n=8)
    with patch("pxh.claude_session.run_claude_session",
               side_effect=SessionBudgetExhausted("consolidate quota reached (1/1)")):
        res = memory.consolidate()
    assert res["status"] == "failed" and "quota" in res["error"]


def test_consolidate_unparseable_response_is_failed():
    _write_thoughts(None, n=8)
    bad = MagicMock(stdout="I could not produce JSON today.", stderr="", returncode=0)
    with patch("pxh.claude_session.run_claude_session", return_value=bad):
        res = memory.consolidate()
    assert res["status"] == "failed"


def test_parse_memory_array_tolerates_fences_and_prose():
    raw = 'Here you go:\n```json\n[{"text": "a memory", "tags": ["x"], "importance": 0.7}]\n```'
    out = memory._parse_memory_array(raw)
    assert out[0]["text"] == "a memory"


def test_maybe_consolidate_outside_window_returns_none():
    noon = dt.datetime(2026, 7, 11, 12, 0, tzinfo=HOBART)
    assert memory.maybe_consolidate(now=noon) is None


def test_maybe_consolidate_runs_once_then_stamps():
    at3 = dt.datetime(2026, 7, 11, 3, 0, tzinfo=HOBART)
    with patch.object(memory, "consolidate", return_value={"status": "ok", "written": 2}) as mc:
        assert memory.maybe_consolidate(now=at3)["status"] == "ok"
        assert memory.maybe_consolidate(now=at3) is None  # stamped done
    assert mc.call_count == 1


def test_maybe_consolidate_two_failures_stop_for_the_day():
    at3 = dt.datetime(2026, 7, 11, 3, 0, tzinfo=HOBART)
    with patch.object(memory, "consolidate", return_value={"status": "failed", "error": "x"}) as mc:
        assert memory.maybe_consolidate(now=at3)["status"] == "failed"
        assert memory.maybe_consolidate(now=at3)["status"] == "failed"
        assert memory.maybe_consolidate(now=at3) is None  # attempt cap
    assert mc.call_count == 2


def test_maybe_consolidate_fresh_date_resets_attempts():
    day1 = dt.datetime(2026, 7, 11, 3, 0, tzinfo=HOBART)
    day2 = dt.datetime(2026, 7, 12, 3, 0, tzinfo=HOBART)
    with patch.object(memory, "consolidate", return_value={"status": "failed", "error": "x"}):
        memory.maybe_consolidate(now=day1)
        memory.maybe_consolidate(now=day1)
    with patch.object(memory, "consolidate", return_value={"status": "ok", "written": 1}) as mc:
        assert memory.maybe_consolidate(now=day2)["status"] == "ok"
    assert mc.call_count == 1


# --- regression tests: review findings --------------------------------------


def test_consolidate_never_raises_on_invalid_utf8():
    """Finding 1: invalid UTF-8 in thoughts/memories files must not raise
    UnicodeDecodeError out of consolidate() — it's called from the mind
    daemon's tick loop, which must never see a raise."""
    thoughts_f = memory._state_dir() / "thoughts-spark.jsonl"
    thoughts_f.parent.mkdir(parents=True, exist_ok=True)
    thoughts_f.write_bytes(b"\xff\xfe not json\n")
    mem_f = memory.memories_file()
    mem_f.parent.mkdir(parents=True, exist_ok=True)
    mem_f.write_bytes(b"\xff\xfe not json\n")

    res = memory.consolidate()
    assert isinstance(res, dict)
    assert res.get("status") in ("failed", "skipped")


def test_maybe_consolidate_dry_returns_none_and_writes_nothing():
    """Finding 2: a dry-run daemon must not consolidate or mutate any state."""
    at3 = dt.datetime(2026, 7, 11, 3, 0, tzinfo=HOBART)
    assert memory.maybe_consolidate(dry=True, now=at3) is None
    assert not memory.consolidation_meta_file().exists()


def _set_recycle_landed(monkeypatch, day):
    """day=None => no marker at all (brain_daemon has never run)."""
    monkeypatch.setattr(
        memory.brain, "recycle_landed_today",
        lambda today: None if day is None else day == today)


def test_maybe_consolidate_defers_during_the_recycle_hour_when_not_yet_landed(monkeypatch):
    """#278: at 02:xx, before brain_daemon's own bookkeeping shows today's
    nightly recycle has landed, consolidation must defer rather than race
    it — and deferring must not cost one of the day's two attempts."""
    at2 = dt.datetime(2026, 7, 11, 2, 30, tzinfo=HOBART)
    _set_recycle_landed(monkeypatch, "2026-07-10")  # yesterday — not landed yet
    with patch.object(memory, "consolidate") as mc:
        assert memory.maybe_consolidate(now=at2) is None
    mc.assert_not_called()
    assert not memory.consolidation_meta_file().exists()


def test_maybe_consolidate_proceeds_during_the_recycle_hour_once_landed(monkeypatch):
    at2 = dt.datetime(2026, 7, 11, 2, 30, tzinfo=HOBART)
    _set_recycle_landed(monkeypatch, "2026-07-11")  # today — already landed
    with patch.object(memory, "consolidate", return_value={"status": "ok", "written": 1}) as mc:
        assert memory.maybe_consolidate(now=at2)["status"] == "ok"
    mc.assert_called_once()


def test_maybe_consolidate_fails_open_when_no_recycle_marker_exists(monkeypatch):
    """A missing recycle_state.json (brain_daemon never ran — e.g. before its
    first boot) must not block consolidation forever; unknown proceeds."""
    at2 = dt.datetime(2026, 7, 11, 2, 30, tzinfo=HOBART)
    _set_recycle_landed(monkeypatch, None)
    with patch.object(memory, "consolidate", return_value={"status": "ok", "written": 1}) as mc:
        assert memory.maybe_consolidate(now=at2)["status"] == "ok"
    mc.assert_called_once()


def test_maybe_consolidate_ignores_recycle_state_past_the_grace_hour():
    """Past the window's first hour, proceed regardless of recycle state —
    a recycle still waiting on a busy brain must not cost the whole day's
    consolidation. No monkeypatch: the real brain.recycle_landed_today runs
    and finds no marker, which alone proves this path never even checks."""
    at3 = dt.datetime(2026, 7, 11, 3, 0, tzinfo=HOBART)
    with patch.object(memory, "consolidate", return_value={"status": "ok", "written": 1}) as mc:
        assert memory.maybe_consolidate(now=at3)["status"] == "ok"
    mc.assert_called_once()


def test_maybe_consolidate_fails_closed_when_meta_stamp_write_fails():
    """Finding 3: if the pre-call attempt stamp can't be written, we must not
    spend the LLM session — consolidate() must not be called, and the
    failure must be reported rather than raised."""
    at3 = dt.datetime(2026, 7, 11, 3, 0, tzinfo=HOBART)
    with patch.object(memory, "atomic_write", side_effect=OSError("disk full")), \
         patch.object(memory, "consolidate") as mc:
        res = memory.maybe_consolidate(now=at3)
    mc.assert_not_called()
    assert isinstance(res, dict)
    assert res.get("status") == "failed"
