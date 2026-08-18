"""Documentation structure tests.

Three checks, deliberately no more. Documentation rot in this repo has always
been *silent* — CLAUDE.md claimed "six kinds" of provenance for as long as
there were seven, and nothing failed. These convert the classes of rot that
can be checked mechanically into red tests, and leave the rest to review.

Not a docs framework. Do not grow this into one: a test that asserts on prose
becomes a test that has to be edited every time prose improves.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Files whose links are checked. The canonical docs plus the entry points that
# route readers into them — the places where a broken link strands someone.
LINK_CHECKED = (
    "CLAUDE.md",
    "AGENTS.md",
    "systemd/README.md",
    "docs/testing.md",
    "docs/git-workflow.md",
    "docs/historical/README.md",
    "docs/superpowers/README.md",
)
LINK_CHECKED_DIRS = ("docs/architecture", "docs/hardware", "docs/operations")

# [text](target) — non-greedy text, target up to the first closing paren.
_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")

_SKIP_PREFIXES = ("http://", "https://", "mailto:", "#")


def _link_checked_files() -> list[Path]:
    files = [REPO_ROOT / name for name in LINK_CHECKED]
    for directory in LINK_CHECKED_DIRS:
        files.extend(sorted((REPO_ROOT / directory).glob("*.md")))
    return files


def _relative_links(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    out = []
    for target in _MD_LINK.findall(text):
        if target.startswith(_SKIP_PREFIXES):
            continue
        out.append(target)
    return out


@pytest.mark.parametrize(
    "doc", _link_checked_files(), ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_relative_links_resolve(doc: Path):
    """A link into the canonical docs must point at a file that exists.

    This is the check that would have caught the docs tree drifting apart as
    subsystems were renamed. Anchors are stripped: whether a heading exists is
    not mechanically checkable in a way worth the false positives.
    """
    assert doc.exists(), f"link-checked file is missing: {doc}"
    broken = []
    for target in _relative_links(doc):
        resolved = (doc.parent / target.split("#", 1)[0]).resolve()
        if not resolved.exists():
            broken.append(target)
    assert not broken, (
        f"{doc.relative_to(REPO_ROOT)} links to missing paths: {sorted(broken)}"
    )


def test_constitution_forbids_blanket_staging():
    """The staging prohibition must survive edits to CLAUDE.md.

    It is the one rule whose violation is silent and unbounded: `git add -A`
    succeeds, the commit reads clean, and the unrelated file only surfaces
    when someone bisects to it. If a rewrite drops it, that must be loud.
    """
    text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    for forbidden in ("git add -A", "git add .", "git commit -a"):
        assert forbidden in text, (
            f"CLAUDE.md no longer names {forbidden!r} as forbidden staging"
        )
    assert "exact owned paths" in text, (
        "CLAUDE.md no longer states the positive rule (stage exact owned paths)"
    )


@pytest.mark.parametrize(
    "banner_doc", ("docs/superpowers/README.md", "docs/historical/README.md")
)
def test_historical_docs_carry_a_banner(banner_doc: str):
    """Specs, plans and archived notes must announce that they are fossils.

    Without the banner a reader finds a confident, dated design document and
    reasonably assumes it describes the running system. Several of them
    describe systems that were never built that way.
    """
    path = REPO_ROOT / banner_doc
    assert path.exists(), f"missing historical index: {banner_doc}"
    text = path.read_text(encoding="utf-8")
    assert "evidence, not operational truth" in text, (
        f"{banner_doc} is missing the fossil banner"
    )


def test_every_canonical_doc_separates_invariant_from_history():
    """Each canonical doc must say which parts are rules and which are history.

    Collapsing the two is how CLAUDE.md grew to 495 lines: incident narrative
    and binding rule in the same voice, so neither could be trimmed safely.
    """
    missing = []
    for directory in LINK_CHECKED_DIRS:
        for doc in sorted((REPO_ROOT / directory).glob("*.md")):
            text = doc.read_text(encoding="utf-8")
            if "## Invariant" not in text or "## Why it looks like this" not in text:
                missing.append(str(doc.relative_to(REPO_ROOT)))
    assert not missing, (
        "canonical docs missing an '## Invariant' or "
        f"'## Why it looks like this' section: {missing}"
    )
