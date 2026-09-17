"""Which long-lived units must be restarted after a deploy.

A deploy changes *files*; a running process keeps executing the code it
imported. Those are two different states, and the `ExecStart`-points-at-a-
deleted-file check covers only one of them (#332): a unit whose entry point
still exists, but whose *imports* were renamed, moved, or deleted, does not
fail at deploy time. It fails hours later inside a lazy import, and the failure
follows the old name — `ImportError` naming a module the tree no longer has.

The rule here: **a unit needs a restart if any file it executes was changed by
this deploy, and the process started before that change.** "File it executes"
means its entry point, or any `pxh` module in the entry point's static import
closure — imports at any nesting depth, plus `importlib.import_module("pxh...")`
calls written as string literals.

This is deliberately a *file* rule rather than a symbol-level proof. A unit
that is flagged but does not actually reach the changed module costs seconds of
downtime; a unit that is missed costs a silent failure on a 22:00 timer, which
is the whole reason this module exists.
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass
from typing import Iterable, Sequence

#: Directories whose contents count as "code a process executes".
CODE_DIRS = ("bin", "src", "systemd")

#: Package directory to walk for imports, relative to the repository root.
PACKAGE_DIR = os.path.join("src", "pxh")

_IMPORT_MODULE_RE = re.compile(r"""import_module\(\s*["'](pxh\.[A-Za-z0-9_.]+)["']""")
_MODULE_FLAG_RE = re.compile(r"""(?:^|\s)-m\s+pxh\.([A-Za-z0-9_]+)""")


def imported_pxh_modules(path: str) -> set[str]:
    """Submodule names of `pxh` imported by a file, at any nesting depth.

    A lazy import inside a function still *names* the module in the process's
    compiled code, so it is found here — that is the case this guard exists for.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            source = fh.read()
    except OSError:
        return set()

    found: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        tree = None

    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("pxh"):
                found.add(node.module.split(".", 1)[1] if "." in node.module else node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("pxh."):
                        found.add(alias.name.split(".", 1)[1])

    # `importlib.import_module("pxh.x")` is a string, invisible to the AST walk
    # above unless the name is also written as an import somewhere.
    for match in _IMPORT_MODULE_RE.findall(source):
        found.add(match.split(".", 1)[1])
    for name in _MODULE_FLAG_RE.findall(source):
        found.add(name)

    return found


def import_closure(entry: str, root: str) -> set[str]:
    """Transitive `pxh` submodules reachable from an entry point."""
    seen: set[str] = set()
    pending = [entry]
    while pending:
        for name in imported_pxh_modules(pending.pop()):
            if name in seen:
                continue
            seen.add(name)
            module_path = os.path.join(root, PACKAGE_DIR, name + ".py")
            if os.path.isfile(module_path):
                pending.append(module_path)
    return seen


def executed_paths(entry: str, root: str) -> set[str]:
    """Repository-relative paths a unit's process executes: entry + closure."""
    paths = {os.path.relpath(entry, root)}
    for name in import_closure(entry, root):
        paths.add(os.path.join(PACKAGE_DIR, name + ".py"))
    return paths


@dataclass(frozen=True)
class UnitState:
    """One long-lived unit, as observed on the host."""

    name: str
    entry: str | None  # absolute path when the unit runs something in the repo
    started_ts: float | None = None  # None when systemd will not tell us


@dataclass(frozen=True)
class Verdict:
    name: str
    needs_restart: bool
    reason: str


def _changed_at(path: str, root: str, deploy_ts: float | None) -> float:
    """When this path last changed, as best we can tell.

    A file the deploy *deleted* has no mtime: fall back to the deploy stamp, and
    with neither, treat it as newer than any process (conservative — the unit is
    executing code that no longer exists).
    """
    try:
        return os.path.getmtime(os.path.join(root, path))
    except OSError:
        return deploy_ts if deploy_ts is not None else float("inf")


def restart_list(
    moved: Sequence[str],
    units: Iterable[UnitState],
    root: str = ".",
    deploy_ts: float | None = None,
) -> list[Verdict]:
    """Decide which units are running code older than the deploy that changed it."""
    moved = [path for path in moved if path]
    verdicts: list[Verdict] = []
    for unit in units:
        if not unit.entry:
            verdicts.append(Verdict(unit.name, False, "runs nothing under this repo"))
            continue
        executed = executed_paths(unit.entry, root)
        hits = [path for path in moved if path in executed]
        if not hits:
            verdicts.append(Verdict(unit.name, False, "executes nothing this deploy moved"))
            continue
        newest = max(_changed_at(path, root, deploy_ts) for path in hits)
        changed = ", ".join(sorted(hits)[:3])
        if unit.started_ts is None:
            verdicts.append(
                Verdict(unit.name, True, f"start time unknown; executes changed {changed}")
            )
        elif unit.started_ts < newest:
            verdicts.append(Verdict(unit.name, True, f"started before this deploy changed {changed}"))
        else:
            verdicts.append(Verdict(unit.name, False, f"restarted after {changed} changed"))
    return verdicts


def needs_restart(verdicts: Iterable[Verdict]) -> list[Verdict]:
    return [v for v in verdicts if v.needs_restart]
