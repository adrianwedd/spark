"""Constitutional suite: resident-only Claude.

    No production code may invoke Claude non-residently.

BLACKLISTED from px-evolve (see pxh.claude_session.BLACKLIST_FILES) together
with tools/check_resident_claude.py, on the same footing as the policy
invariants: an evolution PR must not be able to relax the rule by editing the
test that pins it, nor by defanging the scanner the test calls.

Two halves, and both are load-bearing:

  test_no_cold_claude_in_production   the invariant itself. Red until every
                                      production cold-start is gone; that red
                                      is the debt map, not a broken test.

  the canary tests                    prove the detectors still fire. Without
                                      them the suite would go green the moment
                                      someone commented out a regex, and the
                                      first half would cheerfully agree that
                                      the repo is clean.

The canaries build synthetic trees under tmp_path rather than committing files
containing forbidden syntax, so the repo never carries a `claude -p` that has
to be exempted by name.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import check_resident_claude as guard  # noqa: E402


# ── The invariant ──────────────────────────────────────────────────────────

def test_no_cold_claude_in_production():
    """No production path may cold-start Claude.

    The resident spark-brain / spark-io sessions are the sole Claude execution
    substrate. A cold `claude -p` discards context per call, cannot use SPARK's
    tools, is unmetered, and costs more to run than the resident session it
    purports to rescue — so a resident-brain failure that spawns one amplifies
    the contention that caused the failure instead of degrading.
    """
    violations = guard.scan(REPO_ROOT)
    assert violations == [], (
        f"{len(violations)} cold-start Claude call path(s) in production code:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


# ── Canaries: the detectors must still fire ────────────────────────────────

def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return tmp_path


def test_canary_shell_exec(tmp_path):
    root = _tree(tmp_path, {"bin/thing": '#!/usr/bin/env bash\nexec claude -p "$PROMPT"\n'})
    assert [v.kind for v in guard.scan(root)] == ["shell_exec"]


def test_canary_python_argv(tmp_path):
    root = _tree(tmp_path, {
        "src/pxh/thing.py": 'import subprocess\nsubprocess.run([claude_bin(), "-p", prompt])\n',
    })
    assert [v.kind for v in guard.scan(root)] == ["argv_list"]


def test_canary_polyglot_bash_wrapping_python(tmp_path):
    """The seam the first draft of this guard fell through.

    bin/px-post, bin/px-blog and bin/px-cron-say all carry a bash shebang and
    embedded Python. Parsed as shell they show no command; parsed as Python
    they do not parse at all.
    """
    root = _tree(tmp_path, {
        "bin/thing": '#!/usr/bin/env bash\npython3 - <<EOF\nrun([claude_bin, "-p", prompt])\nEOF\n',
    })
    assert [v.kind for v in guard.scan(root)] == ["argv_list"]


def test_canary_forbidden_helper_call(tmp_path):
    """Calling a cold-start helper is a violation even with no argv list here.

    A fallback ladder three modules deep is still a cold start.
    """
    root = _tree(tmp_path, {"src/pxh/thing.py": "def f():\n    return call_claude_haiku(p, s)\n"})
    assert [v.kind for v in guard.scan(root)] == ["forbidden_helper"]


def test_canary_bridge_reference(tmp_path):
    """Wiring a caller at the fossil re-arms it as architecture."""
    root = _tree(tmp_path, {"bin/launcher": '#!/usr/bin/env bash\nexport CODEX_CHAT_CMD="$DIR/claude-voice-bridge"\n'})
    assert [v.kind for v in guard.scan(root)] == ["bridge_reference"]


def test_canary_bridge_existence(tmp_path):
    """The fossil is deleted, not deprecated: its presence alone is the finding."""
    root = _tree(tmp_path, {"bin/claude-voice-bridge": "#!/usr/bin/env bash\necho hi\n"})
    assert [v.kind for v in guard.scan(root)] == ["bridge_reference"]


# ── Negative canaries: the guard must not cry wolf ─────────────────────────

@pytest.mark.parametrize("rel,body", [
    # espeak's -p is pitch. tmux's -p is print. Neither is Claude.
    ("src/pxh/speech.py", 'run(["espeak", "-v", v, "-p", pitch, "--stdout", text])\n'),
    ("src/pxh/panes.py", 'out = _tmux("capture-pane", "-t", name, "-p")\n'),
    # Prose describing the ban must not trip it.
    ("src/pxh/brain.py", '"""This module is the replacement for `claude -p`."""\n'),
    # A resident launch has no -p and is the thing we want people to use.
    ("bin/px-session", '#!/usr/bin/env bash\nexec "$CLAUDE_BIN" --model "$M"\n'),
])
def test_negative_canaries(tmp_path, rel, body):
    assert guard.scan(_tree(tmp_path, {rel: body})) == []


def test_allowlist_is_narrow():
    """The exemption list is an attack surface; keep it to the launcher."""
    assert set(guard.ALLOWLIST) == {"bin/px-claude-session"}
