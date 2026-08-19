import sys
import os
import json
import shlex
import shutil
import subprocess
import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if SRC.exists():
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def _isolate_health_writes(tmp_path, monkeypatch):
    """Keep health reporting out of the live state/health/ directory.

    Autouse and unconditional. `isolated_project` is opt-in and only isolates
    *subprocesses*, so any in-process test that touches a daemon code path
    (a TestClient request, a mocked reflection) writes health records through
    the real PX_STATE_DIR. On this repo that directory belongs to a running
    robot — a test run would overwrite live health with mock values, and the
    dashboard would report whatever the suite last asserted.

    Redirects only the health directory rather than repointing PX_STATE_DIR
    globally, which many existing tests deliberately rely on.
    """
    try:
        from pxh import health
    except ImportError:
        return
    health_dir = tmp_path / "health"
    monkeypatch.setattr(health, "health_dir", lambda: health_dir)
    health._last_success_write.clear()


@pytest.fixture(autouse=True)
def _isolate_brain_mailbox(tmp_path, monkeypatch):
    """Keep brain requests out of the live state/brain/ mailbox.

    Same hazard as _isolate_health_writes, and worse in one respect: an
    unisolated test that reaches run_claude_session for a brain-routed type
    drops a real request into the running robot's inbox, where the resident
    Claude session will pick it up and answer it. A test would spend budget
    and make SPARK act on a prompt nobody meant to send.

    Redirects only the mailbox root, so tests that deliberately set
    PX_STATE_DIR keep working.
    """
    try:
        from pxh import brain
    except ImportError:
        return
    monkeypatch.setattr(brain, "brain_root", lambda: tmp_path / "brain")


@pytest.fixture(autouse=True)
def _isolate_alive_heartbeat(tmp_path, monkeypatch):
    """Keep heartbeat reads off the live robot's /run/spark.

    Same hazard as _isolate_health_writes, one layer down. #192 moved the
    px-alive heartbeat to tmpfs, and resolve_heartbeat_read_path() prefers
    /run/spark unconditionally — correct in production, but on a host where
    px-alive is actually running it means an isolated test reads the *live*
    robot's heartbeat instead of the one it just wrote to tmp_path. Every
    TestHealth heartbeat assertion then sees a permanently fresh "running"
    beat: eight tests that pass on CI fail on the robot, and worse, they would
    pass there for the wrong reason if the fixture were ever inverted.

    PX_ALIVE_HEARTBEAT_DIR is the documented override, so point it at a
    per-test tmp dir rather than repointing PX_STATE_DIR globally.
    """
    monkeypatch.setenv("PX_ALIVE_HEARTBEAT_DIR", str(tmp_path / "run-spark"))


@pytest.fixture(autouse=True)
def _isolate_session(tmp_path, monkeypatch, request):
    """Keep session reads and writes off the live robot's state/session.json.

    The fourth instance of the hazard the three fixtures above already close,
    and the one that stayed open longest (#210). `isolated_project` sets
    PX_SESSION_PATH too, but it is opt-in and only isolates *subprocesses*, so
    any in-process test reaching load_session()/update_session() resolved to
    the running robot's own session file — reading it, and on update_session()
    rewriting it.

    That is not theoretical. It cost six `test_mind_utils` expression tests,
    permanently red on this machine and green everywhere else: the live session
    carries `spark_quiet_mode: true` (#209), mind.expression() is a #174
    enforcement point, and policy.evaluate() therefore blocked each dispatch
    before the test's own mock could fire. The failure was carried as a known
    baseline for months, which is how a red suite stops being read at all.

    Seeded from state.default_state() rather than left absent, for two reasons.
    Readers that fail *closed* on an unreadable session (policy_context, by
    design) would otherwise see every test as "quiet mode indeterminate" and
    suppress; and seeding explicitly keeps the fixture from depending on
    state/session.template.json happening to be present, which is what
    ensure_session() would fall back to. The two agree today — checked — so
    this changes no behaviour, only what the isolation rests on.

    Set via monkeypatch.setenv at setup, so the 22 sites across 9 test files
    that own PX_SESSION_PATH themselves still win: their setenv/os.environ
    assignment runs after this one. Pinned by
    test_a_test_that_sets_its_own_session_path_still_wins.

    Escape hatch: tests marked `live` exercise the real machine and keep the
    real session — isolating those would have them assert against a fiction.
    """
    if request.node.get_closest_marker("live"):
        return
    try:
        from pxh import state
    except ImportError:
        return
    session_path = tmp_path / "session" / "session.json"
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text(
        json.dumps(state.default_state(), indent=2) + "\n", encoding="utf-8"
    )
    monkeypatch.setenv("PX_SESSION_PATH", str(session_path))


@pytest.fixture
def isolated_project(tmp_path):
    """Creates an isolated project directory for testing."""
    log_dir = tmp_path / "logs"
    state_dir = tmp_path / "state"
    log_dir.mkdir()
    state_dir.mkdir()

    session_path = state_dir / "session.json"

    env = os.environ.copy()
    env["PROJECT_ROOT"] = str(ROOT)
    env["LOG_DIR"] = str(log_dir)
    env["PX_SESSION_PATH"] = str(session_path)
    env["PX_BYPASS_SUDO"] = "1"
    env["PX_VOICE_DEVICE"] = "null"
    env["PX_STATE_DIR"] = str(state_dir)
    # Pin the night-silence window open. bin/tool-voice now evaluates policy
    # (#174) for itself, so without this every subprocess test of a speaking
    # tool would pass by day and return "suppressed" after 19:00 Hobart —
    # the in-process equivalent of what voice_loop._policy_now() exists to
    # prevent. `hour >= 99` is never true, so this window never opens.
    # Tests that mean to exercise night silence override both values.
    env["PX_NIGHT_SILENCE_START_H"] = "99"
    env["PX_NIGHT_SILENCE_END_H"] = "0"

    return {
        "env": env,
        "log_dir": log_dir,
        "state_dir": state_dir,
        "session_path": session_path,
    }

# ── The destructive OS boundary ─────────────────────────────────────────────

class PrivilegedCommandRefused(RuntimeError):
    """A test tried to run a privileged command against the live machine."""


# Names that escalate privilege, control services, control power, or kill
# processes. Everything here appears in `src/pxh` today or is one rename away
# from it: api.py runs `sudo systemctl`, `sudo /usr/bin/systemctl reboot` and
# `sudo /sbin/shutdown`; mind.py runs `sudo -n systemctl start px-alive` and
# `sudo shutdown -h now`; vision.py runs `runuser`.
#
# Deliberately a *small* set. This is not an attempt to sandbox the suite —
# tests legitimately spawn python, bash and every `bin/tool-*`. It is a guard
# on the specific boundary that cost us a running robot.
_PRIVILEGED_NAMES = frozenset({
    "sudo", "su", "doas", "runuser", "pkexec",
    "systemctl", "systemd-run", "service", "telinit",
    "shutdown", "reboot", "halt", "poweroff",
    "pkill", "killall",
})

# Where the *real* ones live. A privileged name resolving anywhere else is a
# stub the test installed itself, which is a legitimate way to assert on argv
# without touching the machine — tests/test_tools.py does exactly that.
_SYSTEM_BIN_DIRS = ("/bin", "/sbin", "/usr/bin", "/usr/sbin",
                    "/usr/local/bin", "/usr/local/sbin")


def _resolves_to_a_real_privileged_binary(args, env, shell) -> bool:
    """True when this argv would reach a real privileged binary on this host.

    Fails closed: an unresolvable privileged name is refused rather than left
    to raise FileNotFoundError, so a stripped PATH can never become a way to
    launder one past the guard (CLAUDE.md invariant 6).
    """
    if shell:
        raw = args if isinstance(args, (str, bytes)) else (list(args) or [""])[0]
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            parts = shlex.split(raw)
        except ValueError:
            return False
        exe = parts[0] if parts else ""
    else:
        seq = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
        if not seq:
            return False
        exe = seq[0]

    exe = os.fspath(exe)
    if isinstance(exe, bytes):
        exe = exe.decode("utf-8", "replace")
    if os.path.basename(exe) not in _PRIVILEGED_NAMES:
        return False

    if os.sep in exe:
        resolved = exe
    else:
        search_path = (env or os.environ).get("PATH") or os.defpath
        resolved = shutil.which(exe, path=search_path)
        if resolved is None:
            return True  # unknown fails closed

    real = os.path.realpath(resolved)
    return any(real.startswith(d + os.sep) for d in _SYSTEM_BIN_DIRS)


def _record_argv(args, shell):
    if shell or isinstance(args, (str, bytes, os.PathLike)):
        return args if isinstance(args, str) else os.fspath(args)
    return [os.fspath(a) if not isinstance(a, str) else a for a in args]


@pytest.fixture(autouse=True)
def _refuse_the_destructive_boundary(request, monkeypatch):
    """Refuse privileged OS commands, whether or not the test was marked live.

    The fourth isolation fixture above closes *state* leaks. This one closes
    an *action* leak, and it is the layer that does not depend on anybody
    having classified the test correctly.

    On 2026-08-19 a bare `python -m pytest` on this robot ran
    `test_service_stop_with_confirm`, which POSTs to the API's service-control
    endpoint with `confirm: true`. That endpoint calls `_run_systemctl`, which
    runs `sudo systemctl stop px-alive` — and consults `PX_DRY` nowhere. The
    daemon stopped. `test_service_start_no_confirm_needed` then started it
    again. Neither test was marked `live`, so `-m "not live"` would not have
    saved us either; that is the whole reason this guard is a second, separate
    mechanism rather than more markers.

    Guarding `subprocess.Popen` covers `run`, `call`, `check_call`,
    `check_output` and `os.popen`, which all construct one. `os.system` does
    not, so it is patched separately. Asyncio's `subprocess_exec` and a raw
    `os.execv` are *not* covered — no call site uses them, and a guard that
    claims more than it does is worse than one with a stated edge.

    Tests marked `live` keep real access: that is what they are for.
    """
    refused: list = []
    if request.node.get_closest_marker("live"):
        yield refused
        return

    real_popen = subprocess.Popen

    class _GuardedPopen(real_popen):
        def __init__(self, args=None, *rest, **kw):
            argv = kw.get("args", args)
            shell = kw.get("shell", False)
            if _resolves_to_a_real_privileged_binary(argv, kw.get("env"), shell):
                recorded = _record_argv(argv, shell)
                refused.append(recorded)
                raise PrivilegedCommandRefused(
                    f"refused {recorded!r}: an unmarked test may not run a "
                    f"privileged command against the live robot. Mark the test "
                    f"`live` if it genuinely must, or patch the boundary."
                )
            super().__init__(args, *rest, **kw)

    monkeypatch.setattr(subprocess, "Popen", _GuardedPopen)

    real_system = os.system

    def _guarded_system(command):
        if _resolves_to_a_real_privileged_binary(command, None, True):
            refused.append(command)
            raise PrivilegedCommandRefused(
                f"refused {command!r}: an unmarked test may not run a "
                f"privileged command against the live robot."
            )
        return real_system(command)

    monkeypatch.setattr(os, "system", _guarded_system)
    yield refused


@pytest.fixture
def refused_privileged_commands(_refuse_the_destructive_boundary):
    """Every privileged command this test tried to run and did not.

    The artefact: asserting on this proves the argv was *constructed* and
    never *executed*, which no amount of `PX_DRY` downstream can prove.
    """
    return _refuse_the_destructive_boundary
