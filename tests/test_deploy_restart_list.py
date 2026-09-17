"""The deploy-time restart rule (#332).

The incident this pins: a deploy renamed `pxh.claude_session` to
`pxh.model_session` and the ff-merge succeeded, but three daemons kept executing
the *old* module name from memory and failed hours later inside a lazy import —
`px-blog`'s 22:00 post, `px-evolve`'s evolution request, and the API server's
budget fields. Nothing failed at deploy time, because nothing restarted them.

These tests assert the derivation, not the host: given a changed-file list and
the units' start times, who must restart.
"""

from __future__ import annotations

import os

import pytest

from pxh.deploy import (
    UnitState,
    executed_paths,
    import_closure,
    imported_pxh_modules,
    needs_restart,
    restart_list,
)

DEPLOY_TS = 1_758_010_264.0  # 2026-09-16 20:51:04 +10:00, the #332 deploy


def _tree(tmp_path, entries: dict[str, str], modules: dict[str, str]):
    """Build the smallest repo that has the shape of the real one."""
    (tmp_path / "bin").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "pxh").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "pxh" / "__init__.py").write_text("")
    for name, body in entries.items():
        path = tmp_path / "bin" / name
        path.write_text(body)
        os.chmod(path, 0o755)
    for name, body in modules.items():
        (tmp_path / "src" / "pxh" / f"{name}.py").write_text(body)
    return tmp_path


@pytest.fixture
def repo(tmp_path):
    return _tree(
        tmp_path,
        entries={
            "px-changed": "#!/usr/bin/env python3\nfrom pxh.alpha import one\n",
            "px-clean": "#!/usr/bin/env python3\nfrom pxh.gamma import two\n",
            "px-nopxh": "#!/usr/bin/env python3\nimport json\n",
        },
        modules={
            "alpha": "from pxh.beta import three\n\n\ndef one():\n    return three()\n",
            "beta": "def three():\n    return 3\n",
            "gamma": "def two():\n    return 2\n",
            "delta": "def four():\n    return 4\n",
        },
    )


def _unit(name, entry_rel, started_ts):
    return UnitState(name=name, entry=str(entry_rel), started_ts=started_ts)


def test_lazy_import_inside_a_function_is_still_a_dependency(repo):
    """The #332 shape: the import is at call time, so the *name* is what matters."""
    entry = repo / "bin" / "px-lazy"
    entry.write_text(
        "#!/usr/bin/env python3\n"
        "def generate():\n"
        "    from pxh.alpha import one\n"
        "    return one()\n"
    )
    assert "alpha" in imported_pxh_modules(str(entry))


def test_importlib_import_module_is_seen(repo):
    entry = repo / "bin" / "px-dynamic"
    entry.write_text(
        "#!/usr/bin/env python3\n"
        "import importlib\n"
        "def go():\n"
        "    mod = importlib.import_module('pxh.beta')\n"
        "    return mod\n"
    )
    assert "beta" in imported_pxh_modules(str(entry))


def test_closure_is_transitive(repo):
    assert import_closure(str(repo / "bin" / "px-changed"), str(repo)) == {"alpha", "beta"}


def test_executed_paths_are_repo_relative(repo):
    assert executed_paths(str(repo / "bin" / "px-changed"), str(repo)) == {
        os.path.join("bin", "px-changed"),
        os.path.join("src", "pxh", "alpha.py"),
        os.path.join("src", "pxh", "beta.py"),
    }


def test_unit_started_before_the_change_must_restart(repo):
    # `src/pxh/beta.py` is written by the fixture, so its mtime is "now" — newer
    # than a stale start time.
    units = [_unit("px-changed.service", repo / "bin" / "px-changed", 0.0)]
    verdicts = restart_list(["src/pxh/beta.py"], units, root=str(repo))
    assert [v.name for v in needs_restart(verdicts)] == ["px-changed.service"]


def test_unit_restarted_after_the_change_is_fine(repo):
    newer_than_anything = 4_000_000_000.0  # year 2096
    units = [_unit("px-changed.service", repo / "bin" / "px-changed", newer_than_anything)]
    verdicts = restart_list(["src/pxh/beta.py"], units, root=str(repo))
    assert needs_restart(verdicts) == []


def test_change_outside_the_closure_does_not_flag(repo):
    units = [
        _unit("px-changed.service", repo / "bin" / "px-changed", 0.0),
        _unit("px-clean.service", repo / "bin" / "px-clean", 0.0),
    ]
    verdicts = restart_list(["src/pxh/gamma.py"], units, root=str(repo))
    assert [v.name for v in needs_restart(verdicts)] == ["px-clean.service"]


def test_changed_entry_point_flags_even_without_pxh_imports(repo):
    units = [_unit("px-nopxh.service", repo / "bin" / "px-nopxh", 0.0)]
    verdicts = restart_list(["bin/px-nopxh"], units, root=str(repo))
    assert [v.name for v in needs_restart(verdicts)] == ["px-nopxh.service"]


def test_deleted_module_uses_the_deploy_stamp(repo):
    """A deleted file has no mtime: the deploy stamp has to answer instead."""
    units = [_unit("px-changed.service", repo / "bin" / "px-changed", DEPLOY_TS - 60)]
    verdicts = restart_list(["src/pxh/alpha.py"], units, root=str(repo), deploy_ts=DEPLOY_TS)
    assert [v.name for v in needs_restart(verdicts)] == ["px-changed.service"]


def test_unit_without_a_repo_entry_point_is_ignored(repo):
    units = [UnitState(name="nginx.service", entry=None, started_ts=0.0)]
    verdicts = restart_list(["bin/px-changed"], units, root=str(repo))
    assert verdicts[0].needs_restart is False


def test_unknown_start_time_is_treated_as_stale(repo):
    units = [UnitState(name="px-changed.service", entry=str(repo / "bin" / "px-changed"), started_ts=None)]
    verdicts = restart_list(["bin/px-changed"], units, root=str(repo))
    assert verdicts[0].needs_restart is True


def test_the_332_deploy_shape_is_flagged(repo):
    """Three units whose entry points moved must restart; an untouched one must not.

    Mirrors the real 20:51 deploy: `bin/px-blog`, `bin/px-evolve`,
    `bin/px-post` and `bin/px-api-server` changed, while `px-battery-poll`,
    `px-frigate-stream` and `px-tts-glados` ran on unaffected files and were
    correctly left alone.
    """
    moved = ["bin/px-changed", "src/pxh/alpha.py", "src/pxh/delta.py"]
    pre_deploy = DEPLOY_TS - 3600 * 30  # 19:08 the previous day
    units = [
        _unit("px-changed.service", repo / "bin" / "px-changed", pre_deploy),
        _unit("px-clean.service", repo / "bin" / "px-clean", pre_deploy),
        _unit("px-nopxh.service", repo / "bin" / "px-nopxh", pre_deploy),
    ]
    verdicts = restart_list(moved, units, root=str(repo), deploy_ts=DEPLOY_TS)
    assert [v.name for v in needs_restart(verdicts)] == ["px-changed.service"]


def test_shell_wrapper_running_python_m_is_seen(repo):
    """`bin/px-mind` is bash: it reaches `pxh.mind` only through `python -m`.

    Without this shape the guard would call the cognitive loop unrelated to the
    module it runs — which is how `src/pxh/mind.py` moving in the #332 deploy
    would have been missed.
    """
    entry = repo / "bin" / "px-wrapper"
    entry.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "exec python -m pxh.alpha \"$@\"\n"
    )
    assert "alpha" in imported_pxh_modules(str(entry))


def test_shell_wrapper_is_flagged_when_its_module_moves(repo):
    entry = repo / "bin" / "px-wrapper"
    entry.write_text("#!/usr/bin/env bash\nexec python3 -m pxh.beta \"$@\"\n")
    units = [_unit("px-wrapper.service", entry, 0.0)]
    verdicts = restart_list(["src/pxh/beta.py"], units, root=str(repo))
    assert [v.name for v in needs_restart(verdicts)] == ["px-wrapper.service"]
