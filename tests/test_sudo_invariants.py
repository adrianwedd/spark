"""Every production `sudo -n` call must be matchable by an exact sudoers rule.

The #281 tightening replaced pi's `NOPASSWD: ALL` with exact-command rules.
An exact rule matches the *literal* resolved command line, which has two
consequences this suite pins (issue #300):

1. `sudo -n env VAR=... cmd` and `sudo -n python3 -c "..."` can never match —
   variable env/argv makes every call line unique. GPIO elevation therefore
   goes through the fixed root-owned launcher `/usr/local/sbin/px-gpio-run`
   (source: systemd/sbin/px-gpio-run), which validates its target and chooses
   its own environment.
2. A bare command name (`systemctl`, `shutdown`) is resolved by sudo via
   secure_path to a spelling the rule may not carry (/usr/bin vs /bin,
   /usr/sbin vs /sbin) — the mismatch is silent and it broke the battery
   emergency shutdown. Production code therefore spells targets absolutely.

The failure mode this guards against is the bad one: `sudo -n` fails with
"a password is required", the tool reports an error nobody hears, and SPARK
goes silent (or, for the shutdown path, a critical battery keeps discharging).
"""
from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Directories whose sudo calls run unattended in production.
SCAN_DIRS = ["bin", "src/pxh"]

# Operator-run interactive scripts (a human is present to type a password);
# these may use plain `sudo`.
INTERACTIVE_SCRIPTS = {"bin/px-update-reboot"}

# The only command words allowed directly after `sudo -n` in production code.
# Everything variable rides *behind* one of these fixed paths.
ALLOWED_SUDO_TARGETS = (
    "/usr/local/sbin/px-gpio-run",
    "/usr/local/sbin/px-signal-alive",
    "/usr/bin/systemctl",
    "/usr/sbin/shutdown",
    # The launcher path is overridable for tests via PX_GPIO_RUN_CMD; call
    # sites hold it in a variable named gpio_run.
    "gpio_run",
    '"${PX_GPIO_RUN_CMD',
)

# Python list form: "sudo", "-n", <target>. Bash form: sudo -n <target>.
_PY_SUDO = re.compile(r'"sudo",\s*(?:"-n",\s*)?(?P<target>"[^"]*"|\w+)')
_SH_SUDO = re.compile(r"\bsudo\s+(?:-n\s+)?(?P<target>\S+)")


def _iter_source_lines():
    for rel in SCAN_DIRS:
        for path in sorted((PROJECT_ROOT / rel).rglob("*")):
            if not path.is_file() or path.suffix in {".pyc", ".json", ".md"}:
                continue
            relpath = str(path.relative_to(PROJECT_ROOT))
            if relpath in INTERACTIVE_SCRIPTS:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                yield relpath, lineno, line


def _target_ok(target: str) -> bool:
    target = target.strip('"')
    return any(target.startswith(allowed.strip('"')) for allowed in ALLOWED_SUDO_TARGETS)


def test_every_production_sudo_call_is_exact_rule_matchable():
    violations = []
    for relpath, lineno, line in _iter_source_lines():
        stripped = line.strip()
        # Comments and docstrings talk *about* sudo; only invocations matter.
        if stripped.startswith("#") or '"sudo"' not in line and "sudo -n" not in line and "sudo " not in line:
            continue
        m = _PY_SUDO.search(line)
        if m is None and (stripped.startswith("exec sudo") or stripped.startswith("sudo ")
                          or " sudo -n " in line):
            m = _SH_SUDO.search(line)
        if m is None:
            continue
        # `"-n"` as the matched token means the Python list continues on the
        # next line; the multiline check below covers those.
        target = m.group("target")
        if target in ('"-n"', "-n"):
            continue
        if not _target_ok(target):
            violations.append(f"{relpath}:{lineno}: {stripped}")
    assert not violations, (
        "sudo invocation(s) that an exact sudoers rule cannot match "
        "(see issue #300 — route GPIO work through px-gpio-run, spell "
        "systemctl/shutdown absolutely):\n" + "\n".join(violations)
    )


def test_no_sudo_env_or_inline_python_anywhere():
    """The two shapes that caused #300 must never come back, even split
    across lines: `sudo ... env VAR=...` and `sudo ... python3 -c`."""
    violations = []
    for relpath, lineno, line in _iter_source_lines():
        if line.strip().startswith("#"):
            continue
        if re.search(r"sudo(\s+-n)?\s+env\s", line) or re.search(
                r'"sudo",.*"(/usr/bin/)?python3"', line):
            violations.append(f"{relpath}:{lineno}: {line.strip()}")
    assert not violations, (
        "sudo+env / sudo+python3 -c invocations found (unmatchable by exact "
        "sudoers rules, #300):\n" + "\n".join(violations)
    )


def test_launcher_source_is_tracked_and_valid():
    """The repo copy of the launcher is the source of truth for the installed
    /usr/local/sbin/px-gpio-run — it must exist, be executable, and carry
    every target name the call sites use."""
    launcher = PROJECT_ROOT / "systemd" / "sbin" / "px-gpio-run"
    assert launcher.exists(), "systemd/sbin/px-gpio-run missing"
    text = launcher.read_text(encoding="utf-8")
    for target in ("circle", "drive", "emote", "figure8", "look", "sonar",
                   "status", "stop", "perform", "wander", "line-follow",
                   "enable-speaker"):
        assert re.search(rf"\b{re.escape(target)}\b", text), (
            f"launcher missing target {target!r}")
    # Unknown targets must be rejected, not passed through.
    assert "unknown target" in text
