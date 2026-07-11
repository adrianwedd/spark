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
