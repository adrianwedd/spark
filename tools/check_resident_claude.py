#!/usr/bin/env python3
"""Resident-only Claude invariant — the audit guard.

    No production code may invoke Claude non-residently.

    No `claude -p`. No helper whose implementation is `claude -p`. No fallback
    to `call_claude_haiku`. No unclassified "cold Claude" kinds.

The resident `spark-brain` / `spark-io` sessions are SPARK's sole Claude
execution substrate. A cold `claude -p` throws away context on every call,
cannot use SPARK's tools, is unmetered, and — the reason this guard exists
rather than a docs paragraph — is *more* expensive to run than the resident
session it claims to be rescuing. A resident-brain failure that responds by
spawning a fresh Claude on the same Pi does not degrade gracefully; it
amplifies the contention that caused the failure. That cascade is observed
behaviour, not theory (2026-08-19: a 5s tmux delivery timeout under load
escalated to two concurrent `claude -p` processes, a 120s timeout, and a
151-second wait for a child who had said "Hey Spark").

This guard is deliberately not a single grep for one exact string. It matches
the *forms* the violation actually takes in this repo:

  argv_list        a Python list literal that builds a Claude argv with `-p`
  shell_exec       a shell script executing Claude with `-p`
  forbidden_helper a named helper whose whole implementation is a cold start
  bridge_reference wiring that points a caller at such a helper

Only executable production trees are scanned (see SCAN_DIRS). Prose is not
scanned: CLAUDE.md must be able to state the ban using the forbidden words,
and a docstring must be able to say "this replaced `claude -p`" without
tripping the rule it is describing.
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent

# Executable production code. `tests/` is excluded here and handled by the
# canary allowlist below; `docs/` and `*.md` are prose and never scanned.
SCAN_DIRS = ("src/pxh", "bin")

# The one legitimate way to start Claude. `bin/px-claude-session` launches the
# *resident* interactive session — no `-p`, and the process is meant to outlive
# the request. It is named here rather than relying on it not matching today's
# patterns, so that a future edit which adds `-p` to it is a deliberate act
# against an explicit exemption instead of a silent pass.
ALLOWLIST = {
    "bin/px-claude-session": "the resident session launcher — starts the substrate, is not a cold start",
}

# Helpers whose entire implementation is a cold start. Named so that *calling*
# them is a violation even from a file that contains no argv list of its own —
# a fallback ladder three modules deep is still a cold start.
FORBIDDEN_HELPERS = {
    "call_claude_haiku": "reflection's cold-start fallback — reflection is optional work; failure must reduce load, not spawn Claude",
}

# Executable fossils. Referencing one by path re-arms it as architecture, which
# is why the file is deleted rather than deprecated.
FORBIDDEN_ARTIFACTS = {
    "claude-voice-bridge": "the cold-start voice adapter — voice turns run on the resident brain",
}

_CLAUDE_RE = re.compile(r"claude", re.IGNORECASE)
_SHELL_EXEC_RE = re.compile(
    r"""(^|[|;&(]|\bexec\s+|\$\()\s*      # start of a command
        ["']?(?:[\w./${}-]*/)?            # optional path prefix
        (?:claude|\$\{?(?:PX_)?CLAUDE_BIN)  # the binary, literal or via var
        \b[^\n]*?                         # its arguments
        (?<![\w-])-p(?![\w-])             # a bare -p among them
    """,
    re.IGNORECASE | re.VERBOSE,
)


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    kind: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - formatting only
        return f"{self.path}:{self.line}: [{self.kind}] {self.detail}"


def _is_python(path: Path, text: str) -> bool:
    if path.suffix == ".py":
        return True
    first = text.split("\n", 1)[0]
    return first.startswith("#!") and "python" in first


def _strip_shell_comments(text: str) -> Iterator[tuple[int, str]]:
    """Yield (lineno, code) with whole-line comments removed.

    Trailing comments are left alone on purpose: stripping them correctly means
    tracking quoting state, and a false positive here is a cheap, visible
    failure while a false negative is the thing this guard exists to prevent.
    """
    for lineno, raw in enumerate(text.split("\n"), start=1):
        if raw.lstrip().startswith("#"):
            continue
        yield lineno, raw


def _scan_shell(rel: str, text: str) -> Iterator[Violation]:
    for lineno, line in _strip_shell_comments(text):
        if _SHELL_EXEC_RE.search(line):
            yield Violation(rel, lineno, "shell_exec",
                            f"shell invocation of Claude with -p: {line.strip()[:90]}")


def _scan_python(rel: str, text: str) -> Iterator[Violation]:
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:  # pragma: no cover - a broken file fails CI elsewhere
        yield Violation(rel, exc.lineno or 0, "unparseable", f"could not parse: {exc.msg}")
        return

    for node in ast.walk(tree):
        # An argv list: `[claude_bin(), "-p", prompt, ...]`. Requiring a Claude
        # reference *inside the same list* is what keeps espeak's `-p pitch`
        # and tmux's `capture-pane -p` from tripping it.
        if isinstance(node, (ast.List, ast.Tuple)):
            has_p = any(
                isinstance(el, ast.Constant) and el.value == "-p"
                for el in node.elts
            )
            if has_p:
                segment = ast.get_source_segment(text, node) or ""
                if _CLAUDE_RE.search(segment):
                    yield Violation(rel, node.lineno, "argv_list",
                                    "argv list builds a cold `claude -p` invocation")

        # Calling — or defining — a helper that is itself a cold start.
        name = None
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
        elif isinstance(node, ast.FunctionDef):
            name = node.name
        if name in FORBIDDEN_HELPERS:
            yield Violation(rel, node.lineno, "forbidden_helper",
                            f"{name}: {FORBIDDEN_HELPERS[name]}")


def _scan_text_argv(rel: str, text: str) -> Iterator[Violation]:
    """Argv lists inside files the AST cannot reach.

    Several `bin/` entry points are polyglots: a `#!/usr/bin/env bash` shebang
    wrapping Python that is fed to an interpreter later in the file. They are
    neither parseable as Python nor recognisable as shell commands, which is
    exactly the seam the first version of this guard fell through — it reported
    10 violations and silently missed px-post, px-blog and px-cron-say. A
    guard that quietly under-reports is worse than none, because it converts
    "we looked" into "it is clean".
    """
    lines = text.split("\n")
    for lineno, line in enumerate(lines, start=1):
        if line.lstrip().startswith("#"):
            continue
        if not re.search(r"""['"]-p['"]""", line):
            continue
        window = "\n".join(lines[max(0, lineno - 3):lineno])
        if _CLAUDE_RE.search(window):
            yield Violation(rel, lineno, "argv_list",
                            "argv list builds a cold `claude -p` invocation")


def _scan_artifacts(rel: str, text: str) -> Iterator[Violation]:
    for lineno, line in enumerate(text.split("\n"), start=1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        for artifact, why in FORBIDDEN_ARTIFACTS.items():
            if artifact in line and Path(rel).name != artifact:
                yield Violation(rel, lineno, "bridge_reference",
                                f"{artifact}: {why}")
            elif Path(rel).name == artifact:
                yield Violation(rel, lineno, "bridge_reference",
                                f"{artifact} exists: {why}") if lineno == 1 else None


def _candidate_files(repo_root: Path) -> Iterator[Path]:
    for d in SCAN_DIRS:
        root = repo_root / d
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            if path.suffix in {".pyc", ".json", ".wav", ".png"}:
                continue
            if "__pycache__" in path.parts:
                continue
            yield path


def scan(repo_root: Path | None = None) -> list[Violation]:
    """Every resident-only violation in the production trees, sorted.

    `repo_root` is a parameter rather than a constant so the guard can be
    pointed at a synthetic tree. That is what lets the test suite prove the
    detectors still fire — a guard whose only assertion is "the repo is clean"
    passes just as happily when someone has quietly defanged it.
    """
    repo_root = (repo_root or REPO_ROOT).resolve()
    found: list[Violation] = []
    for path in _candidate_files(repo_root):
        rel = str(path.relative_to(repo_root))
        if rel in ALLOWLIST:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        # The fossil itself: its existence is the violation, reported once.
        if Path(rel).name in FORBIDDEN_ARTIFACTS:
            artifact = Path(rel).name
            found.append(Violation(rel, 1, "bridge_reference",
                                   f"{artifact} exists: {FORBIDDEN_ARTIFACTS[artifact]}"))
            continue

        if _is_python(path, text):
            found.extend(_scan_python(rel, text))
        else:
            # Both, deliberately: a bash-shebang file may still carry embedded
            # Python, and the two detectors cover different halves of it.
            found.extend(_scan_shell(rel, text))
            found.extend(_scan_text_argv(rel, text))
        found.extend(v for v in _scan_artifacts(rel, text) if v is not None)

    return sorted(set(found), key=lambda v: (v.path, v.line, v.kind))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--list", action="store_true",
                        help="print the debt map and exit 0 regardless")
    args = parser.parse_args(argv)

    violations = scan(REPO_ROOT)
    if not violations:
        print("resident-only Claude: clean — no cold-start call paths in production code")
        return 0

    by_file: dict[str, list[Violation]] = {}
    for v in violations:
        by_file.setdefault(v.path, []).append(v)

    print(f"resident-only Claude: {len(violations)} violation(s) in {len(by_file)} file(s)\n")
    for path, items in by_file.items():
        print(f"  {path}")
        for v in items:
            print(f"    :{v.line}  [{v.kind}] {v.detail}")
        print()
    print("Every one of these cold-starts Claude. The resident spark-brain /")
    print("spark-io sessions are the only permitted Claude substrate.")
    return 0 if args.list else 1


if __name__ == "__main__":
    sys.exit(main())
