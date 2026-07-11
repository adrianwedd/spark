"""Tests for pxh.memory — consolidated memory store + relevance retrieval."""
import datetime as dt
import json

import pytest

from pxh import memory


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


def test_retrieve_pads_with_most_recent_when_few_hits():
    memory.append_memories([
        _mem("alpha bravo charlie", ts="2026-07-01T00:00:00Z"),
        _mem("delta echo foxtrot", ts="2026-07-09T00:00:00Z"),
        _mem("Obi built a lego tower", ts="2026-07-05T00:00:00Z"),
    ])
    out = memory.retrieve_memories("lego", n=2, now=NOW)
    assert "lego" in out[0]["text"]
    assert out[1]["text"] == "delta echo foxtrot"  # newest non-hit pads


def test_retrieve_pads_by_timestamp_not_insertion_order():
    memory.append_memories([
        _mem("bravo charlie delta", ts="2026-07-10T00:00:00Z"),   # newest ts, index 0
        _mem("echo foxtrot golf", ts="2026-06-01T00:00:00Z"),     # oldest ts, index 1
        _mem("Obi built a lego tower", ts="2026-07-05T00:00:00Z"),
    ])
    out = memory.retrieve_memories("lego", n=2, now=NOW)
    assert "lego" in out[0]["text"]
    assert out[1]["text"] == "bravo charlie delta"  # newest by ts, despite index 0


def test_retrieve_empty_store_returns_empty():
    assert memory.retrieve_memories("anything") == []


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
    memory.append_memories([_mem("Obi and I built a lego tower on the kitchen floor")])
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
