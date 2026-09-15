"""Structural test: the reflection ladder stays deleted.

`mind.py` used to carry its own four-rung ladder — resident Claude, an
`M1`/`M5.local` Ollama daemon, Ollama Cloud, and a Pi-local Ollama — behind
`call_claude()` and `call_ollama()`. Both were dead by the time of #308, and
`call_ollama()` was deliberately kept dead while it still referenced
`OLLAMA_CLOUD_HOST`, because "dead code that references a forbidden
destination" is the shape that turns back into architecture when someone
re-wires it.

#308 moved the cognition tier itself onto Ollama Cloud and deleted the ladder's
host constants along with `call_ollama()`. What replaces the old pin is the
property that outlived the ladder:

  **A failure of the cognition tier defers. It does not walk to a second
  destination — no resident Claude, no Pi-local daemon, no second model.**

So this test no longer guards a forbidden *destination*. It guards the absence
of a second destination: no local-host constant, no ladder function, and
`call_llm()` reaching exactly one backend.
"""
from __future__ import annotations

import ast
from pathlib import Path

MIND_PATH = Path(__file__).resolve().parent.parent / "src" / "pxh" / "mind.py"


def _called_names(tree: ast.AST) -> set[str]:
    """Return the set of bare-name function calls in an AST."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name:
                names.add(name)
    return names


def _tree() -> ast.Module:
    return ast.parse(MIND_PATH.read_text(encoding="utf-8"))


def test_the_reflection_ladder_is_gone():
    """Not just uncalled — undefined. A ladder cannot be re-wired if it is absent."""
    defined = {node.name for node in ast.walk(_tree())
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "call_ollama" not in defined, (
        "call_ollama() is the old Ollama rung. The tier is pxh.m5 now; a second "
        "place that decides where cognition runs is what #302 cost."
    )


def test_no_local_ollama_host_survives_in_mind():
    """`mind.py` must not be able to name a Pi-local or LAN daemon at all."""
    source = MIND_PATH.read_text(encoding="utf-8")
    for token in ("localhost:11434", "127.0.0.1:11434", "M5.local:11434"):
        assert token not in source, (
            f"{token} reappeared in mind.py. Reflection reaches the cognition "
            f"tier through pxh.m5 and defers; it does not hold a local host."
        )


def test_call_llm_reaches_exactly_one_backend():
    """One backend, no `or`-chain: ask_m5(), and a defer on failure."""
    tree = _tree()
    fn = next(node for node in ast.walk(tree)
              if isinstance(node, ast.FunctionDef) and node.name == "call_llm")
    called = _called_names(fn)
    assert "ask_m5" in called
    for rung in ("call_claude", "call_claude_haiku", "call_ollama", "ask_brain"):
        assert rung not in called, (
            f"call_llm() calls {rung}(). Reflection defers on a cognition-tier "
            f"failure; it never escalates to Claude or walks to another model."
        )


def test_call_claude_is_never_called():
    """The old reflection Claude tier is dead; ask_brain is the brain's door."""
    assert "call_claude" not in _called_names(_tree()), (
        "call_claude() is the old reflection Claude tier. If it is being called "
        "again, that is the re-wiring of the old fallback ladder."
    )
