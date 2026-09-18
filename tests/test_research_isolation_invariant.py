"""Delegated-research OS isolation invariant (issue #281 phase 2).

Phase 1's boundary is a tool allowlist and is pinned by
tools/check_investigator_agent.py. Phase 2's boundary is a set of unit
properties and one sudoers line, and is pinned here — including the canaries,
because a checker that has never been shown a violation is a checker nobody
can trust.
"""
from __future__ import annotations

import os
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
    root = _tamper(tmp_path, CANARY, '"$(id -u)" -eq 0 ]] || die', "true")
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
    assert "phase 0" in text, (
        "the credential-coverage check is what makes a missing PX_M5_SPARK_HOST "
        "or TIMEOUT_S a failure instead of a silent difference from production")
    assert "comm -23" in text and "TIER_ENV" in text
    assert "getent passwd spark-research" in text, (
        "the release check is the one that proves DynamicUser does not leave a "
        "standing identity behind")
    assert "px-research-run" in text, "the real path is what phase 2 must exercise"


def test_the_canary_dry_run_needs_no_root_and_renders_what_it_would_do():
    """Run rather than read: a 300-line script whose first execution happens at
    the acceptance step is a script whose argument construction has never been
    executed. `--dry-run` is that half, and it must work as an ordinary user —
    on this repo's CI runner the install is absent, which is itself a case it
    has to report rather than crash on."""
    import subprocess

    proc = subprocess.run(
        ["bash", str(REPO_ROOT / CANARY), "--dry-run"],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    out = proc.stdout + proc.stderr
    assert "must run as root" not in out, "the dry run must not require root"
    assert proc.returncode in (0, 2), out

    launcher_props = guard._property_lines(
        guard._code_only((REPO_ROOT / guard.LAUNCHER).read_text()))
    assert f"properties ({len(launcher_props)})" in proc.stdout

    for phase in ("phase 0 would check", "phase 1 would run", "phase 2 would run",
                  "phase 3 would run", "phase 4 would check"):
        assert phase in proc.stdout, f"{phase!r} missing from the dry run"
    # The probe and the release check are the two programs the phases exec.
    assert "-- /bin/bash" in proc.stdout
    assert "-- /bin/sleep 120" in proc.stdout
    # And the real path is driven the way an operator drives it, not as root:
    # as root it would pass with a broken sudoers grant.
    assert "/usr/sbin/runuser -u pi -- sudo -n /usr/local/sbin/px-research-run" in proc.stdout
    if proc.returncode == 2:
        assert "the install is incomplete" in out


def test_the_canary_drives_the_real_path_through_the_grant():
    text = (REPO_ROOT / CANARY).read_text()
    assert "/usr/sbin/runuser -u pi -- sudo -n" in text, (
        "phase 2 must exercise the documented invocation (pi -> sudo -n -> "
        "launcher); running the launcher as root would pass with a broken grant")
    assert "getent passwd spark-research" in text


def _dry_run_missing(path_env: str) -> set:
    """The `  - ` lines the dry run reports, with a given PATH."""
    import subprocess

    proc = subprocess.run(
        ["bash", str(REPO_ROOT / CANARY), "--dry-run"],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
        env={**os.environ, "PATH": path_env},
    )
    return {line.strip()[2:] for line in proc.stdout.splitlines()
            if line.startswith("  - ")}


def test_the_dry_run_does_not_depend_on_the_callers_path():
    """Found by running it on the robot: `/usr/sbin/runuser` is present there but
    `/usr/sbin` is not on a non-login shell's PATH, so a `command -v` check
    reported a piece of the install as missing that was there. A false
    "missing" on the acceptance step is worse than no check — it costs a root
    run and a re-read. Every binary this script names is absolute now, and this
    pins that: the report must be identical under a restricted PATH."""
    assert _dry_run_missing("/usr/bin:/bin") == _dry_run_missing(os.environ.get("PATH", ""))


# ---------------------------------------------------------------------------
# The probe's own machinery (#281 phase 2)
# ---------------------------------------------------------------------------
#
# The probe's first execution used to be the acceptance step: once, as root,
# under systemd-run, with no way to tell a bash mistake from a violated claim.
# `--self-test` proves the assertions can fail; `run-probe-rehearsal.sh` runs the
# whole probe inside a sandbox and asserts its verdict profile. Both found bugs
# in the probe before the authorised run, which is the point of having them.

def _run(bash_file, *args, cwd=None, env=None):
    import os
    import subprocess

    return subprocess.run(
        ["bash", str(bash_file), *args], capture_output=True, text=True,
        cwd=str(cwd or REPO_ROOT), env={**os.environ, **(env or {})},
    )


def test_the_probe_self_test_passes_and_reports_both_directions():
    proc = _run(REPO_ROOT / PROBE, "--self-test")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PASS" in proc.stdout and "FAIL" in proc.stdout
    assert "the assertion helpers can fail, and do" in proc.stdout


def test_the_probe_self_test_detects_broken_machinery(tmp_path):
    """Tamper the helper so it can no longer fail a command: the self-test must
    notice, because a canary that cannot fail is not a canary."""
    text = (REPO_ROOT / PROBE).read_text()
    broken = text.replace('    if out=$("$@" 2>&1); then', "    if false; then", 1)
    assert broken != text
    copy = tmp_path / "canary-probe.sh"
    copy.write_text(broken)

    proc = _run(copy, "--self-test")

    assert proc.returncode != 0, proc.stdout
    assert "would pass claims it should not" in proc.stdout


def test_the_probe_counts_failures_rather_than_flagging_them():
    """Found by `run-probe-rehearsal.sh` on its first run: `record` *assigned*
    `fail=1` rather than counting, so a run with two violated claims exited 1
    and recorded `"failures": 1` — while the canary's own message and the
    rehearsal both read that number as a count."""
    text = (REPO_ROOT / PROBE).read_text()
    assert "fail_count=$((fail_count + 1))" in text
    assert 'exit "$fail_count"' in text
    assert '"failures": $fail_count' in text or '"failures": %s' in text
    assert "[[ \"$1\" == \"FAIL\" ]] && fail=1" not in text


def test_the_rehearsal_expects_both_demonstrable_failures():
    """Claim 14 (the credential file the install creates) and claim 18 (the
    invoking environment, which bwrap passes through) must be *expected* to fail
    in the bwrap rehearsal — that is what makes them non-vacuous there."""
    text = (REPO_ROOT / "tools/prototypes/agent-os-isolation/run-probe-rehearsal.sh").read_text()
    assert '"$failed_claims" == "14 18 "' in text
    assert "PX_CANARY_LEAK_PROBE=" in text, (
        "the rehearsal must export the leak probe variable, or claim 18 passes "
        "vacuously and the rehearsal proves nothing about it")
    assert "16 PASS, 2 FAIL" in text
    assert 'PX_REHEARSAL_PROBE' in text
