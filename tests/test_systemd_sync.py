from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SYNC = ROOT / "bin" / "px-systemd-sync"


def _run(*args: str) -> tuple[int, dict]:
    result = subprocess.run(
        [str(SYNC), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, json.loads(result.stdout)


def test_check_reports_missing_and_drifted_units(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "px-one.service").write_text("[Service]\nExecStart=/bin/true\n")
    (source / "px-two.timer").write_text("[Timer]\nOnBootSec=1\n")
    (target / "px-one.service").write_text("[Service]\nExecStart=/bin/false\n")
    (target / "px-tts-glados.service").write_text("[Service]\nExecStart=/bin/true\n")

    returncode, payload = _run("--source", str(source), "--target", str(target))

    assert returncode == 1
    assert payload["units"] == {
        "px-one.service": "drift",
        "px-two.timer": "missing",
    }
    assert payload["denied_installed"] == ["px-tts-glados.service"]


def test_apply_atomically_reconciles_child_profile(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    expected = "[Service]\nExecStart=/bin/true\n"
    (source / "px-one.service").write_text(expected)
    (target / "px-one.service").write_text("stale\n")
    (target / "px-tts-glados.service").write_text("adult\n")

    returncode, payload = _run(
        "--apply",
        "--no-systemctl",
        "--source",
        str(source),
        "--target",
        str(target),
    )

    assert returncode == 0
    assert payload["status"] == "ok"
    assert payload["changed"] == ["px-one.service"]
    assert payload["removed"] == ["px-tts-glados.service"]
    assert payload["restart_required"] == ["px-one.service"]
    assert (target / "px-one.service").read_text() == expected
    assert not (target / "px-tts-glados.service").exists()
