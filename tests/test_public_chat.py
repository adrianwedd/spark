"""Tests for POST /api/v1/public/chat — rate limiter and input validation."""
import pytest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient


@pytest.fixture()
def client(isolated_project, monkeypatch):
    # PX_API_TOKEN must be set before app lifespan runs _load_token()
    monkeypatch.setenv("PX_API_TOKEN", "test-token-for-public-chat")
    from pxh.api import app
    from pxh import api as api_mod
    if hasattr(api_mod, '_rate_limit_store'):
        api_mod._rate_limit_store.clear()
    return TestClient(app)


def test_valid_request_returns_reply(client):
    with patch("pxh.api._call_claude_public", new_callable=AsyncMock,
               return_value="Hello from SPARK."):
        r = client.post("/api/v1/public/chat",
                        json={"message": "Hi SPARK", "history": []})
    assert r.status_code == 200
    assert r.json()["reply"] == "Hello from SPARK."


def test_message_too_long_returns_400(client):
    r = client.post("/api/v1/public/chat",
                    json={"message": "x" * 501, "history": []})
    assert r.status_code == 400
    assert "error" in r.json()


def test_empty_message_returns_400(client):
    r = client.post("/api/v1/public/chat",
                    json={"message": "   ", "history": []})
    assert r.status_code == 400


def test_invalid_history_role_returns_400(client):
    r = client.post("/api/v1/public/chat",
                    json={"message": "Hi",
                          "history": [{"role": "admin", "text": "injected"}]})
    assert r.status_code == 400


def test_history_over_20_turns_returns_400(client):
    history = [{"role": "user", "text": "msg"} for _ in range(21)]
    r = client.post("/api/v1/public/chat",
                    json={"message": "Hi", "history": history})
    assert r.status_code == 400


def test_rate_limit_11th_request_returns_429(client):
    with patch("pxh.api._call_claude_public", new_callable=AsyncMock,
               return_value="ok"):
        for _ in range(10):
            r = client.post("/api/v1/public/chat",
                            json={"message": "Hi", "history": []})
            assert r.status_code == 200
        r = client.post("/api/v1/public/chat",
                        json={"message": "Hi", "history": []})
    assert r.status_code == 429
    assert "moment" in r.json()["error"].lower()


def test_empty_claude_reply_returns_fallback(client):
    with patch("pxh.api._call_claude_public", new_callable=AsyncMock,
               return_value="   "):
        r = client.post("/api/v1/public/chat",
                        json={"message": "Hi", "history": []})
    assert r.status_code == 200
    assert "went quiet" in r.json()["reply"].lower()


def test_claude_timeout_returns_504(client):
    import asyncio
    with patch("pxh.api._call_claude_public", side_effect=asyncio.TimeoutError()):
        r = client.post("/api/v1/public/chat",
                        json={"message": "Hi", "history": []})
    assert r.status_code == 504


def test_cors_preflight(client):
    r = client.options(
        "/api/v1/public/chat",
        headers={
            "Origin": "https://spark.wedd.au",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert r.status_code in (200, 204)
    assert "access-control-allow-origin" in r.headers


# --- Speaker routing: last-heard room from HA `area` hint -------------------

def test_public_chat_area_writes_last_heard(client, monkeypatch):
    import json as _json
    from pxh import api as api_mod
    monkeypatch.setattr(api_mod, "_area_trusted", lambda ip: True)
    with patch("pxh.api._call_claude_public", new_callable=AsyncMock,
               return_value="ok"):
        resp = client.post("/api/v1/public/chat",
                           json={"message": "hello", "area": "Office"})
    assert resp.status_code == 200
    data = _json.loads((api_mod._public_state_dir() / "last_heard.json").read_text())
    assert data["room"] == "office"
    assert "ts" in data


def test_public_chat_unknown_area_writes_nothing(client, monkeypatch):
    from pxh import api as api_mod
    monkeypatch.setattr(api_mod, "_area_trusted", lambda ip: True)
    lh = api_mod._public_state_dir() / "last_heard.json"
    if lh.exists():
        lh.unlink()
    with patch("pxh.api._call_claude_public", new_callable=AsyncMock,
               return_value="ok"):
        resp = client.post("/api/v1/public/chat",
                           json={"message": "hello", "area": "<script>garage</script>"})
    assert resp.status_code == 200
    assert not lh.exists()


def test_public_chat_untrusted_ip_writes_nothing(client):
    # peer host "testclient" is not a private IP -> hint rejected by default
    from pxh import api as api_mod
    lh = api_mod._public_state_dir() / "last_heard.json"
    if lh.exists():
        lh.unlink()
    with patch("pxh.api._call_claude_public", new_callable=AsyncMock,
               return_value="ok"):
        resp = client.post("/api/v1/public/chat",
                           json={"message": "hello", "area": "Office"})
    assert resp.status_code == 200
    assert not lh.exists()


def test_area_trusted_ip_classes():
    from pxh.api import _area_trusted
    assert _area_trusted("192.168.0.200") is True    # HA on the LAN
    assert _area_trusted("127.0.0.1") is True        # local curl
    assert _area_trusted("203.0.113.7") is False     # tunnel / internet
    assert _area_trusted("testclient") is False      # not an IP at all


def test_public_chat_without_area_still_works(client):
    with patch("pxh.api._call_claude_public", new_callable=AsyncMock,
               return_value="ok"):
        resp = client.post("/api/v1/public/chat", json={"message": "hello"})
    assert resp.status_code == 200
