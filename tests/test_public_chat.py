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
    with TestClient(app) as test_client:
        yield test_client


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
