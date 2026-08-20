"""Structural checks on the resource-containment drop-ins (#217/#218/#219).

These tests parse the version-controlled ``systemd/*.service.d/*.conf`` files
on disk. They must never touch a live service — no ``systemctl``, no
``subprocess``, nothing under ``sudo``. The property under test is "the
committed drop-ins are internally consistent", not "the robot is currently
contained" (that is what the live acceptance checks in
docs/operations/resource-containment.md are for).
"""

import configparser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = ROOT / "systemd"

# Every unit this PR contains. A unit not in this set is intentionally out of
# scope (documented in docs/operations/resource-containment.md), not an
# oversight — this list is what "contained" means for these tests.
CONTAINED_UNITS = [
    "px-brain",
    "px-wake-listen",
    "px-tts-glados",
    "px-frigate-stream",
    "px-api-server",
    "px-post",
    "px-mind",
    "px-alive",
    "px-battery-poll",
    "px-evolve",
    "px-blog",
    "cloudflared",
]


def _drop_in_path(unit: str) -> Path:
    return SYSTEMD_DIR / f"{unit}.service.d" / "10-containment.conf"


def _parse_bytes(value: str) -> int:
    """Parse a systemd byte value like '640M' into bytes. No 'infinity'."""
    units = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    value = value.strip()
    if value and value[-1] in units:
        return int(value[:-1]) * units[value[-1]]
    return int(value)


def _load(unit: str) -> configparser.ConfigParser:
    path = _drop_in_path(unit)
    assert path.is_file(), f"missing containment drop-in for {unit}: {path}"
    parser = configparser.ConfigParser(strict=False)
    parser.optionxform = str  # preserve case of systemd directive names
    parser.read(path)
    assert "Service" in parser, f"{path} has no [Service] section"
    return parser


@pytest.mark.parametrize("unit", CONTAINED_UNITS)
def test_drop_in_exists_and_documents_rationale(unit):
    """Every contained unit ships a comment block, not bare numbers.

    A limit without a stated reason is indistinguishable from a guess by the
    time anyone next reads it. Require at least a few lines of '#' commentary
    ahead of the [Service] section.
    """
    text = _drop_in_path(unit).read_text()
    header = text.split("[Service]")[0]
    comment_lines = [
        line for line in header.splitlines() if line.strip().startswith("#")
    ]
    assert len(comment_lines) >= 3, (
        f"{unit}'s containment drop-in needs a real rationale comment, "
        f"not just a couple of lines"
    )


@pytest.mark.parametrize("unit", CONTAINED_UNITS)
def test_memory_high_below_memory_max(unit):
    service = _load(unit)["Service"]
    assert "MemoryHigh" in service, f"{unit}: no MemoryHigh set"
    assert "MemoryMax" in service, f"{unit}: no MemoryMax set"

    high = _parse_bytes(service["MemoryHigh"])
    max_ = _parse_bytes(service["MemoryMax"])
    assert high < max_, (
        f"{unit}: MemoryHigh ({service['MemoryHigh']}) must be strictly "
        f"below MemoryMax ({service['MemoryMax']}) — High is the throttle "
        f"point, Max is the kill point; equal or inverted values collapse "
        f"the soft-pressure stage into the hard one"
    )


@pytest.mark.parametrize("unit", CONTAINED_UNITS)
def test_memory_max_is_finite(unit):
    """No MemoryMax=infinity for a unit this suite claims to contain.

    #218 documented exactly this failure mode: a limit that parses cleanly
    and does nothing. A unit in CONTAINED_UNITS with an infinite (or absent)
    MemoryMax is not contained, whatever the drop-in file's filename claims.
    """
    service = _load(unit)["Service"]
    raw = service.get("MemoryMax", "infinity")
    assert raw.strip().lower() != "infinity", f"{unit}: MemoryMax=infinity"
    # Must also parse as a real byte count, not silently fall through.
    assert _parse_bytes(raw) > 0


@pytest.mark.parametrize("unit", CONTAINED_UNITS)
def test_oom_policy_is_stop(unit):
    """OOMPolicy=stop: an OOM kill inside this unit's cgroup stops the whole
    unit so Restart= can bring it back cleanly, rather than leaving a
    supervisor running ragged without the worker it just lost."""
    service = _load(unit)["Service"]
    assert service.get("OOMPolicy") == "stop", f"{unit}: OOMPolicy != stop"


def test_memory_high_headroom_over_measured_baseline():
    """Cross-check against the measured baseline in this PR's docs.

    Pins the *relationship* the design relied on (High set above the
    steady-state figure it was measured against), not the live host's
    current MemoryCurrent — that fluctuates and this test must not read it.
    """
    # (unit, steady-state MemoryCurrent observed 2026-08-20T19:33 AEST, bytes)
    baseline = {
        "px-brain": 852 * 1024**2,  # stale two-session state at measurement time
        "px-wake-listen": 522 * 1024**2,
        "px-tts-glados": 501 * 1024**2,
        "px-frigate-stream": 122 * 1024**2,
        "px-api-server": 54 * 1024**2,
        "px-post": 52 * 1024**2,
        "px-mind": 60 * 1024**2,
        "px-alive": 32 * 1024**2,
        "px-battery-poll": 19 * 1024**2,
        "px-evolve": 15 * 1024**2,
        "px-blog": 17 * 1024**2,
        "cloudflared": 37 * 1024**2,
    }
    assert set(baseline) == set(CONTAINED_UNITS)
    for unit, observed in baseline.items():
        service = _load(unit)["Service"]
        high = _parse_bytes(service["MemoryHigh"])
        assert high >= observed, (
            f"{unit}: MemoryHigh ({service['MemoryHigh']}) is below the "
            f"measured steady-state baseline ({observed // 1024**2}M) — "
            f"deploying this would throttle normal operation immediately"
        )


def test_sum_of_memory_max_is_disclosed_not_assumed_safe():
    """The sum of every MemoryMax exceeds total host RAM by design (per-unit
    hard ceilings are not a reservation scheme) — see
    docs/operations/resource-containment.md's honesty about this. This test
    just pins that the sum is what the design doc claims, so a future edit to
    any one limit doesn't silently invalidate that documented caveat."""
    total = 0
    for unit in CONTAINED_UNITS:
        service = _load(unit)["Service"]
        total += _parse_bytes(service["MemoryMax"])
    host_ram_bytes = 3796 * 1024**2
    # This is intentionally NOT an assertion that total < host_ram_bytes.
    # It documents that it is not, so the gap is a decision, not a surprise.
    assert total > host_ram_bytes
