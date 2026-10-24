"""bin/px-units-install and its root launcher (#390, #440).

The installer is the other half of `bin/px-deploy-check`'s drift report, so the
property that matters is parity: after it runs, the gate must report nothing.
#390 sat open with a drop-in the installer did not cover, to be installed
"separately" — a gate whose fix needs a second, hand-typed step.
"""

import os
import re
import importlib.machinery
import importlib.util
import stat
import sys

import pytest
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


def test_real_systemd_ignored_key_refuses_before_copy(tmp_path):
    """Exercise actual Linux diagnostics, not just our analyzer test double."""
    import shutil
    if not shutil.which("systemd-analyze"):
        pytest.skip("systemd-analyze is unavailable on this host; Linux CI covers it")
    source = tmp_path / "source"
    units = source / "systemd"
    units.mkdir(parents=True)
    (units / "spark-installer-fixture.service").write_text(
        "[Unit]\nDescription=Offline installer fixture\n[Service]\n"
        "DefinitelyNotARealSystemdKey=yes\nExecStart=/usr/bin/true\n")
    dest = tmp_path / "destination"
    dest.mkdir()
    env = dict(os.environ, PROJECT_ROOT=str(source), PX_UNITS_DEST_DIR=str(dest))
    proc = subprocess.run(["bash", str(INSTALLER)], env=env,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "refusing" in proc.stderr
    assert not list(dest.iterdir())


def test_launcher_refuses_every_argument():
    # A sudoers command with no argument list permits any arguments.
    for args in (["--dry-run"], ["x"], ["", ""]):
        proc = subprocess.run([sys.executable, "-I", str(LAUNCHER), *args],
                              capture_output=True, text=True, timeout=30)
        assert proc.returncode == 2
        assert "takes no arguments" in proc.stderr


def load_launcher():
    loader = importlib.machinery.SourceFileLoader("units_root_launcher", str(LAUNCHER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_launcher_pins_trusted_inputs_and_scrubs_environment(monkeypatch):
    launcher = load_launcher()
    checked = []
    monkeypatch.setattr(launcher.os, "geteuid", lambda: 0)
    monkeypatch.setattr(launcher, "trusted_path", lambda path, **kw: checked.append(path))
    monkeypatch.setattr(Path, "rglob", lambda *args: iter(()))
    monkeypatch.setenv("PROJECT_ROOT", "/pi-writable/checkout")
    monkeypatch.setenv("BASH_ENV", "/pi-writable/evil.sh")
    monkeypatch.setenv("PX_UNITS_DEST_DIR", "/etc/sudoers.d")
    called = []
    def capture(program, argv, env):
        called.append((program, argv, env))
        raise RuntimeError("captured exec, no process launched")
    monkeypatch.setattr(launcher.os, "execve", capture)
    with pytest.raises(RuntimeError, match="captured exec"):
        launcher.main([])
    assert launcher.IMPLEMENTATION in checked
    assert launcher.SYSTEMD_SOURCE in checked
    assert called == [("/bin/bash", ["/bin/bash", "--noprofile", "--norc", "/usr/local/libexec/spark/px-units-install"],
                       {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "PROJECT_ROOT": "/usr/local/share/spark/units"})]
    assert LAUNCHER.read_text().splitlines()[0] == "#!/usr/bin/python3 -I"
    assert os.access(LAUNCHER, os.X_OK)


@pytest.mark.parametrize("mode,uid,bad_component", [
    (stat.S_IFLNK | 0o777, 0, "/trusted/units"),
    (stat.S_IFREG | 0o644, 1000, "/trusted/units"),
    (stat.S_IFREG | 0o664, 0, "/trusted/units"),
    (stat.S_IFDIR | 0o777, 0, "/trusted"),
    (stat.S_IFDIR | 0o755, 1000, "/trusted"),
    (stat.S_IFIFO | 0o600, 0, "/trusted/units"),
])
def test_trusted_inputs_refuse_writable_or_foreign_components(monkeypatch, mode, uid, bad_component):
    from types import SimpleNamespace
    launcher = load_launcher()
    def metadata(path):
        if str(path) == bad_component:
            return SimpleNamespace(st_mode=mode, st_uid=uid)
        return SimpleNamespace(st_mode=(stat.S_IFREG | 0o644) if str(path)=="/trusted/units" else (stat.S_IFDIR | 0o755), st_uid=0)
    monkeypatch.setattr(Path, "lstat", metadata)
    with pytest.raises(ValueError, match="untrusted"):
        launcher.trusted_path(Path("/trusted/units"))


def test_real_root_owned_file_is_accepted_without_executing_it():
    launcher = load_launcher()
    launcher.trusted_path(Path("/usr/bin/true"))


def test_refused_snapshot_never_executes(monkeypatch):
    launcher = load_launcher()
    monkeypatch.setattr(launcher.os, "geteuid", lambda: 0)
    monkeypatch.setattr(launcher, "trusted_path", lambda *a, **k: (_ for _ in ()).throw(ValueError("unsafe snapshot")))
    monkeypatch.setattr(launcher.os, "execve", lambda *a: pytest.fail("executed a refused snapshot"))
    assert launcher.main([]) == 1


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
