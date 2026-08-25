"""Structural invariants for the person store — the firewall, not the feature.

`tests/test_people.py` checks what the writer extracts. This file checks the
property that has to hold even if every pattern in it is wrong: **reflection
cannot see this store.** Reflection's output lands in `thoughts-spark.jsonl`,
which is served at `/api/v1/public/thoughts` and forwarded to the site feed,
the blog and Bluesky. A fact a child stated in private has exactly one thing
between it and that pipeline, and it is not a prompt instruction — it is the
fact that `mind.reflection()` never opens the file.

Same posture as `tests/test_policy_invariants.py`: a source scan, so a future
bridge fails a test rather than being noticed in review. `src/pxh/people.py`
and this file are blacklisted from px-evolve — `mind.py` and `voice_loop.py`
are both *whitelisted* self-evolution targets, so the module SPARK could edit
must not also be the module that decides whether the edit is allowed.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from pxh import claude_session, memory, mind, people, voice_loop

SRC = Path(mind.__file__).resolve().parent

# Substrings that would mean reflection had acquired a route to the store.
# Deliberately specific rather than the bare word "people": `mind.py` legitimately
# says `rooms_with_people` about Frigate labels, and a scan that cries wolf on
# prose is a scan someone eventually deletes.
FORBIDDEN = ("pxh.people", "import people", "people_file", "read_people",
             "load_people", "record_person_facts", "extract_person_facts",
             "people-")


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names.add(base)
            names.update(f"{base}.{a.name}" if base else a.name
                         for a in node.names)
    return names


def test_reflection_context_builder_has_no_route_to_the_person_store():
    src = inspect.getsource(mind.reflection)
    hits = [token for token in FORBIDDEN if token in src]
    assert not hits, f"reflection() references the person store: {hits}"


def test_mind_does_not_import_people_anywhere():
    """Not just reflection: no import at all, so no future helper inside
    `mind.py` can quietly become the bridge."""
    imported = _imported_modules(Path(mind.__file__))
    assert not {n for n in imported if n.endswith("people") or ".people" in n}


def test_mind_source_never_names_the_person_store():
    src = Path(mind.__file__).read_text(encoding="utf-8")
    hits = [token for token in FORBIDDEN if token in src]
    assert not hits, f"mind.py references the person store: {hits}"


def test_people_does_not_import_mind():
    """The other direction matters too — an import cycle is how a 'just for
    logging' call into the cognitive loop starts."""
    imported = _imported_modules(Path(people.__file__))
    assert not {n for n in imported if n.endswith("mind") or ".mind" in n}


def test_person_store_is_physically_separate_from_consolidated_memory():
    """The firewall is the filesystem. If these two ever resolve to the same
    file, reflection reads person facts on its next tick."""
    assert people.people_file("spark") != memory.memories_file("spark")
    assert "people-" in people.people_file("spark").name
    assert "memories-" in memory.memories_file("spark").name


def test_no_other_module_reads_the_person_store():
    """Step 3 is write-only. Retrieval (step 4) will add readers deliberately;
    until then anything reading this store is an accident."""
    readers = set()
    for path in sorted(SRC.glob("*.py")):
        if path.name == "people.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "read_people" in text or "load_people" in text:
            readers.add(path.name)
    assert readers == set(), f"unexpected readers of the person store: {readers}"


def test_only_the_two_specified_call_sites_write_facts():
    writers = set()
    for path in sorted(SRC.glob("*.py")):
        if path.name == "people.py":
            continue
        if "record_person_facts" in path.read_text(encoding="utf-8"):
            writers.add(path.name)
    assert writers == {"api.py", "voice_loop.py"}


def test_the_writer_holds_no_model_call():
    """Resident-only Claude cuts both ways: the write path must contain no LLM
    call of any kind, so a fact exists only because a human sentence asserted
    it. Deterministic extraction is the strongest available form of that."""
    src = Path(people.__file__).read_text(encoding="utf-8")
    for token in ("claude", "ask_brain", "call_llm", "ollama", "run_claude_session",
                  "subprocess"):
        assert token not in src.lower(), token


@pytest.mark.parametrize("persona", ["gremlin", "vixen"])
def test_the_performance_personas_are_refused_at_the_writer(persona, tmp_path,
                                                            monkeypatch):
    """A per-persona filename alone is not a firewall — it is two stores. The
    refusal lives in `record_person_facts`, above the filename."""
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    assert people.record_person_facts(role="obi", text="I like dinosaurs",
                                      persona=persona) == 0
    assert list(tmp_path.glob("people-*.jsonl")) == []
    assert people.WRITER_PERSONAS == ("spark",)


def test_voice_loop_passes_the_live_persona_through_to_the_gate():
    """The gate is only real if the call site hands it the actual persona —
    a hardcoded "spark" here would let GREMLIN write under SPARK's name."""
    src = inspect.getsource(voice_loop.record_conversation_turn)
    assert "record_person_facts" in src
    assert "persona=persona" in src


def test_writer_and_its_invariants_are_blacklisted_from_self_evolution():
    for path in ("src/pxh/people.py", "tests/test_people_invariants.py"):
        assert path in claude_session.BLACKLIST_FILES
        assert not claude_session.file_in_whitelist(path)
