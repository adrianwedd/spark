# tests/test_evolve_queue.py
import json, time
import pytest


def _setup(monkeypatch, tmp_path):
    monkeypatch.setenv("PX_STATE_DIR", str(tmp_path))
    import importlib, pxh.evolve_queue as eq
    importlib.reload(eq)
    return eq


def test_enqueue_writes_full_schema(monkeypatch, tmp_path):
    eq = _setup(monkeypatch, tmp_path)
    (tmp_path / "introspection.json").write_text('{"battery": 90}')
    entry = eq.enqueue_evolve("add a joke tool", "obi", "obi-chat")
    assert entry["status"] == "pending"
    assert entry["requester"] == "obi" and entry["source"] == "obi-chat"
    assert entry["introspection"] == {"battery": 90}
    assert entry["intent"] == "add a joke tool"
    assert entry["id"].startswith("evolve-")
    line = (tmp_path / "evolve_queue.jsonl").read_text().strip()
    assert json.loads(line)["status"] == "pending"


def test_enqueue_defaults_introspection_to_empty(monkeypatch, tmp_path):
    eq = _setup(monkeypatch, tmp_path)
    entry = eq.enqueue_evolve("x feature", "obi", "obi-chat")
    assert entry["introspection"] == {}


def test_enqueue_rejects_empty_and_oversized(monkeypatch, tmp_path):
    eq = _setup(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        eq.enqueue_evolve("   ", "obi", "obi-chat")
    with pytest.raises(ValueError):
        eq.enqueue_evolve("z" * 301, "obi", "obi-chat")


def test_enqueue_sanitizes_intent(monkeypatch, tmp_path):
    eq = _setup(monkeypatch, tmp_path)
    entry = eq.enqueue_evolve("make <b>jokes</b>\nnow\x00", "obi", "obi-chat")
    assert "<" not in entry["intent"] and "\n" not in entry["intent"] and "\x00" not in entry["intent"]


def test_one_pending_per_requester(monkeypatch, tmp_path):
    eq = _setup(monkeypatch, tmp_path)
    eq.enqueue_evolve("first feature request", "obi", "obi-chat")
    with pytest.raises(eq.EvolvePendingError):
        eq.enqueue_evolve("second feature request", "obi", "obi-chat")
    # different requester is unaffected
    eq.enqueue_evolve("adrians own request", "adrian", "cli")


def test_building_status_blocks_new_enqueue(monkeypatch, tmp_path):
    # a project mid-build must block a second enqueue (quota-bypass guard)
    eq = _setup(monkeypatch, tmp_path)
    (tmp_path / "evolve_queue.jsonl").write_text(
        json.dumps({"id": "x", "intent": "in progress", "status": "building",
                    "requester": "obi"}) + "\n")
    with pytest.raises(eq.EvolvePendingError):
        eq.enqueue_evolve("another while building", "obi", "obi-chat")


def test_rate_limited_accepts_iso_ts_completed(monkeypatch, tmp_path):
    # older log entries use ISO ts_completed, not numeric ts — must still count
    eq = _setup(monkeypatch, tmp_path)
    from datetime import datetime, timezone
    iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (tmp_path / "evolve_log.jsonl").write_text(
        json.dumps({"id": "old", "status": "pr_created", "ts_completed": iso}) + "\n")
    with pytest.raises(eq.EvolveQuotaError):
        eq.enqueue_evolve("blocked by iso entry", "obi", "obi-chat")


def test_build_pr_body_flags_requester(monkeypatch, tmp_path):
    eq = _setup(monkeypatch, tmp_path)
    body = eq.build_pr_body("add joke tool", ["bin/tool-joke"], "obi", "obi-chat")
    assert "Requested by" in body and "obi" in body and "adversarial" in body.lower()
    assert "bin/tool-joke" in body


def test_reset_building_to_pending(monkeypatch, tmp_path):
    eq = _setup(monkeypatch, tmp_path)
    (tmp_path / "evolve_queue.jsonl").write_text("\n".join([
        json.dumps({"id": "a", "status": "building", "requester": "obi"}),
        json.dumps({"id": "b", "status": "pending", "requester": "obi"}),
        json.dumps({"id": "c", "status": "pr_created", "requester": "adrian"}),
    ]) + "\n")
    assert eq.reset_building_to_pending() == 1
    statuses = {e["id"]: e["status"] for e in eq.read_queue()}
    assert statuses == {"a": "pending", "b": "pending", "c": "pr_created"}


def test_rate_limited_by_recent_pr_created(monkeypatch, tmp_path):
    eq = _setup(monkeypatch, tmp_path)
    (tmp_path / "evolve_log.jsonl").write_text(
        json.dumps({"ts": time.time(), "id": "evolve-x", "status": "pr_created"}) + "\n")
    with pytest.raises(eq.EvolveQuotaError):
        eq.enqueue_evolve("blocked", "obi", "obi-chat")


def test_old_pr_created_does_not_block(monkeypatch, tmp_path):
    eq = _setup(monkeypatch, tmp_path)
    (tmp_path / "evolve_log.jsonl").write_text(
        json.dumps({"ts": time.time() - 90000, "id": "old", "status": "pr_created"}) + "\n")
    entry = eq.enqueue_evolve("allowed", "obi", "obi-chat")
    assert entry["status"] == "pending"
