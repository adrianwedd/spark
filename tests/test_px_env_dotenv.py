"""`.env` is loaded once, by `bin/px-env`, for every launcher (#345, option 1).

The filed failure, reproduced live on the robot 2026-09-18:

    $ bin/px-spark --input-mode text --dry-run --max-turns 1
    [spark] persona activated
    You> [voice-loop] cognition tier bad_response: PX_M5_SPARK_MODEL must name
         a model explicitly (not auto)

The loop started, greeted, accepted input, and answered every turn with the
unavailable acknowledgement. The environment it needed was in `.env`, which only
the systemd units read via `EnvironmentFile=`. Nothing in the manual launch path
supplied it: `bin/px-env` set the venv, `PYTHONPATH` and `LOG_DIR`, and
`~/.profile` sourced nothing.

Option 1 (chosen): `bin/px-env` loads `.env` once, so every `bin/px-*` launcher
behaves like a unit. These tests drive the real script against a fixture project
rather than asserting on its text — the failure it fixes is an environment fact,
so the test has to observe an environment.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PX_ENV = ROOT / "bin" / "px-env"

PROBE = """#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/px-env"
for v in PX_M5_SPARK_MODEL PX_HA_TOKEN QUOTED EXPORTED_FORM CALLER_VALUE; do
    if [[ -n "${!v+set}" ]]; then
        echo "$v=${!v}"
    else
        echo "$v=<unset>"
    fi
done
"""

DOTENV = """# a comment line
PX_M5_SPARK_MODEL=deepseek-v4.1-flash:cloud
PX_HA_TOKEN=from-dotenv
QUOTED="a value with spaces"
export EXPORTED_FORM=yes
CALLER_VALUE=from-dotenv
"""


@pytest.fixture
def fixture_project(tmp_path):
    """A throwaway project root with the real px-env and a .env."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shutil.copy(PX_ENV, bin_dir / "px-env")
    (tmp_path / ".env").write_text(DOTENV, encoding="utf-8")
    (bin_dir / "probe").write_text(PROBE, encoding="utf-8")
    (bin_dir / "probe").chmod(0o755)
    return tmp_path


def _run_probe(project: Path, env: dict[str, str] | None = None) -> dict[str, str]:
    """Run the probe in a *clean* environment, as a fresh shell would."""
    base = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")}
    base.update(env or {})
    result = subprocess.run(
        [str(project / "bin" / "probe")],
        env=base, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"probe failed: {result.stderr}"
    out = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, _, val = line.partition("=")
            out[key] = val
    return out


def test_a_fresh_shell_gets_the_env_file(fixture_project):
    """The reported failure: the manual launcher had no PX_M5_SPARK_MODEL."""
    out = _run_probe(fixture_project)
    assert out["PX_M5_SPARK_MODEL"] == "deepseek-v4.1-flash:cloud"
    assert out["PX_HA_TOKEN"] == "from-dotenv"


def test_quoted_values_and_export_lines_survive(fixture_project):
    """`EnvironmentFile=` handles both; so must the shell path."""
    out = _run_probe(fixture_project)
    assert out["QUOTED"] == "a value with spaces"
    assert out["EXPORTED_FORM"] == "yes"


def test_a_value_the_caller_already_set_still_wins(fixture_project):
    """`env FOO=bar bin/px-x` must not be overridden by the file.

    systemd's own precedence is caller > EnvironmentFile, and the test suite
    depends on it (a test that sets PX_STATE_DIR must not be repointed by the
    developer's .env).
    """
    out = _run_probe(fixture_project, {"CALLER_VALUE": "from-caller"})
    assert out["CALLER_VALUE"] == "from-caller"


def test_the_opt_out_leaves_the_environment_untouched(fixture_project):
    """PX_ENV_SKIP_DOTENV=1 for a caller building its own environment."""
    out = _run_probe(fixture_project, {"PX_ENV_SKIP_DOTENV": "1"})
    assert out["PX_M5_SPARK_MODEL"] == "<unset>"
    assert out["CALLER_VALUE"] == "<unset>"


def test_a_host_with_no_env_file_is_not_an_error(fixture_project):
    """A fresh clone and CI have no .env; px-env must still succeed."""
    (fixture_project / ".env").unlink()
    out = _run_probe(fixture_project)
    assert out["PX_M5_SPARK_MODEL"] == "<unset>"


def test_the_real_repo_has_no_env_committed():
    """`.env` carries tokens — it must never be tracked.

    px-env happily sources it if present, which makes this the guard that
    matters: the file is local-only.
    """
    assert not (ROOT / ".env").exists() or subprocess.run(
        ["git", "check-ignore", "-q", ".env"], cwd=ROOT
    ).returncode == 0, ".env must be git-ignored"
