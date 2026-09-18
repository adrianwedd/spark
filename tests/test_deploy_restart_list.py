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
from pathlib import Path

import pytest

from pxh.deploy import (
    UnitState,
    changed_symbols,
    executed_paths,
    import_closure,
    imported_pxh_modules,
    needs_restart,
    restart_list,
    unit_module_references,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

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


# ---------------------------------------------------------------------------
# #336 — the graph edges the closure was missing, and reachability over fan-out
# ---------------------------------------------------------------------------
#
# The incident: `pxh.health` grew capability-block machinery in `4503782b`, the
# gate said "every long-lived unit is executing the deployed revision" while
# eight were not, and the restart list was derived by hand. Two causes — missing
# closure edges, and a rule that flagged on *import* rather than on *reach*.


def _git(tmp_path, files):
    """A tiny real repo, because `changed_symbols` reads revisions via git."""
    import subprocess

    def run(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True,
                       capture_output=True, text=True)

    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    run("add", "-A")
    run("commit", "-qm", "v1")
    first = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                           capture_output=True, text=True, check=True).stdout.strip()
    for rel, body in files.items():
        (tmp_path / rel).write_text(body)
    run("add", "-A")
    run("commit", "-qm", "v2")
    return first


def test_from_pxh_import_names_the_submodule_not_the_package(repo):
    """`from pxh import health as _health` put the literal "pxh" in the closure,
    which then died at src/pxh/pxh.py — the dominant idiom in this tree."""
    entry = repo / "bin" / "px-import-pkg"
    entry.write_text("#!/usr/bin/env python3\nfrom pxh import health as _health\n")
    modules = imported_pxh_modules(str(entry))
    assert "health" in modules
    assert "pxh" not in modules
    assert "src/pxh/health.py" in executed_paths(str(entry), str(repo))


def test_from_pxh_import_handles_multiple_names_and_aliases(repo):
    entry = repo / "bin" / "px-import-many"
    entry.write_text(
        "#!/usr/bin/env python3\n"
        "from pxh import people, policy, policy_context, presence\n"
        "from pxh import spark_config as cfg\n"
    )
    modules = imported_pxh_modules(str(entry))
    assert {"people", "policy", "policy_context", "presence", "spark_config"} <= modules


def test_asgi_app_string_is_an_edge(repo):
    """`exec uvicorn pxh.api:app` is how px-api-server reaches api.py, and no
    import-shaped rule can see it."""
    entry = repo / "bin" / "px-api-server"
    entry.write_text("#!/usr/bin/env bash\nexec uvicorn pxh.api:app --host 0.0.0.0\n")
    assert "api" in imported_pxh_modules(str(entry))
    assert "src/pxh/api.py" in executed_paths(str(entry), str(repo))


def test_bash_heredoc_entry_has_a_closure(repo):
    """bin/px-alive is bash wrapping `<<'PY' … PY`: the file is not Python, so
    the AST walk found nothing for the most important daemon on the host."""
    entry = repo / "bin" / "px-alive"
    entry.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "exec /usr/bin/python3 - \"$@\" <<'PY'\n"
        "from pxh.beta import three\n"
        "print(three())\n"
        "PY\n"
    )
    assert "beta" in imported_pxh_modules(str(entry))
    assert "src/pxh/beta.py" in executed_paths(str(entry), str(repo))


def test_the_incident_shape_on_a_wake_listen_like_wrapper(repo):
    """The probe from the issue: a heredoc entry importing pxh.hostload, which
    the gate could not see when that file changed."""
    entry = repo / "bin" / "px-wake-listen"
    entry.write_text(
        "#!/usr/bin/env bash\n"
        "exec /usr/bin/python3 - \"$@\" <<'PY'\n"
        "from pxh.hostload import host_load_fields\n"
        "print(host_load_fields('x'))\n"
        "PY\n"
    )
    # `hostload` is not in the fixture repo's modules, so add it and re-probe.
    (repo / "src" / "pxh" / "hostload.py").write_text("def host_load_fields(p):\n    return {}\n")
    assert "src/pxh/hostload.py" in executed_paths(str(entry), str(repo))
    assert "hostload" in import_closure(str(entry), str(repo))


def test_changed_symbols_sees_a_body_change_and_ignores_an_untouched_module(tmp_path):
    _tree(tmp_path, entries={}, modules={"alpha": "def one():\n    return 1\n"})
    first = _git(tmp_path, {"src/pxh/alpha.py": "def one():\n    return 2\n"})
    change = changed_symbols(first, "HEAD", "src/pxh/alpha.py", str(tmp_path))
    assert change is not None
    assert change.symbols == frozenset({"one"})
    assert change.module_level is False


def test_changed_symbols_sees_a_module_level_import_change(tmp_path):
    _tree(tmp_path, entries={}, modules={"alpha": "from pxh import beta\n\ndef one():\n    return 1\n"})
    first = _git(tmp_path, {"src/pxh/alpha.py": "from pxh import gamma\n\ndef one():\n    return 1\n"})
    change = changed_symbols(first, "HEAD", "src/pxh/alpha.py", str(tmp_path))
    assert change is not None
    assert change.symbols == frozenset()
    assert change.module_level is True, "an import rename is exactly the #332 shape"


def test_changed_symbols_is_none_when_a_revision_cannot_be_read(repo):
    assert changed_symbols("not-a-revision", "HEAD", "src/pxh/alpha.py", str(repo)) is None


def test_file_module_references_covers_the_idioms_in_this_tree(repo):
    from pxh.deploy import file_module_references

    direct = repo / "direct.py"
    direct.write_text("from pxh.alpha import one, two as trois\n")
    assert file_module_references(str(direct), "alpha") == ({"one", "two"}, True)

    alias = repo / "alias.py"
    alias.write_text("from pxh import alpha as a\n\ndef go():\n    return a.one()\n")
    assert file_module_references(str(alias), "alpha") == ({"one"}, True)

    dotted = repo / "dotted.py"
    dotted.write_text("import pxh.alpha\n\ndef go():\n    return pxh.alpha.one()\n")
    assert file_module_references(str(dotted), "alpha") == ({"one"}, True)

    as_dotted = repo / "asdotted.py"
    as_dotted.write_text("import pxh.alpha as a\n\ndef go():\n    return a.two()\n")
    assert file_module_references(str(as_dotted), "alpha") == ({"two"}, True)


def test_file_module_references_reads_a_heredoc_wrapper(repo):
    from pxh.deploy import file_module_references

    entry = repo / "bin" / "px-heredoc"
    entry.write_text(
        "#!/usr/bin/env bash\n"
        "exec /usr/bin/python3 - <<'PY'\n"
        "from pxh import alpha as a\n"
        "a.one()\n"
        "PY\n"
    )
    assert file_module_references(str(entry), "alpha") == ({"one"}, True)


def test_file_module_references_flags_what_it_cannot_resolve(repo):
    from pxh.deploy import file_module_references

    dynamic = repo / "dynamic.py"
    dynamic.write_text(
        "from pxh import alpha as a\n"
        "def go(name):\n"
        "    return getattr(a, name)()\n"
    )
    names, resolvable = file_module_references(str(dynamic), "alpha")
    assert resolvable is False, "getattr(module, name) cannot be ruled out"

    star = repo / "star.py"
    star.write_text("from pxh.alpha import *\n")
    assert file_module_references(str(star), "alpha") == (set(), False)

    literal = repo / "literal.py"
    literal.write_text("import importlib\nmod = importlib.import_module(\"pxh.alpha\")\n")
    assert file_module_references(str(literal), "alpha")[1] is False


def test_restart_list_flags_only_units_that_reach_a_changed_symbol(tmp_path):
    """The issue's decision, as a rule: reachability, not fan-out.

    `health.record_success` is byte-identical across the change; only
    `read_health` moved. The daemon that reports its own health gains nothing
    from a restart, and the one that reads the store must restart.
    """
    _tree(
        tmp_path,
        entries={
            "px-reports": "#!/usr/bin/env python3\nfrom pxh import health as h\nh.record_success('px-reports')\n",
            "px-reads": "#!/usr/bin/env python3\nfrom pxh import health as h\nprint(h.read_health())\n",
        },
        modules={"health": "def record_success(c):\n    pass\n\n\ndef read_health():\n    return {'ok': 1}\n"},
    )
    first = _git(
        tmp_path,
        {"src/pxh/health.py": "def record_success(c):\n    pass\n\n\ndef read_health():\n    return {'ok': 2}\n"},
    )
    health = "src/pxh/health.py"
    changes = {health: changed_symbols(first, "HEAD", health, str(tmp_path))}
    units = [
        _unit("px-reports.service", tmp_path / "bin" / "px-reports", 0.0),
        _unit("px-reads.service", tmp_path / "bin" / "px-reads", 0.0),
    ]
    verdicts = {v.name: v for v in restart_list([health], units, root=str(tmp_path), changes=changes)}
    assert verdicts["px-reads.service"].needs_restart is True
    assert "read_health" in verdicts["px-reads.service"].reason
    assert verdicts["px-reports.service"].needs_restart is False
    assert "references none of the changed names" in verdicts["px-reports.service"].reason


def test_a_module_level_change_flags_every_importer(tmp_path):
    _tree(
        tmp_path,
        entries={"px-reads": "#!/usr/bin/env python3\nfrom pxh import health as h\nh.read_health()\n"},
        modules={"health": "def read_health():\n    return 1\n"},
    )
    first = _git(
        tmp_path,
        {"src/pxh/health.py": "from pxh import logging\n\n\ndef read_health():\n    return 1\n"},
    )
    health = "src/pxh/health.py"
    changes = {health: changed_symbols(first, "HEAD", health, str(tmp_path))}
    units = [_unit("px-reads.service", tmp_path / "bin" / "px-reads", 0.0)]
    verdicts = restart_list([health], units, root=str(tmp_path), changes=changes)
    assert verdicts[0].needs_restart is True
    assert "module-level change" in verdicts[0].reason


def test_an_undetermined_change_is_a_restart(tmp_path):
    """Fail-safe: "cannot tell" must not read as "nothing changed"."""
    _tree(
        tmp_path,
        entries={"px-reads": "#!/usr/bin/env python3\nfrom pxh import health as h\nh.read_health()\n"},
        modules={"health": "def read_health():\n    return 1\n"},
    )
    health = "src/pxh/health.py"
    units = [_unit("px-reads.service", tmp_path / "bin" / "px-reads", 0.0)]
    verdicts = restart_list([health], units, root=str(tmp_path), changes={health: None})
    assert verdicts[0].needs_restart is True
    assert "could not be determined" in verdicts[0].reason


# --- a changed name that reaches importers through the module's own code (#365)
#
# The mic ring: `DEFAULT_ALSA_BUFFER_S` is read exactly once in
# `pxh/mic_stream.py`, as the default value of `ArecordStream.__init__`'s
# `alsa_buffer_s`. `px-wake-listen` names only `ArecordStream`, so the symbol
# comparison answered "references none of the changed names" and the capture
# daemon would have been left on the old ring (replayed on `picar` 2026-09-17).

_MIC_V1 = """DEFAULT_ALSA_BUFFER_S = 4.0


class ArecordStream:
    def __init__(self, alsa_buffer_s: float = DEFAULT_ALSA_BUFFER_S):
        self.alsa_buffer_s = alsa_buffer_s

    def build_command(self):
        return ["--buffer-size", str(self.alsa_buffer_s)]


def unrelated():
    return 1
"""

_MIC_V2 = _MIC_V1.replace("= 4.0", "= 12.0")


def test_changed_symbols_attributes_a_read_to_the_definition_that_reads_it(tmp_path):
    _tree(
        tmp_path,
        entries={"px-wake-listen": "#!/usr/bin/env python3\nfrom pxh.mic_stream import ArecordStream\n"},
        modules={"mic_stream": _MIC_V1},
    )
    first = _git(tmp_path, {"src/pxh/mic_stream.py": _MIC_V2})
    path = "src/pxh/mic_stream.py"
    change = changed_symbols(first, "HEAD", path, str(tmp_path))
    assert change.symbols == frozenset({"DEFAULT_ALSA_BUFFER_S"})
    # Edges point the way the flow goes: definition -> top-level names it reads.
    # Only top-level names are edges — a parameter or a builtin inside the
    # definition is not something this module can change under a caller.
    assert "DEFAULT_ALSA_BUFFER_S" in change.local_reads["ArecordStream"]
    assert "self" not in change.local_reads["ArecordStream"]
    assert change.read_at_module_level is False


def test_a_constant_behind_a_default_argument_flags_the_unit_that_calls_the_class(tmp_path):
    _tree(
        tmp_path,
        entries={
            "px-wake-listen": "#!/usr/bin/env python3\nfrom pxh.mic_stream import ArecordStream\nArecordStream()\n",
            # References the module but not the class whose default moved.
            "px-other": "#!/usr/bin/env python3\nfrom pxh.mic_stream import unrelated\nunrelated()\n",
        },
        modules={"mic_stream": _MIC_V1},
    )
    first = _git(tmp_path, {"src/pxh/mic_stream.py": _MIC_V2})
    path = "src/pxh/mic_stream.py"
    changes = {path: changed_symbols(first, "HEAD", path, str(tmp_path))}
    units = [
        _unit("px-wake-listen.service", tmp_path / "bin" / "px-wake-listen", 0.0),
        _unit("px-other.service", tmp_path / "bin" / "px-other", 0.0),
    ]
    verdicts = {
        v.name: v
        for v in restart_list([path], units, root=str(tmp_path), changes=changes)
    }
    assert verdicts["px-wake-listen.service"].needs_restart is True
    assert "DEFAULT_ALSA_BUFFER_S" in verdicts["px-wake-listen.service"].reason
    assert "reaches" in verdicts["px-wake-listen.service"].reason
    # Precision: the flow is attributed to the definition the unit calls, so a
    # unit that calls something else in the same module is not dragged in.
    assert verdicts["px-other.service"].needs_restart is False


def test_a_wrapper_that_runs_the_module_is_flagged_on_any_change(tmp_path):
    """The *real* shape of the `#370` miss, which my first version of this test
    got wrong: `bin/px-mind` is bash ending in `exec python -m pxh.mind "$@"`.
    The gate resolved the module (it is in the closure) but extracted **no
    names** from it, so the reference comparison and the reachability walk both
    came up empty and it reported "references none of the changed names" — a
    true statement about a unit that runs the whole module."""
    _tree(
        tmp_path,
        entries={
            "px-mind": (
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "source \"$SCRIPT_DIR/px-env\"\n"
                'exec python -m pxh.mind "$@"\n'
            )
        },
        modules={
            "mind": (
                "def awareness_tick():\n"
                "    return 1\n"
                "\n"
                "\n"
                "def main():\n"
                "    return awareness_tick()\n"
            )
        },
    )
    first = _git(
        tmp_path,
        {"src/pxh/mind.py": (
            "def awareness_tick():\n"
            "    return 2\n"
            "\n"
            "\n"
            "def main():\n"
            "    return awareness_tick()\n"
        )},
    )
    path = "src/pxh/mind.py"
    changes = {path: changed_symbols(first, "HEAD", path, str(tmp_path))}
    units = [_unit("px-mind.service", tmp_path / "bin" / "px-mind", 0.0)]
    # The closure must actually see the module for this shape: the wrapper only
    # reaches it through `python -m` (test_shell_wrapper_running_python_m_is_seen).
    assert "mind" in import_closure(str(tmp_path / "bin" / "px-mind"), str(tmp_path))
    assert path in executed_paths(str(tmp_path / "bin" / "px-mind"), str(tmp_path))
    assert unit_module_references(
        str(tmp_path / "bin" / "px-mind"), str(tmp_path), "pxh.mind"
    ) == (set(), True)
    verdicts = restart_list([path], units, root=str(tmp_path), changes=changes)
    assert verdicts[0].needs_restart is True
    assert "runs the module" in verdicts[0].reason


def test_a_change_two_hops_in_flags_the_unit_that_runs_the_loop(tmp_path):
    """The `#370` shape: `awareness_tick` changed, `bin/px-mind` names only
    `main`, and `main` reaches the tick through the loop. One hop was not
    enough — the gate said "references none of the changed names" and would
    have left px-mind running the old tick."""
    _tree(
        tmp_path,
        entries={"px-mind": "#!/usr/bin/env python3\nfrom pxh.mind import main\nmain()\n"},
        modules={
            "mind": (
                "def awareness_tick():\n"
                "    return 1\n"
                "\n"
                "\n"
                "def mind_loop():\n"
                "    return awareness_tick()\n"
                "\n"
                "\n"
                "def main():\n"
                "    return mind_loop()\n"
            )
        },
    )
    first = _git(
        tmp_path,
        {"src/pxh/mind.py": (
            "def awareness_tick():\n"
            "    return 2\n"
            "\n"
            "\n"
            "def mind_loop():\n"
            "    return awareness_tick()\n"
            "\n"
            "\n"
            "def main():\n"
            "    return mind_loop()\n"
        )},
    )
    path = "src/pxh/mind.py"
    change = changed_symbols(first, "HEAD", path, str(tmp_path))
    assert change.symbols == frozenset({"awareness_tick"})
    units = [_unit("px-mind.service", tmp_path / "bin" / "px-mind", 0.0)]
    verdicts = restart_list([path], units, root=str(tmp_path), changes={path: change})
    assert verdicts[0].needs_restart is True
    assert "awareness_tick" in verdicts[0].reason


def test_reachability_does_not_flag_a_unit_that_calls_elsewhere(tmp_path):
    """Precision, at the same time as the closure: a unit whose names cannot
    reach the changed definition is not dragged in."""
    _tree(
        tmp_path,
        entries={"px-other": "#!/usr/bin/env python3\nfrom pxh.mind import other\nother()\n"},
        modules={
            "mind": (
                "def awareness_tick():\n"
                "    return 1\n"
                "\n"
                "\n"
                "def other():\n"
                "    return 0\n"
            )
        },
    )
    first = _git(
        tmp_path,
        {"src/pxh/mind.py": (
            "def awareness_tick():\n"
            "    return 2\n"
            "\n"
            "\n"
            "def other():\n"
            "    return 0\n"
        )},
    )
    path = "src/pxh/mind.py"
    changes = {path: changed_symbols(first, "HEAD", path, str(tmp_path))}
    units = [_unit("px-other.service", tmp_path / "bin" / "px-other", 0.0)]
    verdicts = restart_list([path], units, root=str(tmp_path), changes=changes)
    assert verdicts[0].needs_restart is False


def test_a_changed_name_read_at_module_level_flags_every_importer(tmp_path):
    _tree(
        tmp_path,
        entries={"px-reads": "#!/usr/bin/env python3\nfrom pxh.cfg import DERIVED\nprint(DERIVED)\n"},
        modules={"cfg": "BASE = 1\nDERIVED = BASE + 1\n"},
    )
    first = _git(tmp_path, {"src/pxh/cfg.py": "BASE = 2\nDERIVED = BASE + 1\n"})
    path = "src/pxh/cfg.py"
    change = changed_symbols(first, "HEAD", path, str(tmp_path))
    assert change.read_at_module_level is True
    units = [_unit("px-reads.service", tmp_path / "bin" / "px-reads", 0.0)]
    verdicts = restart_list([path], units, root=str(tmp_path), changes={path: change})
    assert verdicts[0].needs_restart is True
    assert "module level" in verdicts[0].reason


def test_without_a_change_map_the_old_path_rule_stands(tmp_path):
    """Callers with no git revisions to hand keep the conservative behaviour."""
    _tree(
        tmp_path,
        entries={"px-reads": "#!/usr/bin/env python3\nfrom pxh import health as h\nh.read_health()\n"},
        modules={"health": "def read_health():\n    return 1\n"},
    )
    health = "src/pxh/health.py"
    units = [_unit("px-reads.service", tmp_path / "bin" / "px-reads", 0.0)]
    verdicts = restart_list([health], units, root=str(tmp_path))
    assert verdicts[0].needs_restart is True


def test_the_replayed_incident_flags_exactly_the_hand_restarted_units():
    """The 2026-09-17 #332/#334/#335 deploy, replayed against the real repo.

    The gate named two units and the truth was four; the corrected-rule table in
    #336 has them. Skipped where the clone is too shallow to read both
    revisions (CI checks out depth 1), because a skipped test is honest and a
    silently-passing one is not.
    """
    import datetime as dt
    import subprocess
    from zoneinfo import ZoneInfo

    from pxh.deploy import CODE_DIRS

    prev, head = "09b1fbda", "4503782b"
    probe = subprocess.run(["git", "show", f"{prev}:src/pxh/health.py"],
                           cwd=str(PROJECT_ROOT), capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip("revisions unavailable in this clone (shallow checkout)")

    moved = subprocess.run(
        ["git", "diff", "--name-only", prev, head, "--", *CODE_DIRS],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, check=True,
    ).stdout.split()
    changes = {
        path: changed_symbols(prev, head, path, str(PROJECT_ROOT)) for path in moved
    }

    hobart = ZoneInfo("Australia/Hobart")

    def started(stamp):
        return dt.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=hobart).timestamp()

    observed = [
        ("px-alive.service", "bin/px-alive", "2026-09-17 13:17:49"),
        ("px-api-server.service", "bin/px-api-server", "2026-09-17 11:24:17"),
        ("px-battery-poll.service", "bin/px-battery-poll", "2026-09-15 19:08:51"),
        ("px-blog.service", "bin/px-blog", "2026-09-17 11:39:26"),
        ("px-evolve.service", "bin/px-evolve", "2026-09-17 11:39:26"),
        ("px-frigate-stream.service", "bin/px-frigate-stream", "2026-09-15 19:08:51"),
        ("px-mind.service", "bin/px-mind", "2026-09-16 20:51:40"),
        ("px-post.service", "bin/px-post", "2026-09-17 11:39:26"),
        ("px-tts-glados.service", "bin/tts-glados-server", "2026-09-15 19:08:51"),
        ("px-wake-listen.service", "bin/px-wake-listen", "2026-09-16 20:51:32"),
    ]
    units = [
        UnitState(name=name, entry=str(PROJECT_ROOT / entry), started_ts=started(stamp))
        for name, entry, stamp in observed
    ]

    flagged = {
        v.name for v in needs_restart(
            restart_list(moved, units, root=str(PROJECT_ROOT), changes=changes)
        )
    }
    assert flagged == {
        "px-api-server.service",
        "px-blog.service",
        "px-evolve.service",
        "px-mind.service",
    }

# --- installed unit files vs the repo (#336) -------------------------------
#
# The gate's promise is about *code*; the unit file the host reads is installed
# by hand. Six installed units on picar were months behind the repo on
# 2026-09-18, including functional differences (bounded restarts,
# Wants=network-online.target, a narrowed PATH).


def _unit_pair(tmp_path, name="px-mind.service", *, repo_text="[Unit]\nX=1\n",
               installed_text="[Unit]\nX=1\n", installed=True, drop_in=None,
               base=None):
    tmp_path = base or tmp_path
    repo = tmp_path / "systemd"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(repo_text)
    if drop_in is not None:
        d = repo / (name + ".d")
        d.mkdir()
        (d / "10-containment.conf").write_text(drop_in)
    installed_dir = tmp_path / "etc"
    installed_dir.mkdir(parents=True, exist_ok=True)
    if installed:
        (installed_dir / name).write_text(installed_text)
    return str(tmp_path), str(installed_dir)


def test_a_matching_unit_file_is_not_reported(tmp_path):
    from pxh.deploy import unit_file_drift

    root, installed = _unit_pair(tmp_path)
    assert unit_file_drift("px-mind.service", root=root, install_dir=installed) == []


def test_a_stale_installed_unit_file_is_reported(tmp_path):
    from pxh.deploy import unit_file_drift

    root, installed = _unit_pair(
        tmp_path, repo_text="[Unit]\nStartLimitBurst=5\n",
        installed_text="[Unit]\nStartLimitIntervalSec=0\n",
    )
    drift = unit_file_drift("px-mind.service", root=root, install_dir=installed)
    assert [(d.reason, d.repo_path) for d in drift] == [
        ("differs", "systemd/px-mind.service")
    ]


def test_a_missing_or_unreadable_install_is_not_reported_as_drift(tmp_path):
    """`cannot check` and `changed` are different facts, and only one is actionable."""
    from pxh.deploy import unit_file_drift

    root, installed = _unit_pair(tmp_path, installed=False)
    assert [d.reason for d in unit_file_drift("px-mind.service", root=root, install_dir=installed)] == [
        "not installed"
    ]
    # A directory where the file should be: read fails, path exists.
    root2, installed2 = _unit_pair(tmp_path, installed=True, base=tmp_path / "second")
    os.remove(os.path.join(installed2, "px-mind.service"))
    os.mkdir(os.path.join(installed2, "px-mind.service"))
    assert [d.reason for d in unit_file_drift("px-mind.service", root=root2, install_dir=installed2)] == [
        "unreadable"
    ]


def test_drop_ins_are_checked_too(tmp_path):
    from pxh.deploy import unit_file_drift

    root = tmp_path / "r"
    (root / "systemd" / "px-frigate-stream.service.d").mkdir(parents=True)
    (root / "systemd" / "px-frigate-stream.service").write_text("[Unit]\n")
    (root / "systemd" / "px-frigate-stream.service.d" / "10-containment.conf").write_text("a=1\n")
    installed = tmp_path / "i"
    (installed / "px-frigate-stream.service.d").mkdir(parents=True)
    (installed / "px-frigate-stream.service").write_text("[Unit]\n")
    (installed / "px-frigate-stream.service.d" / "10-containment.conf").write_text("a=2\n")

    drift = unit_file_drift(
        "px-frigate-stream.service", root=str(root), install_dir=str(installed)
    )
    assert [(d.reason, os.path.basename(d.repo_path)) for d in drift] == [
        ("differs", "10-containment.conf")
    ]


def test_repo_unit_names_finds_services_and_timers_and_ignores_scripts(tmp_path):
    """The drift check iterates the repo's shipped units, not the running ones:
    a unit outside the px-* namespace, or one never installed at all, is exactly
    what a `systemctl list-units px-*` sweep cannot see."""
    from pxh.deploy import repo_unit_names

    src = tmp_path / "systemd"
    src.mkdir()
    for name in ("px-mind.service", "spark-pip-cleanup.timer", "px-io-attrib.service",
                 "spark-pip-cleanup.sh", "README.md"):
        (src / name).write_text("x")
    assert repo_unit_names(str(tmp_path)) == [
        "px-io-attrib.service", "px-mind.service", "spark-pip-cleanup.timer"
    ]
    assert repo_unit_names(str(tmp_path / "nope")) == []


def test_a_unit_the_repo_ships_but_nobody_installed_is_reported(tmp_path):
    """`not installed` and `differs` are different facts, and both are actionable."""
    from pxh.deploy import unit_file_drift

    root = tmp_path / "r"
    (root / "systemd").mkdir(parents=True)
    (root / "systemd" / "px-io-attrib.service").write_text("[Service]\nUser=root\n")
    installed = tmp_path / "i"
    installed.mkdir()
    drift = unit_file_drift("px-io-attrib.service", root=str(root), install_dir=str(installed))
    assert [(d.reason, d.repo_path) for d in drift] == [
        ("not installed", "systemd/px-io-attrib.service")
    ]


# --- the gate's first line -------------------------------------------------


def test_the_summary_leads_with_what_needs_doing():
    """Added after the same miss twice: detail below the reassurance, exit 0.

    On 2026-09-18 the host had seven drifted unit files — one of them a unit
    that had never been installed — and `px-deploy-check` exited **0** with the
    drift printed under "every long-lived unit is executing the deployed
    revision". A reader who skimmed, or a caller that trusted the code, saw a
    clean deploy.
    """
    from pxh.deploy import UnitFileDrift, action_summary

    missing = UnitFileDrift(
        unit="px-io-attrib.service",
        installed_path="/etc/systemd/system/px-io-attrib.service",
        repo_path="systemd/px-io-attrib.service",
        reason="not installed",
    )
    differs = UnitFileDrift(
        unit="px-mind.service",
        installed_path="/etc/systemd/system/px-mind.service",
        repo_path="systemd/px-mind.service",
        reason="differs",
    )
    line = action_summary(1, ["px-wake-listen.service"], [missing, differs])
    assert line.startswith("px-deploy-check: ")
    assert "changed files: 1" in line
    assert "units to restart: 1" in line
    assert "unit files to install: 2 (1 missing)" in line
    # Nothing to do is stated, not implied by an absence.
    quiet = action_summary(0, [], [])
    assert quiet.endswith("units to restart: 0 | carried staleness: 0 | unit files to install: 0")
    assert "missing" not in quiet


# ---------------------------------------------------------------------------
# Staleness carried from an earlier deploy (#421)
# ---------------------------------------------------------------------------
#
# `restart_list` answers "must *this* deploy restart it", from HEAD@{1}..HEAD.
# A restart that could not be performed — no non-interactive sudo grant for
# that unit, an interrupted deploy — leaves no trace there, so the next deploy
# reports a clean delta over a host still executing the older module. That is
# a different question, and these tests pin that it is asked separately.

def _ages(repo, *, entry_age_s=1000.0, module_age_s=100.0, now=None):
    """Set every file's mtime, so nothing depends on when the test ran."""
    import time as _time

    base = now if now is not None else _time.time()
    for path in repo.rglob("*"):
        if path.is_file():
            os.utime(path, (base - entry_age_s, base - entry_age_s))
    os.utime(repo / "src" / "pxh" / "alpha.py",
             (base - module_age_s, base - module_age_s))
    return base


def test_staleness_from_an_earlier_deploy_is_visible_and_the_delta_is_not(repo):
    """The crux of #421: both statements are true at once.

    With an empty delta the gate correctly has nothing to restart; the host is
    still executing `alpha.py` as it was before that file changed.
    """
    import time as _time

    from pxh.deploy import UnitState, carried_staleness, needs_restart, restart_list

    now = _ages(repo)
    old = UnitState("px-old.service", str(repo / "bin" / "px-changed"),
                    started_ts=now - 500.0)   # started after the entry, before alpha.py
    fresh = UnitState("px-new.service", str(repo / "bin" / "px-changed"),
                      started_ts=now - 50.0)  # started after everything

    verdicts = restart_list([], [old, fresh], root=str(repo))
    assert needs_restart(verdicts) == [], "an empty delta must not invent a restart"

    carried = carried_staleness([old, fresh], root=str(repo))
    assert [c.name for c in carried] == ["px-old.service"]
    assert carried[0].paths and "src/pxh/alpha.py" in carried[0].paths
    assert carried[0].newest == pytest.approx(now - 100.0, abs=1.0)


def test_a_unit_started_after_its_files_is_not_flagged(repo):
    import time as _time

    from pxh.deploy import UnitState, carried_staleness

    now = _ages(repo)
    unit = UnitState("px-new.service", str(repo / "bin" / "px-changed"),
                     started_ts=now - 1.0)
    assert carried_staleness([unit], root=str(repo)) == []


def test_an_unknown_start_time_is_not_guessed_at(repo):
    """`restart_list` already flags unknown start times; a second warning built
    on an absent number would be a guess dressed as a finding."""
    from pxh.deploy import UnitState, carried_staleness

    _ages(repo)
    unit = UnitState("px-unknown.service", str(repo / "bin" / "px-changed"), started_ts=None)
    assert carried_staleness([unit], root=str(repo)) == []


def test_a_unit_running_nothing_under_this_repo_is_skipped(repo):
    """A unit whose entry point is outside the checkout has no executed paths to
    compare, and inventing an answer from the absence would be a guess."""
    from pxh.deploy import UnitState, carried_staleness

    _ages(repo)
    assert carried_staleness(
        [UnitState("px-elsewhere.service", "/usr/local/bin/other", started_ts=1.0)],
        root=str(repo),
    ) == []
    # A unit whose entry point *is* in the repo is not skipped: px-nopxh
    # executes only itself, and that file is as much a part of the comparison as
    # an imported module.
    assert carried_staleness(
        [UnitState("px-nopxh.service", str(repo / "bin" / "px-nopxh"), started_ts=1.0)],
        root=str(repo),
    ) != []


def test_the_carried_list_names_every_file_newer_than_the_process(repo):
    import time as _time

    from pxh.deploy import UnitState, carried_staleness

    now = _ages(repo)
    unit = UnitState("px-old.service", str(repo / "bin" / "px-changed"),
                     started_ts=now - 500.0)
    carried = carried_staleness([unit], root=str(repo))
    assert len(carried) == 1
    # The entry point is written at the same age as the rest of the tree, so it
    # is not newer than the process and must not appear.
    assert "bin/px-changed" not in carried[0].paths
    assert all(p.endswith(".py") for p in carried[0].paths)


def test_a_deleted_executed_file_counts_as_newer(repo):
    """A unit importing a module that no longer exists is executing code the
    checkout cannot supply — the conservative direction, with no mtime to
    compare against."""
    import time as _time

    from pxh.deploy import UnitState, carried_staleness

    now = _ages(repo)
    (repo / "src" / "pxh" / "beta.py").unlink()
    unit = UnitState("px-old.service", str(repo / "bin" / "px-changed"),
                     started_ts=now - 500.0)
    carried = carried_staleness([unit], root=str(repo))
    assert carried and "src/pxh/beta.py" in carried[0].paths


def test_the_summary_line_states_carried_staleness():
    """#421's own near-miss: the first line read like a clean deploy on a host
    carrying two stale units."""
    from pxh.deploy import CarriedStaleness, action_summary

    carried = CarriedStaleness(
        name="px-blog.service",
        started_ts=1_758_010_264.0,
        newest=1_758_013_000.0,
        paths=("src/pxh/model_session.py",),
    )
    line = action_summary(4, [], [], [carried])
    assert "carried staleness: 1" in line
    assert line.index("carried staleness") < line.index("unit files to install")
    # Nothing carried and nothing to do is still stated, not implied.
    assert action_summary(0, [], []).count("carried staleness: 0") == 1
