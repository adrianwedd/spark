"""The sandboxed delegated-research worker (#281 phase 2).

This module is what runs *inside* the OS boundary that
`docs/operations/agent-os-isolation-design.md` describes: a transient
`systemd-run` unit started by the root-owned `px-research-run` launcher, as a
`DynamicUser` named `spark-research`, with the production checkout read-only,
`state/` and `.env` unreachable, no device nodes, no new privileges and no
capabilities.

Two things follow from that, and they are the whole of this module's design:

**It cannot read anything but its own request.** `read_request` opens exactly
one path — `<inbox>/<uuid>.json` — and the uuid was validated as a bare v4
uuid before it became a filename component. There is no search path, no
glob, and no configuration file it reads to decide what to do. A request
carries a prompt; that is the entire input vocabulary.

**It cannot write anywhere but the outbox.** `write_result` is the only
writer, it resolves its destination under the outbox directory, and it
refuses a uuid that is not a bare v4 uuid. This is what makes the flow-back
rule mechanical rather than aspirational: findings arrive as one file a
human applies, never as an edit to the checkout the worker was given.

The worker is *deliberately* unable to touch production. When it needs
something that only the operator can do, the answer is a line in its own
result file, not an action. The same posture `px-evolve` already takes with
its PR gate, applied to an agent that runs under a different uid.

Not a security boundary *of its own*: run this module as `pi` and none of the
above is enforced by anything but these lines. The enforcement is the unit
properties the launcher sets, and the reason this file is pi-owned and the
checkout is mounted read-only is that the sandboxed uid must be able to
*execute* this code without being able to *change* it.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

# A bare v4 uuid, matched greedily-anchored: this is the only caller-controlled
# string that becomes a filesystem path in this process, and it is the same
# shape `px-research-run` validates before it starts the unit (defence in
# depth, not a duplicate — the launcher's job is to keep a bad uuid out of
# systemd, this one's is to keep it out of a path).
UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

DEFAULT_INBOX = Path("/var/lib/px-research/inbox")
DEFAULT_OUTBOX = Path("/var/lib/px-research/outbox")

# Bounds, chosen for the box rather than for the model: a Pi with an SD card
# that charges ~86 ms per synchronous write (#405) is not the place for an
# unbounded request or an unbounded answer, and an outbox file a human has to
# read has no use for a megabyte.
MAX_REQUEST_BYTES = 64 * 1024
MAX_PROMPT_CHARS = 8000
MAX_SYSTEM_CHARS = 2000
MAX_RESPONSE_CHARS = 20000

# The kind this worker bills under. It is a label on the tier's own meter
# (`pxh.m5._record_request`), so a delegated research call is attributable in
# the same evidence a human-invoked one is.
TIER_KIND = "research"

SYSTEM_PROMPT = (
    "You are a delegated research assistant for SPARK, a small robot. You have "
    "no tools, no shell, and no filesystem access: the request below is "
    "everything you can see, and your answer is the only thing you produce. "
    "Answer concisely and concretely, state what you are unsure of, and do not "
    "claim to have checked anything you were not given."
)


def inbox_dir() -> Path:
    return Path(os.environ.get("PX_RESEARCH_INBOX", str(DEFAULT_INBOX)))


def outbox_dir() -> Path:
    return Path(os.environ.get("PX_RESEARCH_OUTBOX", str(DEFAULT_OUTBOX)))


def is_valid_uuid(value: str) -> bool:
    """`fullmatch`, not `match`: Python's `$` matches *before* a trailing
    newline, and a newline is a legal character in a filename on this host.
    The bash side has no such quirk (`=~ ^...$` anchors to the whole string),
    which is exactly why the two are checked against the same input in the
    test suite rather than assumed to agree."""
    return bool(UUID4_RE.fullmatch(value or ""))


def request_path(uuid: str) -> Path:
    """`<inbox>/<uuid>.json`, or ValueError for anything else.

    No `Path.resolve()` fallback and no "search the inbox for a file that
    matches" behaviour on purpose: a resolver is a way for a caller to name a
    path this process did not sanction, and there is nothing here that needs
    one.
    """
    if not is_valid_uuid(uuid):
        raise ValueError(f"not a bare v4 uuid: {uuid!r}")
    return inbox_dir() / f"{uuid}.json"


def read_request(uuid: str) -> dict:
    """The one request this process is allowed to read."""
    path = request_path(uuid)
    raw = path.read_bytes()
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError(
            f"request is {len(raw)} bytes, over the {MAX_REQUEST_BYTES} bound"
        )
    doc = json.loads(raw.decode("utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("request must be a JSON object")
    prompt = doc.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("request needs a non-empty 'prompt' string")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(
            f"prompt is {len(prompt)} chars, over the {MAX_PROMPT_CHARS} bound"
        )
    system = doc.get("system")
    if system is not None and (not isinstance(system, str)
                              or len(system) > MAX_SYSTEM_CHARS):
        raise ValueError(f"'system' must be a string under {MAX_SYSTEM_CHARS} chars")
    return {"prompt": prompt, "system": system or SYSTEM_PROMPT}


def write_result(uuid: str, payload: dict) -> Path:
    """Write the one file this process is allowed to write.

    Atomic (tmp + rename) because the operator polls this directory: a
    half-written result that parses as JSON is worse than no file, since it
    would be applied as though it were complete.
    """
    if not is_valid_uuid(uuid):
        raise ValueError(f"not a bare v4 uuid: {uuid!r}")
    out = outbox_dir()
    out.mkdir(parents=True, exist_ok=True)
    dest = out / f"{uuid}.json"
    tmp = out / f".{uuid}.json.tmp"
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, dest)
    return dest


def investigate(request: dict) -> dict:
    """One no-tools tier call, and the evidence that it happened.

    Imported here rather than at module scope so that importing this module
    for a test or an invariant check cannot fail on a host without the tier
    configured (`pxh.m5` reads its environment at import).
    """
    from pxh import m5

    started = time.monotonic()
    result = m5.ask_m5(TIER_KIND, request["prompt"], request["system"])
    # `M5Status` has no "ok": "available" is the one status that means the
    # tier answered. Everything else is a reason, and the reason is what the
    # operator needs to see — `busy` and `offline` are not the same failure
    # and must not read the same in the outbox.
    return {
        "status": "ok" if result.status == "available" else "error",
        "status_detail": result.status,
        "response": (result.response or "")[:MAX_RESPONSE_CHARS],
        "error": result.error or "",
        "backend": result.backend or "",
        "model": result.model or "",
        "duration_ms": result.duration_ms,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: research-worker <uuid4>", file=sys.stderr)
        return 2
    uuid = argv[1]
    if not is_valid_uuid(uuid):
        print(f"research-worker: not a bare v4 uuid: {uuid!r}", file=sys.stderr)
        return 2

    base = {"uuid": uuid, "worker_pid": os.getpid(), "utc": _utc()}
    try:
        request = read_request(uuid)
    except (OSError, ValueError) as exc:
        # A request that cannot be read still produces a result file, so the
        # operator is never left with silence and no reason for it.
        write_result(uuid, dict(base, status="error", error=f"request: {exc}"))
        print(f"research-worker: {exc}", file=sys.stderr)
        return 3

    try:
        payload = investigate(request)
    except Exception as exc:  # noqa: BLE001 - the result file is the report
        write_result(uuid, dict(base, status="error",
                                error=f"{type(exc).__name__}: {exc}"))
        print(f"research-worker: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 4

    write_result(uuid, dict(base, **payload))
    return 0 if payload.get("status") == "ok" else 4


def _utc() -> str:
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


if __name__ == "__main__":  # pragma: no cover - exercised via bin/px-research-worker
    sys.exit(main(sys.argv))
