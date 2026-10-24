"""The real shared launcher refuses HAT calls before their executable body."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('name', ['tool-look', 'tool-emote', 'tool-sonar', 'tool-drive',
    'tool-perform', 'px-look', 'px-emote', 'px-sonar', 'px-drive', 'px-scan', 'px-race'])
def test_hold_stops_direct_and_wrapped_hardware(tmp_path, name):
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    shutil.copy(ROOT / 'bin/px-env', bindir / 'px-env')
    (tmp_path / 'state').mkdir()
    marker = tmp_path / 'state/robot-hat-disabled'
    marker.touch()
    probe = bindir / name
    probe.write_text('#!/bin/bash\nsource "$(dirname "$0")/px-env"\necho BODY_REACHED\n')
    env = {'PATH': os.environ['PATH'], 'HOME': str(tmp_path)}
    result = subprocess.run(['bash', str(probe)], env=env, capture_output=True, text=True)
    assert result.returncode == 0
    assert json.loads(result.stdout)['reason'] == 'robot_hat_disabled'
    marker.unlink()
    result = subprocess.run(['bash', str(probe)], env=env, capture_output=True, text=True)
    assert result.returncode == 0
    assert 'BODY_REACHED' in result.stdout


@pytest.mark.parametrize('name', ['tool-voice', 'px-wake-listen', 'px-frigate-stream', 'px-mind', 'px-stop'])
def test_hold_leaves_working_paths_and_emergency_stop_available(tmp_path, name):
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    shutil.copy(ROOT / 'bin/px-env', bindir / 'px-env')
    (tmp_path / 'state').mkdir()
    (tmp_path / 'state/robot-hat-disabled').touch()
    probe = bindir / name
    probe.write_text('#!/bin/bash\nsource "$(dirname "$0")/px-env"\necho BODY_REACHED\n')
    result = subprocess.run(['bash', str(probe)], env={'PATH': os.environ['PATH'], 'HOME': str(tmp_path)}, capture_output=True, text=True)
    assert result.returncode == 0
    assert 'BODY_REACHED' in result.stdout
