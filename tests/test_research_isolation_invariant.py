"""Delegated-research OS isolation invariant (issue #281 phase 2).

Phase 1's boundary is a tool allowlist and is pinned by
tools/check_investigator_agent.py. Phase 2's boundary is a set of unit
properties and one sudoers line, and is pinned here — including the canaries,
because a checker that has never been shown a violation is a checker nobody
can trust.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import check_research_isolation as guard  # noqa: E402

ARTIFACTS = (
    Path("systemd/sbin/px-research-run"),
    Path("src/pxh/research_worker.py"),
    Path("systemd/sudoers.d/picar-x-services"),
    Path("tools/prototypes/agent-os-isolation/canary-real-uid.sh"),
    Path("tools/prototypes/agent-os-isolation/canary-probe.sh"),
)
CANARY = ARTIFACTS[3]
PROBE = ARTIFACTS[4]


def _fake_repo(tmp_path) -> Path:
    for rel in ARTIFACTS:
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / rel, dest)
    return tmp_path


def test_the_shipped_artifacts_are_closed():
    violations = guard.check()
    assert violations == [], "\n".join(violations)


def test_the_report_names_every_guarantee_it_checks():
    report = "\n".join(guard.report())
    for prop in guard.REQUIRED_PROPERTIES:
        assert prop in report, f"{prop} is not in the map this checker prints"


def _tamper(tmp_path, rel: Path, old: str, new: str) -> Path:
    root = _fake_repo(tmp_path)
    path = root / rel
    text = path.read_text()
    assert old in text, f"canary anchor {old!r} is gone from {rel}"
    path.write_text(text.replace(old, new, 1))
    return root


def test_a_dropped_property_is_detected(tmp_path):
    root = _tamper(tmp_path, ARTIFACTS[0],
                   "    --property=PrivateDevices=yes \\\n", "")
    violations = guard.check(root)
    assert any("PrivateDevices=yes" in v for v in violations), violations


def test_a_caller_parameterised_property_is_detected(tmp_path):
    """The one that matters most: a sandbox property that a caller can shape."""
    root = _tamper(
        tmp_path, ARTIFACTS[0],
        '    --property=RuntimeDirectoryMode=0700 \\\n',
        '    --property=RuntimeDirectoryMode=0700 \\\n'
        '    --property=ReadWritePaths="$1" \\\n',
    )
    violations = guard.check(root)
    assert any("property is built from caller input" in v for v in violations), violations


def test_a_secret_shaped_env_property_is_detected(tmp_path):
    root = _tamper(
        tmp_path, ARTIFACTS[0],
        '    --property=Environment=HOME=/run/px-research \\\n',
        '    --property=Environment=HOME=/run/px-research \\\n'
        '    --setenv=OLLAMA_API_KEY=inline \\\n',
    )
    violations = guard.check(root)
    assert any("--setenv" in v for v in violations), violations


def test_a_weakened_uuid_check_is_detected(tmp_path):
    root = _tamper(
        tmp_path, ARTIFACTS[0],
        '[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}',
        '[0-9a-f-]+',
    )
    violations = guard.check(root)
    assert any("uuid validation" in v for v in violations), violations


def test_an_extra_argument_being_accepted_is_detected(tmp_path):
    root = _tamper(tmp_path, ARTIFACTS[0], "[[ $# -eq 1 ]]", "[[ $# -ge 1 ]]")
    violations = guard.check(root)
    assert any("exactly one argument" in v for v in violations), violations


def test_a_widened_sudoers_grant_is_detected(tmp_path):
    root = _tamper(
        tmp_path, ARTIFACTS[2],
        "pi ALL=(root) NOPASSWD: /usr/local/sbin/px-research-run *",
        "pi ALL=(root) NOPASSWD: /usr/local/sbin/px-research-run * , ALL",
    )
    violations = guard.check(root)
    assert any("more than the launcher" in v or "exactly" in v
               for v in violations), violations


def test_a_second_writer_in_the_worker_is_detected(tmp_path):
    root = _tamper(
        tmp_path, ARTIFACTS[1],
        '    dest = out / f"{uuid}.json"',
        '    (out / "index.txt").write_text("x")\n    dest = out / f"{uuid}.json"',
    )
    violations = guard.check(root)
    assert any("write_text" in v for v in violations), violations


def test_a_missing_launcher_is_detected(tmp_path):
    root = _fake_repo(tmp_path)
    (root / ARTIFACTS[0]).unlink()
    assert guard.check(root)


@pytest.mark.parametrize("needle", ["subprocess", "os.system"])
def test_a_shell_reaching_worker_is_detected(tmp_path, needle):
    root = _tamper(tmp_path, ARTIFACTS[1], "import json",
                   f"import json\nimport {needle}  # noqa")
    violations = guard.check(root)
    assert any(needle in v for v in violations), violations


def test_the_bin_entry_point_execs_the_module_without_px_env():
    """The unit execs `bin/px-research-worker`, which must reach the module
    without touching bin/px-env — px-env exports LOG_DIR and the production
    environment, and this process is meant to have neither."""
    text = (REPO_ROOT / "bin" / "px-research-worker").read_text()
    assert "-m pxh.research_worker" in text
    assert not [line for line in guard._code_only(text).splitlines()
                if "px-env" in line], "the worker must not source bin/px-env"


# ---------------------------------------------------------------------------
# The canary must test the sandbox that ships (#281 phase 2)
# ---------------------------------------------------------------------------
#
# The real path execs `bin/px-research-worker` and nothing else — no shell, no
# probes — so the property set has to be canaried by a root-side systemd-run
# with the *same* properties and a different program. That identity is the
# load-bearing claim, and two lists in two files drift unless something compares
# them. These are the canaries for the comparison itself.

def test_the_canary_uses_the_launchers_property_set():
    launcher = guard._property_lines(guard._code_only((REPO_ROOT / guard.LAUNCHER).read_text()))
    canary = guard._property_lines(guard._code_only((REPO_ROOT / CANARY).read_text()))
    assert sorted(canary) == sorted(launcher)
    assert len(launcher) >= 25, "a property list this short is not the design's"


def test_a_canary_that_drops_a_property_is_detected(tmp_path):
    root = _tamper(tmp_path, CANARY, "    --property=ProtectHome=yes \\\n", "")
    violations = guard.check(root)
    assert any("differs from" in v for v in violations), violations


def test_a_canary_that_adds_a_property_is_detected(tmp_path):
    root = _tamper(
        tmp_path, CANARY,
        "    --property=PrivateTmp=yes \\\n",
        "    --property=PrivateTmp=yes \\\n    --property=ReadWritePaths=/tmp \\\n",
    )
    violations = guard.check(root)
    assert any("differs from" in v for v in violations), violations


def test_a_canary_without_a_root_guard_is_detected(tmp_path):
    root = _tamper(tmp_path, CANARY, '[[ "$(id -u)" -eq 0 ]] || die', "true || die")
    violations = guard.check(root)
    assert any("root guard" in v for v in violations), violations


def test_a_canary_that_names_another_unit_is_detected(tmp_path):
    root = _tamper(tmp_path, CANARY, '--unit="px-canary-$u1"', '--unit="px-mind"')
    violations = guard.check(root)
    assert any("px-canary-*" in v for v in violations), violations


def test_a_probe_that_targets_a_real_unit_is_detected(tmp_path):
    root = _tamper(tmp_path, PROBE, "px-canary-nonexistent.service", "px-alive.service")
    violations = guard.check(root)
    assert any("nonexistent" in v for v in violations), violations


def test_a_missing_canary_is_detected(tmp_path):
    root = _fake_repo(tmp_path)
    (root / CANARY).unlink()
    violations = guard.check(root)
    assert any("no executable acceptance artifact" in v for v in violations), violations


def test_the_canary_declares_the_three_phases_it_claims():
    """Properties, the real path, and release — named in the file so a future
    edit that drops one has to delete a comment that says what it was for."""
    text = (REPO_ROOT / CANARY).read_text()
    assert "phase 1" in text and "phase 2" in text and "phase 3" in text
    assert "getent passwd spark-research" in text, (
        "the release check is the one that proves DynamicUser does not leave a "
        "standing identity behind")
    assert "px-research-run" in text, "the real path is what phase 2 must exercise"
