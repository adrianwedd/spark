"""Which long-lived units must be restarted after a deploy.

A deploy changes *files*; a running process keeps executing the code it
imported. Those are two different states, and the `ExecStart`-points-at-a-
deleted-file check covers only one of them (#332): a unit whose entry point
still exists, but whose *imports* were renamed, moved, or deleted, does not
fail at deploy time. It fails hours later inside a lazy import, and the failure
follows the old name — `ImportError` naming a module the tree no longer has.

The rule here: **a unit is stale when it can execute a changed code path from its
current process image** (#336's decision — reachability, not fan-out). A daemon
that merely *imports* a changed module but references none of the changed names
gains nothing from a restart, and a fleet restart on a one-file observability
change is churn that teaches people to ignore this gate.

Two stages, and the first is a prerequisite for the second: the import closure
must actually have the graph edges before path sensitivity means anything. Three
real shapes in this tree were invisible to it until #336:

* `from pxh import health as _health` — `node.module == "pxh"` and the submodule
  is in `node.names`; reading only `node.module` recorded the literal `"pxh"`,
  a package, and the closure died at `src/pxh/pxh.py`. This is the dominant
  idiom in the tree, used at module scope by eight units.
* ASGI app strings — `bin/px-api-server` runs `exec uvicorn pxh.api:app`, which
  no import-shaped rule can see.
* Shell entries with an embedded Python heredoc — `bin/px-alive` is bash wrapping
  `<<'PY' … PY`, so the file is not parseable as Python and the AST walk finds
  nothing at all.

Beyond the closure: `restart_list` compares the *symbols* a changed module
changed against the names a unit actually references from it, and treats
"cannot tell" as "flag". One case needs more than that comparison: a changed
name the module's *own* code reads — a constant used as a function's default
argument reaches every importer without any of them naming it, which is how a
4 s→12 s ALSA ring in `pxh.mic_stream` could have been deployed without
restarting the capture daemon (replayed on `picar`, 2026-09-17). A false restart costs seconds; the failures this module
exists for (a renamed module reached hours later inside a lazy import) cost a
silent 22:00 job.

This is deliberately a *file* rule rather than a symbol-level proof. A unit
that is flagged but does not actually reach the changed module costs seconds of
downtime; a unit that is missed costs a silent failure on a 22:00 timer, which
is the whole reason this module exists.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

#: Directories whose contents count as "code a process executes".
CODE_DIRS = ("bin", "src", "systemd")

#: Package directory to walk for imports, relative to the repository root.
PACKAGE_DIR = os.path.join("src", "pxh")

#: ASGI app factories named as a string: `exec uvicorn pxh.api:app`. Invisible
#: to every import-shaped rule, and how `px-api-server` reaches `pxh.api` — the
#: unit that was a false negative on the gate's first production run (#336).
_ASGI_APP_RE = re.compile(r"\b(?:uvicorn|gunicorn|hypercorn)\b[^\n]*?\bpxh\.([A-Za-z0-9_]+)")

#: A shell heredoc opener: `<<'PY'`, `<<PY`, `<<-EOF`.
_HEREDOC_OPEN_RE = re.compile(r"<<-?\s*'?([A-Za-z_][A-Za-z0-9_]*)'?")

_IMPORT_MODULE_RE = re.compile(r"""import_module\(\s*["'](pxh\.[A-Za-z0-9_.]+)["']""")
_MODULE_FLAG_RE = re.compile(r"""(?:^|\s)-m\s+pxh\.([A-Za-z0-9_]+)""")


def heredoc_bodies(source: str) -> list[str]:
    """Python payloads embedded in shell heredocs, as text.

    `bin/px-alive` is bash wrapping `<<'PY' … PY`. The file is not parseable as
    Python, so the AST walk finds *nothing* — the entry point of the most
    important daemon on this host had no closure at all (#336). Extraction is
    deliberately dumb (an opener line, then lines until the tag repeats) because
    the alternative is a shell parser; a body that fails to parse is picked up by
    the text scans below instead.
    """
    bodies: list[str] = []
    lines = source.splitlines()
    index = 0
    while index < len(lines):
        match = _HEREDOC_OPEN_RE.search(lines[index])
        if not match:
            index += 1
            continue
        tag = match.group(1)
        index += 1
        body: list[str] = []
        while index < len(lines) and lines[index].strip() != tag:
            body.append(lines[index])
            index += 1
        if body:
            bodies.append("\n".join(body))
        index += 1
    return bodies


def _pxh_modules_in_python(source: str) -> set[str]:
    """Submodule names a *Python* source imports from `pxh`, at any depth."""
    found: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        tree = None
    if tree is None:
        return found
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("pxh"):
            if "." in node.module:
                found.add(node.module.split(".", 1)[1])
            else:
                # `from pxh import health as _health`: the submodule is in
                # `names`, not in `module`. Reading only `module` recorded the
                # literal "pxh" — the package — and the closure died at
                # src/pxh/pxh.py, which does not exist.
                for alias in node.names:
                    found.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("pxh."):
                    found.add(alias.name.split(".", 1)[1])
    return found


def imported_pxh_modules(path: str) -> set[str]:
    """Submodule names of `pxh` imported by a file, at any nesting depth.

    A lazy import inside a function still *names* the module in the process's
    compiled code, so it is found here — that is the case this guard exists for.
    Reads the file as Python *and* any heredoc payload inside it, because the
    `bin/px-*` entries are shell wrappers around exactly that.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            source = fh.read()
    except OSError:
        return set()

    found = _pxh_modules_in_python(source)
    for body in heredoc_bodies(source):
        found |= _pxh_modules_in_python(body)

    # `importlib.import_module("pxh.x")` is a string, invisible to the AST walk
    # above unless the name is also written as an import somewhere.
    for match in _IMPORT_MODULE_RE.findall(source):
        found.add(match.split(".", 1)[1])
    for name in _MODULE_FLAG_RE.findall(source):
        found.add(name)
    for match in _ASGI_APP_RE.findall(source):
        found.add(match)

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


# --- stage 2: which *names* a unit actually reaches (#336) -----------------


@dataclass(frozen=True)
class ModuleChange:
    """What changed in one module between two revisions."""

    #: Top-level names (functions, classes, assignments) whose source differs.
    symbols: frozenset[str]
    #: A top-level statement that is not one of those differs — an import block,
    #: a decorator, a conditional definition — or the module could not be
    #: parsed. Every unit that imports the module is stale, because what the
    #: module *provides* may have moved rather than changed in place. This is
    #: #332's shape: a renamed import inside a module only fails at call time.
    module_level: bool
    #: The module's own call graph at top level: definition -> every top-level
    #: name that definition reads. Changed names are matched against the
    #: *reachable* set from the names a unit references, because a change
    #: reaches a unit through code it calls whether the unit names the changed
    #: thing or not, and through more than one hop:
    #:
    #: - one hop: `ArecordStream.__init__`'s default is the constant that moved,
    #:   so a unit naming only `ArecordStream` is affected (#365);
    #: - two and more: `awareness_tick` changed, `bin/px-mind` names only
    #:   `main`, and `main` reaches `awareness_tick` through the loop (#370).
    #:
    #: Over-approximating this (it is a static graph, so no dynamic dispatch) is
    #: the right direction: a false restart costs seconds, a missed one costs
    #: hours of a daemon running code that no longer exists on disk.
    local_reads: Mapping[str, frozenset[str]] = field(default_factory=dict)
    #: A changed name read *outside* any definition — the module's own
    #: import-time state moved, so every importer is affected, like
    #: `module_level`.
    read_at_module_level: bool = False


def _reachable(seeds: Iterable[str], edges: Mapping[str, frozenset[str]]) -> set[str]:
    """Everything reachable from `seeds` in the module's own top-level graph."""
    seen: set[str] = set()
    stack = list(seeds)
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        stack.extend(edges.get(name, ()))
    return seen


def _internal_flows(source: str) -> tuple[dict[str, set[str]], set[str]]:
    """`({definition: top-level names it reads}, names read at module level)`.

    `DEFAULT_ALSA_BUFFER_S` is read exactly once in `pxh/mic_stream.py` — as the
    default value of `ArecordStream.__init__`'s `alsa_buffer_s` parameter — so
    every unit that reaches `ArecordStream` is affected by the constant's value
    while naming only the class. The deploy gate called that "references none of
    the changed names", which would have left `px-wake-listen` on the old 4 s
    ring (replayed on `picar`, 2026-09-17). The same shape appears two hops
    deeper in `pxh/mind.py`: `awareness_tick` changed, `bin/px-mind` names only
    `main`, and `main` reaches the tick through the loop (#370).

    A `Load` occurrence is the whole test, and the *enclosing definition* is the
    edge, so a unit's reachable set can be walked. Only names bound at top level
    count as edges: a parameter or a builtin is not something this module can
    change under a caller. Reads outside any definition are reported separately
    because those are import-time state, which every importer sees.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}, {}  # callers treat an unparsable change as undetermined
    top_level: set[str] = set()
    edges: dict[str, set[str]] = {}
    definitions = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    definition_ids = {id(node) for node in definitions}
    top_names = {node.name for node in definitions}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            top_names.update(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            top_names.add(node.target.id)
    for node in definitions:
        reads = edges.setdefault(node.name, set())
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                if sub.id in top_names:
                    reads.add(sub.id)
    for node in tree.body:
        if id(node) in definition_ids:
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                if sub.id in top_names:
                    top_level.add(sub.id)
    return edges, top_level


class _DropProse(ast.NodeTransformer):
    """Remove bare string statements before anything is compared (#431).

    Comments never reach the AST; docstrings did. A docstring added to a
    *method* changes the dump of the class containing it, so on 2026-09-18 a
    documentation-only commit to `src/pxh/gpio_lease.py` reported
    `GpioLeaseGuard` as a changed symbol and every unit referencing that name was
    told to restart. Three daemons were restarted for prose — the exact churn the
    reachability rule exists to avoid, and the failure mode that teaches people
    to leave a gate alone.

    The rule is "a bare string is prose or dead code, never behaviour" and it is
    applied at *every* nesting level, not just module level. `_module_symbols`
    already ignored a module-level bare string; leaving nested ones counted made
    the same edit a change or not depending on where it sat, which is how the
    class above became a "changed symbol". A statement that evaluates a string
    and discards it cannot affect behaviour, so nothing real is hidden by this.
    """

    def visit_Expr(self, node):
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return None
        return node


def _module_symbols(source: str) -> tuple[dict[str, str], str]:
    """`({name: ast dump}, dump of everything else at module level)`.

    Assignments are keyed by the name they bind, so a constant changing is a
    *symbol* change rather than a module-level one. The second element is what
    the module does at import time — its import block, conditional definitions,
    decorators — and it only matters by *comparison*: an unchanged import block
    must not make every importer stale, which is what a presence check would do
    (every module has imports).
    """
    tree = _DropProse().visit(ast.parse(source))
    symbols: dict[str, str] = {}
    others: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols[node.name] = ast.dump(node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    symbols[target.id] = ast.dump(node)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            symbols[node.target.id] = ast.dump(node)
        else:
            others.append(ast.dump(node))
    return symbols, "\n".join(others)


def changed_symbols(prev: str, head: str, path: str, root: str = ".") -> ModuleChange | None:
    """What `path` changed between two revisions. None when it cannot be told.

    Reads both revisions through `git show`, so it works before and after the
    checkout and for deleted files. `None` is not "nothing changed" — the caller
    must treat it as "flag", which is the direction this whole module errs in.
    """
    def _show(rev: str) -> str | None:
        try:
            out = subprocess.run(
                ["git", "show", f"{rev}:{path}"],
                cwd=root, capture_output=True, text=True, check=False,
            )
        except OSError:
            return None
        return out.stdout if out.returncode == 0 else None

    before_src, after_src = _show(prev), _show(head)
    if before_src is None or after_src is None:
        return None
    try:
        before, before_other = _module_symbols(before_src)
        after, after_other = _module_symbols(after_src)
    except SyntaxError:
        return None
    symbols = frozenset(
        name for name in set(before) | set(after)
        if before.get(name) != after.get(name)
    )
    edges, top_level = _internal_flows(after_src)
    return ModuleChange(
        symbols=symbols,
        module_level=before_other != after_other,
        local_reads={name: frozenset(reads) for name, reads in edges.items()},
        read_at_module_level=bool(top_level & symbols),
    )


def _dotted(node: ast.AST) -> str | None:
    """`pxh.mind.m5_mod` for a dotted attribute chain, else None."""
    parts: list[str] = []
    cursor = node
    while isinstance(cursor, ast.Attribute):
        parts.append(cursor.attr)
        cursor = cursor.value
    if not isinstance(cursor, ast.Name):
        return None
    parts.append(cursor.id)
    return ".".join(reversed(parts))


def _references_in_python(source: str, module: str) -> tuple[set[str], bool]:
    """`(names, resolvable)` for one Python source's use of `pxh.<module>`."""
    names: set[str] = set()
    resolvable = True
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return names, False

    # Bindings that point at the module: `from pxh import health as _health`
    # binds `_health`; `import pxh.health` binds the dotted path `pxh.health`.
    bound_names: set[str] = set()
    dotted = f"pxh.{module}"

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == f"pxh.{module}":
                for alias in node.names:
                    if alias.name == "*":
                        resolvable = False
                    elif alias.name != "":
                        names.add(alias.name)
            elif node.module == "pxh":
                for alias in node.names:
                    if alias.name == module:
                        bound_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == dotted:
                    if alias.asname:
                        bound_names.add(alias.asname)
                    else:
                        bound_names.add(dotted)
        elif isinstance(node, ast.Attribute):
            value = node.value
            if isinstance(value, ast.Name) and value.id in bound_names:
                names.add(node.attr)
            elif dotted in bound_names and _dotted(value) == dotted:
                names.add(node.attr)
        elif isinstance(node, ast.Call):
            func = node.func
            is_getattr = (isinstance(func, ast.Name) and func.id == "getattr")
            if is_getattr and node.args:
                first = node.args[0]
                if isinstance(first, ast.Name) and first.id in bound_names:
                    # getattr(health_mod, name) — the name is data, not syntax.
                    resolvable = False
                elif dotted in bound_names and _dotted(first) == dotted:
                    resolvable = False

    return names, resolvable


def file_module_references(path: str, module: str) -> tuple[set[str], bool]:
    """Names `path` reaches from `pxh.<module>`, and whether that set is complete.

    Covers the idioms this tree actually uses — `from pxh.mod import a`,
    `from pxh import mod as alias` + `alias.a`, `import pxh.mod` + `pxh.mod.a`,
    `import pxh.mod as alias` + `alias.a` — and answers `resolvable=False` for
    the ones it cannot (`getattr`, `import *`, `importlib.import_module`). The
    caller turns that into a restart rather than a pass.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            source = fh.read()
    except OSError:
        return set(), False

    # A `bin/px-*` entry is a shell wrapper: the whole file is not Python, and
    # its *bodies* are the code. Treating the unparseable wrapper as
    # "unresolvable" would flag every unit whose entry is a heredoc, which is
    # most of them — the opposite of what this stage is for.
    chunks: list[str] = []
    try:
        ast.parse(source)
        chunks.append(source)
    except SyntaxError:
        pass
    bodies = heredoc_bodies(source)
    chunks.extend(bodies)
    if not chunks:
        # No embedded Python. Two very different cases hide here: a wrapper whose
        # Python is *named* (`exec python -m pxh.mind`, `exec uvicorn pxh.api:app`)
        # has no hidden references, while a wrapper with an inline `python -c`
        # we cannot parse might. The first is resolvable-and-empty, the second is
        # the fail-safe case.
        if _MODULE_FLAG_RE.search(source) or _ASGI_APP_RE.search(source):
            return set(), True
        return (set(), False) if "python" in source else (set(), True)

    names: set[str] = set()
    resolvable = True
    for chunk in chunks:
        chunk_names, chunk_resolvable = _references_in_python(chunk, module)
        names |= chunk_names
        resolvable = resolvable and chunk_resolvable

    # Text-level literals survive even in a shell wrapper.
    for text in [source, *bodies]:
        for match in _IMPORT_MODULE_RE.findall(text):
            if match == f"pxh.{module}":
                # The module is reached, the names are not: dynamic by
                # construction, so a change inside it cannot be ruled out.
                resolvable = False
    return names, resolvable


def unit_module_references(entry: str, root: str, module: str) -> tuple[set[str], bool]:
    """Union of every executed file's references to `pxh.<module>`."""
    names: set[str] = set()
    resolvable = True
    for path in sorted(executed_paths(entry, root)):
        file_names, file_resolvable = file_module_references(os.path.join(root, path), module)
        names |= file_names
        resolvable = resolvable and file_resolvable
    return names, resolvable


def _module_name_for(path: str) -> str | None:
    """`src/pxh/health.py` -> `health`; anything else -> None."""
    prefix = PACKAGE_DIR + os.sep
    if not path.startswith(prefix) or not path.endswith(".py"):
        return None
    return path[len(prefix):-3].replace(os.sep, ".")


#: Where systemd reads installed units from on the host this runs on.
SYSTEMD_INSTALL_DIR = "/etc/systemd/system"


@dataclass(frozen=True)
class UnitFileDrift:
    """A repo unit file whose installed copy is not what the repo says.

    The gate's promise — "every long-lived unit is executing the deployed
    revision" — is about *code*. It says nothing about the unit file itself, and
    six installed units on `picar` were months behind the repo on 2026-09-18
    (`px-post.service` and `px-evolve.service` since March, `px-mind.service`
    since May, `px-api-server.service` since July, `px-wake-listen.service` since
    August), including *functional* differences: bounded restarts,
    `Wants=network-online.target`, and a narrowed `PATH`. Those changes were
    reviewed and merged; they were simply never installed, and nothing on the
    host noticed.
    """

    unit: str
    repo_path: str
    installed_path: str
    reason: str  # "differs" | "not installed" | "unreadable"


def repo_unit_names(root: str = ".", unit_src_dir: str = "systemd") -> list[str]:
    """Every unit file the repo ships (`*.service`, `*.timer`), by name.

    The drift check iterates *these*, not the running units: a unit named outside
    the `px-*` namespace (`spark-pip-cleanup.timer`) was invisible to a
    `systemctl list-units px-*` sweep, and a unit that is **not installed at all**
    (`px-io-attrib.service`) cannot appear in a list of running units by
    definition -- which is exactly the case an operator most needs to see.
    """
    src_root = os.path.join(root, unit_src_dir)
    try:
        names = sorted(os.listdir(src_root))
    except OSError:
        return []
    return [n for n in names if n.endswith((".service", ".timer"))]


def _digest(path: str) -> str | None:
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


def unit_file_drift(
    unit: str,
    root: str = ".",
    install_dir: str = SYSTEMD_INSTALL_DIR,
    unit_src_dir: str = "systemd",
) -> list[UnitFileDrift]:
    """Repo unit files (and drop-ins) that differ from the installed copies.

    Best-effort and never raising: an unreadable installed file is reported as
    `unreadable` rather than as drift, because "cannot check" and "changed" are
    different facts and only one of them is actionable.
    """
    src_root = os.path.join(root, unit_src_dir)
    candidates: list[tuple[str, str]] = [
        (os.path.join(src_root, unit), os.path.join(install_dir, unit))
    ]
    drop_in_src = os.path.join(src_root, unit + ".d")
    drop_in_dst = os.path.join(install_dir, unit + ".d")
    try:
        names = sorted(os.listdir(drop_in_src))
    except OSError:
        names = []
    for name in names:
        candidates.append((os.path.join(drop_in_src, name), os.path.join(drop_in_dst, name)))

    drift: list[UnitFileDrift] = []
    for repo_path, installed_path in candidates:
        if not os.path.isfile(repo_path):
            continue
        repo_digest = _digest(repo_path)
        if repo_digest is None:
            continue  # the repo copy is unreadable: not something to report here
        installed_digest = _digest(installed_path)
        reason = None
        if installed_digest is None:
            reason = "unreadable" if os.path.exists(installed_path) else "not installed"
        elif installed_digest != repo_digest:
            reason = "differs"
        if reason is not None:
            drift.append(
                UnitFileDrift(
                    unit=unit,
                    repo_path=os.path.relpath(repo_path, root),
                    installed_path=installed_path,
                    reason=reason,
                )
            )
    return drift


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


def _change_hits(
    unit: UnitState,
    moved: Sequence[str],
    root: str,
    executed: set[str],
    changes: Mapping[str, ModuleChange | None] | None,
) -> list[tuple[str, str]]:
    """`(path, why)` for each moved file this unit can actually *reach*.

    With `changes` supplied this is #336's rule: a changed module counts only if
    the unit references a name the module changed, and anything undecidable
    counts. Without it — a caller with no git revisions to hand — every executed
    path counts, which is this module's earlier, deliberately over-broad rule.
    """
    entry_rel = os.path.relpath(unit.entry, root) if unit.entry else None
    hits: list[tuple[str, str]] = []
    for path in moved:
        if path not in executed:
            continue
        if path == entry_rel:
            # The process *is* this file; which of its paths the invocation
            # reaches is not statically knowable.
            hits.append((path, "its own entry point"))
            continue
        module = _module_name_for(path) if changes is not None else None
        if module is None:
            hits.append((path, "executes it"))
            continue
        change = changes.get(path)
        if change is None:
            hits.append((path, "change could not be determined"))
            continue
        if change.module_level:
            hits.append((path, "module-level change (imports or top-level logic)"))
            continue
        if change.read_at_module_level:
            hits.append((path, "a changed name is read at module level (import-time state)"))
            continue
        names, resolvable = unit_module_references(unit.entry, root, module)
        if not resolvable:
            hits.append((path, "references it in a way static analysis cannot resolve"))
            continue
        if not names:
            # The unit *runs* this module rather than naming anything in it:
            # `bin/px-mind` ends in `exec python -m pxh.mind "$@"`, so there is
            # no name to compare against and every changed symbol is reachable.
            # This is what hid `#370`'s `awareness_tick` change — the verdict
            # read "references none of the changed names", which was true and
            # meaningless. A dead import now costs a restart instead.
            hits.append((path, "this unit runs the module, so any change in it counts"))
            continue
        touched = change.symbols & names
        if touched:
            shown = ", ".join(sorted(touched)[:3])
            hits.append((path, f"references removed or changed {shown}"))
            continue
        # The other way a changed name reaches this unit: through code the unit
        # *does* reference, possibly several hops in. `px-wake-listen` names
        # `ArecordStream` whose default argument is the constant that moved;
        # `bin/px-mind` names `main`, and `main` reaches the tick that changed.
        # The reference comparison above answers "not referenced" and would be
        # wrong in both cases. Reachability from the unit's own names keeps this
        # from degenerating into "any importer of the module".
        flowed = sorted(_reachable(names, change.local_reads) & change.symbols)
        if flowed:
            shown = ", ".join(flowed[:3])
            hits.append((path, f"changes {shown}, which code this unit reaches"))
    return hits


@dataclass(frozen=True)
class CarriedStaleness:
    """A long-lived unit older than the files it executes — whatever deploy did it (#421).

    A different question from `Verdict`, and the difference is the point.
    `restart_list` answers "must *this* deploy restart it", which is what a
    deploy step needs and is computed from `HEAD@{1}..HEAD`. This answers "is
    this host running what its checkout says", which is what a human reading
    the gate's summary line believes they are being told. A restart that could
    not be performed — no `sudo -n` grant for that unit, an interrupted deploy —
    is invisible to the delta check forever after, because nothing anywhere
    remembers that it was owed.

    Deliberately not folded into `restart_list`: same inputs, different
    question, and a summary that mixes them makes a to-do list for the future
    out of a report about the past.
    """

    name: str
    started_ts: float
    newest: float
    paths: tuple[str, ...]


def carried_staleness(
    units: Iterable[UnitState],
    root: str = ".",
    deploy_ts: float | None = None,
) -> list[CarriedStaleness]:
    """Units whose executed files are newer on disk than the process running them.

    Works entirely from what the host already has — the unit's start time from
    systemd, the file's mtime from the checkout — because a fast-forward writes
    exactly the files it changed. No git history walk, and no state kept
    between runs.

    Two deliberate omissions:

    * A unit systemd will not report a start time for is **not** flagged here.
      `restart_list` already flags those ("start time unknown"), and a second
      warning built on an absent number would be a guess dressed as a finding.
    * A unit that runs nothing under this repo is skipped, for the same reason.
      "Under this repo" is checked on the entry's real path: an out-of-checkout
      entry has no meaningful mtime to compare against, and `_changed_at` would
      hand back its conservative infinity for a file that does not exist —
      flagging a unit on the strength of a comparison it never made.

    Known false positive, stated rather than discovered later: a file whose
    mtime moved without its content changing (a no-op `git checkout` of an
    identical blob) flags its unit. A restart on that costs seconds; the exact
    alternative is a content hash per executed path per run. The conservative
    rule is the cheap one here.
    """
    out: list[CarriedStaleness] = []
    root_abs = os.path.abspath(root)
    for unit in units:
        if not unit.entry or unit.started_ts is None:
            continue
        if not os.path.abspath(unit.entry).startswith(root_abs + os.sep):
            continue
        executed = executed_paths(unit.entry, root)
        if not executed:
            continue
        newer = [
            (path, _changed_at(path, root, deploy_ts))
            for path in sorted(executed)
            if _changed_at(path, root, deploy_ts) > unit.started_ts
        ]
        if not newer:
            continue
        out.append(
            CarriedStaleness(
                name=unit.name,
                started_ts=unit.started_ts,
                newest=max(ts for _p, ts in newer),
                paths=tuple(path for path, _ts in newer),
            )
        )
    return out


def restart_list(
    moved: Sequence[str],
    units: Iterable[UnitState],
    root: str = ".",
    deploy_ts: float | None = None,
    changes: Mapping[str, ModuleChange | None] | None = None,
) -> list[Verdict]:
    """Decide which units are running code older than the deploy that changed it.

    `changes` maps each moved path to what changed inside it (see
    `changed_symbols`); `None` for a path means "could not be determined" and is
    treated as a restart. Omitting the mapping entirely falls back to the
    path-level rule, which flags every unit that merely executes a moved file —
    the conservative direction, and what callers without git revisions get.
    """
    moved = [path for path in moved if path]
    verdicts: list[Verdict] = []
    for unit in units:
        if not unit.entry:
            verdicts.append(Verdict(unit.name, False, "runs nothing under this repo"))
            continue
        executed = executed_paths(unit.entry, root)
        hits = _change_hits(unit, moved, root, executed, changes)
        if not hits:
            # With a change map, "executes nothing" would be misleading: the unit
            # may well execute the moved file and simply reach none of what
            # changed inside it — which is the whole point of the second stage.
            executed_moved = sorted(path for path in moved if path in executed)
            if changes is not None and executed_moved:
                verdicts.append(
                    Verdict(
                        unit.name,
                        False,
                        f"executes {', '.join(executed_moved[:2])} but references none of the "
                        f"changed names",
                    )
                )
            else:
                verdicts.append(Verdict(unit.name, False, "executes nothing this deploy moved"))
            continue
        newest = max(_changed_at(path, root, deploy_ts) for path, _why in hits)
        changed = ", ".join(f"{path} ({why})" for path, why in sorted(hits)[:2])
        if unit.started_ts is None:
            verdicts.append(
                Verdict(unit.name, True, f"start time unknown; executes changed {changed}")
            )
        elif unit.started_ts < newest:
            verdicts.append(Verdict(unit.name, True, f"started before this deploy changed {changed}"))
        else:
            verdicts.append(Verdict(unit.name, False, f"restarted after {changed} changed"))
    return verdicts


def action_summary(
    changed_files: int,
    restart_units: Sequence[str],
    drift: Sequence["UnitFileDrift"],
    carried: Sequence[CarriedStaleness] = (),
) -> str:
    """The gate's first line: what needs doing, not what was checked.

    Added after the same miss twice on 2026-09-18: the detail lines print *after*
    "every long-lived unit is executing the deployed revision", and a host with
    seven drifted unit files (one of them a unit that was never installed) exited
    **0** — so a reader who skimmed the reassurance, or a caller that trusted the
    exit code, saw a clean deploy. The counts belong above the reassurance, not
    below it.

    `carried` is the same lesson one layer out (#421): staleness left by an
    *earlier* deploy is invisible in `changed_files`/`restart_units` by
    construction, so a host carrying two units on old code printed a summary
    that read like a clean one.
    """
    missing = sum(1 for item in drift if item.reason == "not installed")
    drift_part = "unit files to install: {}".format(len(drift))
    if missing:
        drift_part += " ({} missing)".format(missing)
    return "px-deploy-check: {} | units to restart: {} | carried staleness: {} | {}".format(
        "changed files: {}".format(changed_files),
        len(restart_units),
        len(carried),
        drift_part,
    )


def needs_restart(verdicts: Iterable[Verdict]) -> list[Verdict]:
    return [v for v in verdicts if v.needs_restart]
