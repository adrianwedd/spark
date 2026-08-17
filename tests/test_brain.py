"""Tests for the brain mailbox — the channel that replaces `claude -p`.

The protocol is a filesystem handshake between two processes that never see
each other, so the interesting cases are all failure cases: a session that never
answers, two callers racing, a reply that arrives after the caller gave up, and
a reply tool being fed something a language model made up. The happy path is
covered by a round-trip through the *real* `bin/tool-brain-reply`, because a
mocked reply writer would prove nothing about the handshake.
"""
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from pxh import brain, tmux_claude

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "bin" / "tool-brain-reply"


@pytest.fixture(autouse=True)
def _mailbox(tmp_path, monkeypatch):
    """Point the mailbox at tmp and make waiting instant."""
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PROJECT_ROOT", str(ROOT))
    monkeypatch.setattr(brain, "POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(brain, "READY_WAIT_S", 0.05)
    monkeypatch.setattr(brain, "LOCK_WAIT_S", 0.5)
    return tmp_path / "state"


@pytest.fixture
def _live_pane(monkeypatch):
    """A tmux session that exists, is ready, and swallows every injection."""
    injected: list[str] = []
    monkeypatch.setattr(tmux_claude, "ensure_session", lambda *a, **k: True)
    monkeypatch.setattr(tmux_claude, "pane_ready", lambda *a, **k: True)
    monkeypatch.setattr(tmux_claude, "inject",
                        lambda text, spec=None: (injected.append(text), True)[1])
    return injected


def _reply_via_tool(session: str, request_id: str, payload: dict) -> subprocess.CompletedProcess:
    """Answer a request the way a real session does — by running the tool."""
    env = os.environ.copy()
    env["PX_BRAIN_SESSION"] = session
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [str(TOOL), request_id, json.dumps(payload)],
        capture_output=True, text=True, timeout=30, env=env,
    )


def _pending_id(session: str, timeout_s: float = 5.0) -> str:
    """Wait for a request to appear in the inbox and return its id."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        entries = list(brain.inbox_dir(session).glob("*.json"))
        if entries:
            return entries[0].stem
        time.sleep(0.01)
    raise AssertionError("no request appeared in the inbox")


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------

def test_round_trip_through_the_real_reply_tool(_live_pane):
    """A fake brain answers via bin/tool-brain-reply and ask_brain returns it."""
    session = brain.session_for_kind("research")
    result: dict = {}

    def _fake_brain():
        request_id = _pending_id(session)
        request = json.loads((brain.inbox_dir(session) / f"{request_id}.json").read_text())
        result["saw"] = request
        _reply_via_tool(session, request_id, {"verdict": "yes"})

    worker = threading.Thread(target=_fake_brain, daemon=True)
    worker.start()
    reply = brain.ask_brain("research", {"question": "why"}, timeout_s=15)
    worker.join(timeout=10)

    assert reply is not None, "a reply written to the outbox must be returned"
    assert reply["reply"] == {"verdict": "yes"}
    assert result["saw"]["payload"] == {"question": "why"}
    assert result["saw"]["kind"] == "research"


def test_nudge_names_the_request_file_and_the_reply_verb(_live_pane):
    """The session may have drifted or been /cleared — the nudge has to carry
    the whole protocol, not assume the system prompt is still in context."""
    session = brain.session_for_kind("research")
    threading.Thread(
        target=lambda: _reply_via_tool(session, _pending_id(session), {"ok": True}),
        daemon=True,
    ).start()
    brain.ask_brain("research", {"q": 1}, timeout_s=15)

    nudge = [line for line in _live_pane if "NEW REQUEST" in line]
    assert nudge, f"no nudge injected, got {_live_pane}"
    assert "tool-brain-reply" in nudge[0]
    assert str(brain.inbox_dir(session)) in nudge[0]


# ---------------------------------------------------------------------------
# Failure paths — every one of these must fall back, never raise
# ---------------------------------------------------------------------------

def test_timeout_returns_none_so_the_caller_falls_back(_live_pane):
    """No answer is not an error. Callers drop to the Ollama tiers on None."""
    assert brain.ask_brain("research", {"q": 1}, timeout_s=0.2) is None


def test_timeout_clears_current_json(_live_pane):
    """current.json is what wedge detection keys on. A caller-side timeout is
    not a wedge, so an abandoned request must not leave one behind."""
    session = brain.session_for_kind("research")
    brain.ask_brain("research", {"q": 1}, timeout_s=0.2)
    assert not brain.current_path(session).exists()
    assert not list(brain.inbox_dir(session).glob("*.json")), \
        "an abandoned request must not stay pending"


def test_a_dead_session_returns_none(monkeypatch):
    monkeypatch.setattr(tmux_claude, "ensure_session", lambda *a, **k: False)
    assert brain.ask_brain("research", {"q": 1}, timeout_s=1) is None


def test_a_busy_pane_is_never_injected_into(monkeypatch):
    """Injecting mid-turn splices two prompts into one — worse than falling
    back, because the answer looks plausible."""
    injected: list[str] = []
    monkeypatch.setattr(tmux_claude, "ensure_session", lambda *a, **k: True)
    monkeypatch.setattr(tmux_claude, "pane_ready", lambda *a, **k: False)
    monkeypatch.setattr(tmux_claude, "inject",
                        lambda text, spec=None: (injected.append(text), True)[1])
    assert brain.ask_brain("research", {"q": 1}, timeout_s=1) is None
    assert injected == []


def test_second_caller_falls_back_rather_than_interleaving(_live_pane, monkeypatch):
    """Single-flight: two concurrent send-keys runs garble each other, so the
    loser of the lock must fall back instead of waiting behind a long turn."""
    session = brain.session_for_kind("research")
    monkeypatch.setattr(brain, "LOCK_WAIT_S", 0.1)

    results: list = []
    holder = threading.Thread(
        target=lambda: results.append(brain.ask_brain("research", {"q": 1}, timeout_s=2)),
        daemon=True,
    )
    holder.start()
    _pending_id(session)  # the holder now owns the lock
    assert brain.ask_brain("research", {"q": 2}, timeout_s=2) is None, \
        "a second concurrent request must fall back, not queue"
    holder.join(timeout=10)


def test_a_reply_that_lands_after_the_deadline_is_not_returned_later(_live_pane):
    """A late reply belongs to a request nobody is waiting on. It must not be
    picked up by the *next* request, which would answer the wrong question."""
    session = brain.session_for_kind("research")
    assert brain.ask_brain("research", {"q": 1}, timeout_s=0.2) is None
    stale_id = str(uuid.uuid4())
    brain.ensure_mailbox(session)
    (brain.outbox_dir(session) / f"{stale_id}.json").write_text(
        json.dumps({"id": stale_id, "reply": {"verdict": "stale"}}))

    assert brain.ask_brain("research", {"q": 2}, timeout_s=0.2) is None, \
        "an unrelated outbox file must not satisfy a new request"


def test_unparseable_reply_degrades_to_a_timeout(_live_pane, monkeypatch):
    """A half-written file is treated as absent, not as an error — a wrong
    answer is worse than a fallback."""
    session = brain.session_for_kind("research")

    def _garbage():
        request_id = _pending_id(session)
        (brain.outbox_dir(session) / f"{request_id}.json").write_text("{not json")

    threading.Thread(target=_garbage, daemon=True).start()
    assert brain.ask_brain("research", {"q": 1}, timeout_s=0.6) is None


# ---------------------------------------------------------------------------
# Session routing and the trust boundary
# ---------------------------------------------------------------------------

def test_untrusted_kinds_route_to_the_io_session():
    """Text SPARK did not write never reaches the privileged session."""
    for kind in ("post_qa", "public_chat", "obi_chat"):
        assert brain.session_for_kind(kind) == brain.IO_SESSION, kind
    for kind in ("research", "compose", "evolve", "reflection"):
        assert brain.session_for_kind(kind) == brain.BRAIN_SESSION, kind


def test_io_session_holds_exactly_one_tool_and_no_repo_access():
    """The io envelope IS the security property — assert it, don't assume it."""
    spec = brain.spec_for_session(brain.IO_SESSION)
    tools = spec.env["PX_CLAUDE_ALLOWED_TOOLS"]
    assert "tool-brain-reply" in tools
    assert tools.count("Bash(") == 1, f"io session must hold one tool, got {tools}"
    assert not Path(spec.cwd).samefile(ROOT) if Path(spec.cwd).exists() else True
    assert str(ROOT) != spec.cwd, "io session must not run at the repo root"


def test_model_is_only_switched_when_it_actually_changes(_live_pane):
    """/model costs a turn. The marker exists so a run of same-model requests
    does not pay for it every time."""
    session = brain.session_for_kind("research")

    def _answer():
        _reply_via_tool(session, _pending_id(session), {"ok": True})

    for _ in range(2):
        threading.Thread(target=_answer, daemon=True).start()
        brain.ask_brain("research", {"q": 1}, timeout_s=15, model="claude-haiku-4-5")

    switches = [line for line in _live_pane if line.startswith("/model")]
    assert len(switches) == 1, f"expected one /model switch, got {switches}"


# ---------------------------------------------------------------------------
# Mailbox housekeeping
# ---------------------------------------------------------------------------

def test_mailbox_dirs_are_world_writable(_mailbox):
    """Mixed-uid writers: a root-created 0755 dir locks every pi daemon out of
    atomic_write's mkstemp. Same lesson as state/health/."""
    brain.ensure_mailbox(brain.BRAIN_SESSION)
    for path in (brain.inbox_dir(brain.BRAIN_SESSION),
                 brain.outbox_dir(brain.BRAIN_SESSION),
                 brain.dead_dir(brain.BRAIN_SESSION)):
        assert path.stat().st_mode & 0o7777 == 0o1777, f"{path} must be 1777"


def test_sweep_moves_pending_requests_to_dead(_mailbox):
    """A restarted session must never replay requests whose callers have
    already fallen back and moved on."""
    session = brain.BRAIN_SESSION
    brain.ensure_mailbox(session)
    ids = [str(uuid.uuid4()) for _ in range(3)]
    for rid in ids:
        (brain.inbox_dir(session) / f"{rid}.json").write_text("{}")
    brain.current_path(session).write_text("{}")

    assert brain.sweep_pending(session) == 3
    assert not list(brain.inbox_dir(session).glob("*.json"))
    assert len(list(brain.dead_dir(session).glob("*.json"))) == 3
    assert not brain.current_path(session).exists()


def test_meter_counts_every_request(_live_pane):
    """Reflection Tier 2 bypassed budget accounting entirely and spent
    hundreds of unbudgeted calls. ask_brain is the chokepoint that cannot be
    bypassed, so the count lives here."""
    brain.ask_brain("research", {"q": 1}, timeout_s=0.2)
    brain.ask_brain("research", {"q": 2}, timeout_s=0.2)
    assert brain.meter_summary()["by_kind"]["research"] == 2


# ---------------------------------------------------------------------------
# tool-brain-reply validation — the io session's only tool
# ---------------------------------------------------------------------------

def _run_tool(session: str, request_id: str, raw_payload: str):
    env = os.environ.copy()
    env["PX_BRAIN_SESSION"] = session
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([str(TOOL), request_id, raw_payload],
                          capture_output=True, text=True, timeout=30, env=env)


@pytest.mark.parametrize("bad_id", [
    "../../../etc/passwd",
    "not-a-uuid",
    "",
    "../" + str(uuid.uuid4()),
    str(uuid.uuid4()) + "/../escape",
])
def test_reply_tool_rejects_ids_that_are_not_bare_uuids(_mailbox, bad_id):
    """The id becomes a filename. This is the path-traversal guard."""
    brain.ensure_mailbox(brain.IO_SESSION)
    out = _run_tool(brain.IO_SESSION, bad_id, '{"a": 1}')
    payload = json.loads(out.stdout.strip().splitlines()[-1])
    assert payload["status"] == "error", payload
    assert not list(brain.outbox_dir(brain.IO_SESSION).glob("*")), \
        "a rejected id must not write anything"


def test_reply_tool_rejects_an_id_nobody_is_waiting_on(_mailbox):
    """Without this, a valid uuid is a write primitive aimed at the outbox."""
    brain.ensure_mailbox(brain.IO_SESSION)
    out = _run_tool(brain.IO_SESSION, str(uuid.uuid4()), '{"a": 1}')
    assert json.loads(out.stdout.strip().splitlines()[-1])["status"] == "error"


def test_reply_tool_rejects_non_json_and_oversized_payloads(_mailbox):
    session = brain.IO_SESSION
    brain.ensure_mailbox(session)
    rid = str(uuid.uuid4())
    (brain.inbox_dir(session) / f"{rid}.json").write_text("{}")

    bad = _run_tool(session, rid, "this is not json")
    assert json.loads(bad.stdout.strip().splitlines()[-1])["status"] == "error"

    huge = json.dumps({"x": "a" * (brain.MAX_REPLY_BYTES + 1)})
    big = _run_tool(session, rid, huge)
    assert json.loads(big.stdout.strip().splitlines()[-1])["status"] == "error"

    assert (brain.inbox_dir(session) / f"{rid}.json").exists(), \
        "a rejected reply must leave the request pending"


def test_reply_tool_retires_the_request_on_success(_mailbox):
    session = brain.IO_SESSION
    brain.ensure_mailbox(session)
    rid = str(uuid.uuid4())
    (brain.inbox_dir(session) / f"{rid}.json").write_text("{}")

    out = _run_tool(session, rid, '{"verdict": "yes"}')
    assert json.loads(out.stdout.strip().splitlines()[-1])["status"] == "ok", out.stdout
    assert not (brain.inbox_dir(session) / f"{rid}.json").exists()
    written = json.loads((brain.outbox_dir(session) / f"{rid}.json").read_text())
    assert written["reply"] == {"verdict": "yes"}


# ---------------------------------------------------------------------------
# Async seam
# ---------------------------------------------------------------------------

def test_ask_brain_async_does_not_block_the_event_loop(monkeypatch):
    """api.py's handlers are async. A blocking poll on the loop thread would
    stall every other request for the length of a Claude turn."""
    import asyncio

    loop_thread = threading.get_ident()
    seen: dict = {}

    def _fake_ask(kind, payload, timeout_s=None, model=None):
        seen["thread"] = threading.get_ident()
        return {"reply": "ok"}

    monkeypatch.setattr(brain, "ask_brain", _fake_ask)

    async def _go():
        return await brain.ask_brain_async("public_chat", {"m": "hi"})

    assert asyncio.run(_go()) == {"reply": "ok"}
    assert seen["thread"] != loop_thread, "ask_brain must run off the event loop"


def test_module_contains_no_claude_p_subprocess():
    """The whole point. A regression here is the thing Adrian has been asking
    to be rid of for months."""
    source = (ROOT / "src" / "pxh" / "brain.py").read_text()
    assert '"-p"' not in source and "'-p'" not in source
    # Prose about subprocesses is fine — reaching for one is not.
    assert "import subprocess" not in source
    assert "subprocess.run" not in source and "subprocess.Popen" not in source, \
        "brain.py must reach Claude through the resident session, not a subprocess"
