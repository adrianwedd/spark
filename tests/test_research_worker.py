"""The sandboxed delegated-research worker (#281 phase 2).

Behaviour, not the boundary: the boundary is
tests/test_research_isolation_invariant.py and, on the robot, the adversarial
canary in docs/operations/agent-os-isolation-design.md. What is tested here is
that the worker can only ever read one file and write one file, and that it
never leaves the operator without a reason.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from pxh import m5, research_worker as rw


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    inbox.mkdir()
    monkeypatch.setenv("PX_RESEARCH_INBOX", str(inbox))
    monkeypatch.setenv("PX_RESEARCH_OUTBOX", str(outbox))
    return inbox, outbox


def _request(inbox: Path, prompt: str = "What changed?") -> str:
    uid = str(uuid.uuid4())
    (inbox / f"{uid}.json").write_text(json.dumps({"prompt": prompt}))
    return uid


class TestUuidIsTheOnlyWayIn:
    @pytest.mark.parametrize("bad", [
        "../../etc/passwd",
        "/etc/passwd",
        "..",
        "0" * 32,
        "6db1136d-d29b-3993-8808-e635e22c699d",          # v3, not v4
        "6DB1136D-29B3-4993-8808-E635E22C699D",          # upper case
        "6db1136d29b349938808e635e22c699d",              # no dashes
        "6db1136d-29b3-4993-8808-e635e22c699d\n",        # trailing newline
        "6db1136d-29b3-4993-8808-e635e22c699d; rm -rf /",
        "",
    ])
    def test_a_non_v4_uuid_cannot_name_a_path(self, sandbox, bad):
        with pytest.raises(ValueError):
            rw.request_path(bad)
        with pytest.raises(ValueError):
            rw.write_result(bad, {"status": "ok"})

    def test_a_real_uuid4_resolves_inside_the_inbox(self, sandbox):
        inbox, _ = sandbox
        uid = str(uuid.uuid4())
        assert rw.request_path(uid) == inbox / f"{uid}.json"


class TestOneReadableFile:
    def test_a_valid_request_reads(self, sandbox):
        inbox, _ = sandbox
        uid = _request(inbox, "Explain the IO stall.")
        assert rw.read_request(uid)["prompt"] == "Explain the IO stall."

    def test_the_default_system_prompt_is_supplied(self, sandbox):
        inbox, _ = sandbox
        assert rw.read_request(_request(inbox))["system"] == rw.SYSTEM_PROMPT

    def test_an_absent_request_is_an_error_not_a_search(self, sandbox):
        with pytest.raises(OSError):
            rw.read_request(str(uuid.uuid4()))

    def test_a_request_without_a_prompt_is_refused(self, sandbox):
        inbox, _ = sandbox
        uid = str(uuid.uuid4())
        (inbox / f"{uid}.json").write_text(json.dumps({"system": "hi"}))
        with pytest.raises(ValueError, match="prompt"):
            rw.read_request(uid)

    def test_an_oversize_request_is_refused(self, sandbox):
        inbox, _ = sandbox
        uid = str(uuid.uuid4())
        (inbox / f"{uid}.json").write_bytes(b"x" * (rw.MAX_REQUEST_BYTES + 1))
        with pytest.raises(ValueError, match="bound"):
            rw.read_request(uid)

    def test_an_overlong_prompt_is_refused(self, sandbox):
        inbox, _ = sandbox
        uid = _request(inbox, "x" * (rw.MAX_PROMPT_CHARS + 1))
        with pytest.raises(ValueError, match="bound"):
            rw.read_request(uid)


class TestOneWritableFile:
    def test_the_result_lands_in_the_outbox_and_nowhere_else(self, sandbox):
        _, outbox = sandbox
        uid = str(uuid.uuid4())
        written = rw.write_result(uid, {"status": "ok"})
        assert written == outbox / f"{uid}.json"
        assert json.loads(written.read_text()) == {"status": "ok"}
        assert [p.name for p in outbox.iterdir()] == [f"{uid}.json"]

    def test_the_write_is_atomic(self, sandbox):
        """No `.tmp` survives a completed write — the operator polls this
        directory and a half-written result would parse as a whole one."""
        _, outbox = sandbox
        rw.write_result(str(uuid.uuid4()), {"status": "ok"})
        assert not [p for p in outbox.iterdir() if p.name.startswith(".")]


class TestTheOperatorIsNeverLeftWithSilence:
    def test_a_missing_request_still_produces_a_result(self, sandbox):
        _, outbox = sandbox
        uid = str(uuid.uuid4())
        assert rw.main(["worker", uid]) == 3
        payload = json.loads((outbox / f"{uid}.json").read_text())
        assert payload["status"] == "error" and "request" in payload["error"]

    def test_a_tier_failure_still_produces_a_result(self, sandbox, monkeypatch):
        inbox, outbox = sandbox
        uid = _request(inbox)

        def _boom(*_a, **_kw):
            raise RuntimeError("tier exploded")

        monkeypatch.setattr(rw, "investigate", _boom)
        assert rw.main(["worker", uid]) == 4
        payload = json.loads((outbox / f"{uid}.json").read_text())
        assert payload["status"] == "error" and "tier exploded" in payload["error"]

    def test_a_bad_uuid_writes_nothing_anywhere(self, sandbox):
        _, outbox = sandbox
        assert rw.main(["worker", "../escape"]) == 2
        assert not outbox.exists() or not list(outbox.iterdir())

    def test_wrong_argument_counts_write_nothing(self, sandbox):
        _, outbox = sandbox
        assert rw.main(["worker"]) == 2
        assert rw.main(["worker", "a", "b"]) == 2
        assert not outbox.exists() or not list(outbox.iterdir())


class TestTheCall:
    def test_an_available_tier_is_recorded_as_ok(self, sandbox, monkeypatch):
        inbox, outbox = sandbox
        uid = _request(inbox, "Summarise this.")
        monkeypatch.setattr(m5, "ask_m5", lambda *a, **kw: m5.M5Result(
            status="available", response="  it stalled  ", model="x:cloud",
            backend="ollama-m5", duration_ms=12))

        assert rw.main(["worker", uid]) == 0
        payload = json.loads((outbox / f"{uid}.json").read_text())
        assert payload["status"] == "ok"
        assert payload["status_detail"] == "available"
        assert payload["response"] == "  it stalled  "
        assert payload["model"] == "x:cloud" and payload["backend"] == "ollama-m5"
        assert payload["uuid"] == uid

    @pytest.mark.parametrize("status", ["busy", "timeout", "offline", "bad_response"])
    def test_every_non_available_status_is_a_reason_the_operator_can_read(
            self, sandbox, monkeypatch, status):
        inbox, outbox = sandbox
        uid = _request(inbox)
        monkeypatch.setattr(m5, "ask_m5", lambda *a, **kw: m5.M5Result(
            status=status, error=f"{status} for a specific reason"))

        assert rw.main(["worker", uid]) == 4
        payload = json.loads((outbox / f"{uid}.json").read_text())
        assert payload["status"] == "error"
        assert payload["status_detail"] == status
        assert status in payload["error"]

    def test_the_call_is_no_tools_and_carries_its_own_kind(self, sandbox, monkeypatch):
        inbox, _ = sandbox
        uid = _request(inbox, "the prompt")
        seen = {}

        def _capture(kind, prompt, system, **kw):
            seen.update(kind=kind, prompt=prompt, system=system, kw=kw)
            return m5.M5Result(status="available", response="ok")

        monkeypatch.setattr(m5, "ask_m5", _capture)
        rw.main(["worker", uid])
        assert seen["kind"] == rw.TIER_KIND
        assert seen["prompt"] == "the prompt"
        assert seen["system"] == rw.SYSTEM_PROMPT
        assert "images" not in seen["kw"] or seen["kw"]["images"] is None
