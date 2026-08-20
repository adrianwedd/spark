"""Structural test: dead Ollama/Claude tier functions in mind.py stay dead.

``call_ollama()`` and ``call_claude()`` are remnants of the old reflection
fallback ladder. They reference ``OLLAMA_CLOUD_HOST`` and ``LOCAL_OLLAMA_HOST``
— the Cloud and Pi-local Ollama tiers that migrated cognition must never reach.

They are currently dead code: ``call_llm()`` goes straight to ``ask_m5()`` and
defers on failure. But "dead code that references a forbidden destination" is
exactly the shape that turns back into architecture when someone re-wires it,
so this test pins the fact that nothing calls them.

If a future change needs ``call_ollama`` or ``call_claude`` again, it must
delete this test deliberately — the same standard the resident-only invariant
applies to its own scanner.
"""
from __future__ import annotations

import ast
from pathlib import Path


def _function_names_called_in(tree: ast.AST) -> set[str]:
    """Return the set of bare-name function calls in an AST."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name:
                names.add(name)
    return names


def test_call_ollama_is_never_called():
    """call_ollama references OLLAMA_CLOUD_HOST — a forbidden fallback tier."""
    mind_path = Path(__file__).resolve().parent.parent / "src" / "pxh" / "mind.py"
    tree = ast.parse(mind_path.read_text(encoding="utf-8"))
    called = _function_names_called_in(tree)
    assert "call_ollama" not in called, (
        "call_ollama() is dead code referencing OLLAMA_CLOUD_HOST (a forbidden "
        "fallback tier for migrated cognition). If it is being called again, "
        "that is the exact re-wiring this test exists to catch."
    )


def test_call_claude_is_never_called():
    """call_claude is the old reflection Claude tier — replaced by ask_brain."""
    mind_path = Path(__file__).resolve().parent.parent / "src" / "pxh" / "mind.py"
    tree = ast.parse(mind_path.read_text(encoding="utf-8"))
    called = _function_names_called_in(tree)
    assert "call_claude" not in called, (
        "call_claude() is the old reflection Claude tier. call_llm() now goes "
        "through ask_m5() and defers; call_claude is dead code. If it is being "
        "called again, that is a re-wiring of the old fallback ladder."
    )