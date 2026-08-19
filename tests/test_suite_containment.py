"""The suite must not be able to act on the robot it is running on.

This file is the containment boundary itself, not a test of a feature. Every
test here fails the moment an ordinary `python -m pytest` regains the ability
to drive hardware or the service manager, which is what happened on
2026-08-19: a bare run executed the `live` tests and reached
`sudo systemctl stop px-alive` through the API's service-control endpoint.

Two independent mechanisms are pinned here, and they are deliberately not the
same mechanism:

- **Marker deselection** (`addopts = -m "not live"`) keeps the tests that
  *announce* they need hardware out of the default run. It is necessary and
  entirely insufficient: it only covers tests somebody remembered to mark.
- **The destructive-boundary guard** (`_refuse_the_destructive_boundary` in
  conftest) refuses the privileged OS call itself, whether or not anybody
  marked the test. This is the layer that catches the test nobody classified.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

CANARY = """\
import pytest


def test_hermetic_canary():
    pass


@pytest.mark.live
def test_live_canary():
    pass
"""


def _synthetic_project(tmp_path: Path) -> Path:
    """A throwaway project carrying this repo's real pytest configuration.

    Copying `pyproject.toml` rather than pointing pytest at the repo means the
    canary proves what the *configuration* does, without the run inheriting
    this repo's conftest — and without adding a permanently-deselected file to
    `tests/`, which would then need its own explanation forever.
    """
    shutil.copy(ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_canary.py").write_text(CANARY, encoding="utf-8")
    return tmp_path


def _collect(project: Path, *extra: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *extra],
        cwd=project, capture_output=True, text=True, timeout=120,
    )
    return result.stdout + result.stderr


# ── Marker deselection ──────────────────────────────────────────────────────


def test_the_default_invocation_does_not_select_a_live_marked_test(tmp_path):
    """Bare `python -m pytest` must not collect a newly marked `live` test.

    Written against a canary rather than against the existing live files
    because the property under test is "a test marked *today* is contained",
    not "these three known files are contained".
    """
    out = _collect(_synthetic_project(tmp_path))
    assert "test_hermetic_canary" in out, f"the canary project did not run:\n{out}"
    assert "test_live_canary" not in out, (
        "an ordinary pytest run selected a live-marked test — the default "
        f"invocation is not contained:\n{out}"
    )


def test_explicit_m_live_still_selects_it(tmp_path):
    """Containment must not cost the ability to run live tests deliberately."""
    out = _collect(_synthetic_project(tmp_path), "-m", "live")
    assert "test_live_canary" in out, (
        f"-m live no longer selects live tests; the opt-in is broken:\n{out}"
    )
    assert "test_hermetic_canary" not in out


def test_live_or_not_live_still_selects_everything(tmp_path):
    """The documented spelling for a genuinely complete run."""
    out = _collect(_synthetic_project(tmp_path), "-m", "live or not live")
    assert "test_live_canary" in out and "test_hermetic_canary" in out, out


def test_pyproject_deselects_live_by_default():
    """A fast tripwire on the configuration itself.

    The canary tests above are the real proof, but they cost two pytest
    start-ups. This one fails in milliseconds if somebody deletes `addopts`,
    so the reason for the deletion gets discussed rather than discovered.
    """
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    addopts = config["tool"]["pytest"]["ini_options"]["addopts"]
    assert "not live" in " ".join(addopts), (
        f"pyproject no longer deselects live tests by default: {addopts!r}"
    )


# ── The destructive-boundary guard ──────────────────────────────────────────
#
# Every argv below is privileged in its *name* and inert in its *arguments*:
# a canary unit that does not exist, or `--help`. That is deliberate and it is
# load-bearing. These tests assert a refusal, so they only stay harmless while
# the guard works — and the first RED run of this file, before the guard
# existed, executed `sudo systemctl stop px-alive` for real and stopped the
# live daemon at 12:47:36 on 2026-08-19. A test that verifies containment must
# not itself depend on containment. Never put a real service name, `reboot`,
# or `shutdown -h now` in this section.

CANARY_UNIT = "px-canary-not-a-real-unit"


def test_sudo_systemctl_is_refused_before_it_reaches_the_os(refused_privileged_commands):
    """The call shape that stopped px-alive during the 2026-08-19 run.

    `PX_DRY` is not part of this: the point is that the boundary holds even
    where nothing downstream honours a dry flag, which is precisely the case
    for `pxh.api._run_systemctl`.
    """
    argv = ["sudo", "systemctl", "stop", CANARY_UNIT]
    with pytest.raises(RuntimeError) as excinfo:
        subprocess.run(argv, capture_output=True)

    assert "live robot" in str(excinfo.value)
    assert argv in refused_privileged_commands


@pytest.mark.parametrize("argv", [
    ["systemctl", "restart", CANARY_UNIT],
    ["reboot", "--help"],
    ["shutdown", "--help"],
    ["/sbin/shutdown", "--help"],
    ["sudo", "-n", "systemctl", "start", CANARY_UNIT],
    ["runuser", "--version"],
    ["pkill", "--version"],
])
def test_every_privileged_spelling_is_refused(argv, refused_privileged_commands):
    """Bare names, absolute paths and sudo-wrapped forms all resolve the same.

    Listed explicitly rather than trusting one representative, because each
    spelling appears somewhere in `src/pxh` today: api.py uses both
    `["sudo", "systemctl", ...]` and `["sudo", "/usr/bin/systemctl", ...]`,
    mind.py uses `["sudo", "-n", "systemctl", ...]` and
    `["sudo", "shutdown", ...]`, vision.py uses `runuser`.
    """
    with pytest.raises(RuntimeError):
        subprocess.Popen(argv)
    assert argv in refused_privileged_commands


def test_os_system_is_refused_too(refused_privileged_commands):
    """`os.system` bypasses Popen entirely, so it needs its own guard.

    The `os.system` and `shell=True` calls in this file are fixed literals and
    are asserted to be *refused* — they are the guard's negative controls, not
    an injection sink. Do not "fix" them into list-form subprocess calls; that
    deletes the coverage.
    """
    with pytest.raises(RuntimeError):
        os.system(f"sudo systemctl stop {CANARY_UNIT}")
    assert refused_privileged_commands


def test_a_shell_string_is_inspected_not_waved_through():
    """`shell=True` hands Popen a string; the guard must still read argv[0]."""
    with pytest.raises(RuntimeError):
        subprocess.run(f"sudo systemctl stop {CANARY_UNIT}", shell=True)


def test_a_stub_on_path_is_not_refused(tmp_path):
    """A test that installs its own fake `sudo` is testing argv, not the OS.

    `tests/test_tools.py` does exactly this to assert `HOME` is threaded
    through tool-wander's sudo env. Refusing by *name* would break it and
    teach people that the guard is noise; refusing by *resolved path* keeps
    the distinction the guard exists to make.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "sudo"
    stub.write_text("#!/usr/bin/env bash\necho stubbed\n")
    stub.chmod(0o755)

    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"}
    result = subprocess.run(["sudo", "whatever"], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.stdout.strip() == "stubbed"


def test_an_unresolvable_privileged_name_still_fails_closed(tmp_path):
    """Unknown resolves the way known-dangerous does (CLAUDE.md invariant 6).

    If `sudo` is not on PATH the call would have failed anyway — but it must
    fail as a refusal, so an empty PATH can never become a way to launder one.
    """
    with pytest.raises(RuntimeError):
        subprocess.run(["sudo", "systemctl", "stop", CANARY_UNIT],
                       env={"PATH": str(tmp_path)})


def test_ordinary_subprocesses_are_untouched():
    """The guard must be invisible to the ~1400 tests that legitimately spawn."""
    result = subprocess.run([sys.executable, "-c", "print('fine')"],
                            capture_output=True, text=True, timeout=30)
    assert result.stdout.strip() == "fine"
