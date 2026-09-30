"""bin/px-units-install and its root launcher (#390, #440).

The installer is the other half of `bin/px-deploy-check`'s drift report, so the
property that matters is parity: after it runs, the gate must report nothing.
#390 sat open with a drop-in the installer did not cover, to be installed
"separately" — a gate whose fix needs a second, hand-typed step.
"""

import os
import re
import subprocess
from pathlib import Path

from pxh import deploy

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = PROJECT_ROOT / "bin" / "px-units-install"
LAUNCHER = PROJECT_ROOT / "systemd" / "sbin" / "px-units-install"
SUDOERS = PROJECT_ROOT / "systemd" / "sudoers.d" / "picar-x-services"


def _fake_analyzer(tmp_path, body="exit 0"):
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    tool = bindir / "systemd-analyze"
    tool.write_text(f"#!/bin/bash\n{body}\n")
    tool.chmod(0o755)
    return bindir


def _run(tmp_path, *args, analyzer="exit 0"):
    dest = tmp_path / "etc-systemd-system"
    dest.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["PX_UNITS_DEST_DIR"] = str(dest)
    env["PATH"] = f"{_fake_analyzer(tmp_path, analyzer)}:{env['PATH']}"
    env.pop("PROJECT_ROOT", None)
    proc = subprocess.run(["bash", str(INSTALLER), *args], env=env,
                          capture_output=True, text=True, timeout=60)
    return proc, dest


def test_dry_run_lists_drop_ins_and_writes_nothing(tmp_path):
    proc, dest = _run(tmp_path, "--dry-run")
    assert proc.returncode == 2, proc.stderr
    assert "px-frigate-stream.service.d/10-containment.conf" in proc.stdout
    assert not any(dest.iterdir())


def test_install_leaves_the_gate_nothing_to_report(tmp_path):
    proc, dest = _run(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    drift = [d for unit in deploy.repo_unit_names(str(PROJECT_ROOT))
             for d in deploy.unit_file_drift(unit, root=str(PROJECT_ROOT),
                                             install_dir=str(dest))]
    assert drift == []
    # Idempotent: a second pass has nothing to do.
    again, _ = _run(tmp_path)
    assert again.returncode == 0 and "nothing to do" in again.stdout


def test_a_verify_warning_on_a_repo_file_refuses_the_install(tmp_path):
    # `systemd-analyze verify` exits 0 on "Unknown key … ignoring" — #436's
    # misplaced restart limits — so the exit code alone cannot be the gate.
    warn = ('echo "$2:13: Unknown key \'StartLimitIntervalSec\' in section '
            '[Service], ignoring." >&2; exit 0')
    proc, dest = _run(tmp_path, analyzer=warn)
    assert proc.returncode == 1
    assert "refusing" in proc.stderr and "Unknown key" in proc.stderr
    assert not any(dest.iterdir())


def test_a_warning_about_an_installed_unit_does_not_block(tmp_path):
    # Stale *installed* units are what the install replaces; their warnings
    # must not stop it.
    warn = ('echo "/etc/systemd/system/px-blog.service:13: Unknown key" >&2; '
            'exit 0')
    proc, _ = _run(tmp_path, analyzer=warn)
    assert proc.returncode == 0, proc.stderr


def test_launcher_refuses_every_argument():
    # A sudoers command with no argument list permits any arguments.
    for args in (["--dry-run"], ["x"], ["", ""]):
        proc = subprocess.run(["bash", str(LAUNCHER), *args],
                              capture_output=True, text=True, timeout=30)
        assert proc.returncode == 2
        assert "takes no arguments" in proc.stderr


def test_launcher_pins_repo_and_scrubs_environment():
    text = LAUNCHER.read_text(encoding="utf-8")
    assert os.access(LAUNCHER, os.X_OK)
    assert "REPO=/home/pi/picar-x-hacking" in text
    assert re.search(r"exec /usr/bin/env -i ", text)


def test_every_sbin_grant_has_a_tracked_launcher_source():
    """#440 granted /usr/local/sbin/px-units-install with no source in the repo,
    so the grant could never be installed as written."""
    granted = set(re.findall(r"/usr/local/sbin/([A-Za-z0-9_-]+)",
                             "\n".join(line for line in
                                       SUDOERS.read_text().splitlines()
                                       if not line.lstrip().startswith("#"))))
    assert granted
    missing = sorted(n for n in granted
                     if not (PROJECT_ROOT / "systemd" / "sbin" / n).is_file())
    assert not missing, f"sudoers grants launchers with no source: {missing}"
