"""Backend tests — LiteLLM is mocked, so no real API keys are needed.

Run from the repo root:  make test
or from backend/:  pytest -q
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as app_module  # noqa: E402


def _mock_response(reply="The capital of France is Paris.",
                   input_tokens=42, output_tokens=9):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=reply))],
        usage=SimpleNamespace(prompt_tokens=input_tokens,
                              completion_tokens=output_tokens),
    )


@pytest.fixture()
def client(tmp_path):
    db = tmp_path / "test.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db}"
    os.environ["LLM_API_KEYS"] = "test-key-1,test-key-2"
    os.environ["ENABLE_FAILURE_SIMULATION"] = "true"
    os.environ["LLM_MODEL"] = "gemini/gemini-3.6-flash"
    app_module._CACHE.clear()
    app_module.init_db()
    with TestClient(app_module.app, raise_server_exceptions=False) as c:
        yield c
    app_module._CACHE.clear()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "keys_configured": 2,
                        "cache_enabled": True}


def test_chat_live_and_history(client):
    with patch("litellm.completion", return_value=_mock_response()) as m:
        r = client.post("/chat", json={"session_id": "s-1",
                                       "message": "What is the capital of France?"})
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == "s-1"
    assert body["reply"] == "The capital of France is Paris."
    meta = body["metadata"]
    assert meta["resolution_source"] == "LIVE_INFERENCE"
    assert meta["model"] == "gemini/gemini-3.6-flash"
    assert meta["key_id"] == "key_1"
    assert meta["fallback_attempts"] == 0
    assert meta["input_tokens"] == 42
    assert meta["output_tokens"] == 9
    assert isinstance(meta["latency_ms"], int) and meta["latency_ms"] >= 1

    r = client.get("/sessions/s-1/messages")
    assert r.status_code == 200
    msgs = r.json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "What is the capital of France?"
    assert msgs[1]["content"] == "The capital of France is Paris."
    assert msgs[1]["metadata"]["resolution_source"] == "LIVE_INFERENCE"


def test_chat_validation_422(client):
    for body in (
        {"session_id": "s-1", "message": "   "},
        {"session_id": "", "message": "hello"},
        {"session_id": "s-1", "message": "x" * 4001},
        {"message": "hello"},
    ):
        r = client.post("/chat", json=body)
        assert r.status_code == 422, body
        assert "detail" in r.json()


def test_cache_hit_identical_and_null_key(client):
    with patch("litellm.completion", return_value=_mock_response()) as m:
        r1 = client.post("/chat", json={"session_id": "s-a",
                                        "message": "Hello there"})
        assert r1.json()["metadata"]["resolution_source"] == "LIVE_INFERENCE"
        assert m.call_count == 1
        # Same first message in a NEW session -> cache hit.
        r2 = client.post("/chat", json={"session_id": "s-b",
                                        "message": "Hello there"})
    assert r2.status_code == 200
    assert r2.json()["reply"] == r1.json()["reply"]
    meta = r2.json()["metadata"]
    assert meta["resolution_source"] == "LLM_CACHE"
    assert meta["key_id"] is None
    assert meta["fallback_attempts"] == 0
    assert meta["input_tokens"] == 42
    assert meta["output_tokens"] == 9
    # A different message must never come from cache.
    with patch("litellm.completion",
               return_value=_mock_response(reply="Different answer.")):
        r3 = client.post("/chat", json={"session_id": "s-c",
                                        "message": "Something else entirely"})
    assert r3.json()["metadata"]["resolution_source"] == "LIVE_INFERENCE"


def test_failover_key1_429_goes_to_key2(client):
    calls = []

    def fake_completion(**kwargs):
        calls.append(kwargs.get("api_key"))
        return _mock_response(reply="Answer from key 2.")

    with patch("litellm.completion", side_effect=fake_completion):
        r = client.post(
            "/chat",
            json={"session_id": "s-fail", "message": "Hi"},
            headers={"X-Simulate-Failure": "key_1=429"},
        )
    assert r.status_code == 200
    assert r.json()["reply"] == "Answer from key 2."
    meta = r.json()["metadata"]
    assert meta["key_id"] == "key_2"
    assert meta["fallback_attempts"] == 1
    assert meta["resolution_source"] == "LIVE_INFERENCE"
    assert calls == ["test-key-2"]


def test_all_keys_fail_503_and_no_history(client):
    r = client.post(
        "/chat",
        json={"session_id": "s-dead", "message": "Hi"},
        headers={"X-Simulate-Failure": "*=429"},
    )
    assert r.status_code == 503
    assert r.json() == {"detail": "all providers unavailable"}
    # Nothing was added to history — the session does not exist.
    assert client.get("/sessions/s-dead/messages").status_code == 404


def test_openrouter_fallback_when_gemini_fails(client):
    os.environ["OPENROUTER_API_KEY"] = "or-test-key"

    def fake_completion(**kwargs):
        if kwargs.get("model", "").startswith("openrouter/"):
            return _mock_response(reply="Answer via OpenRouter.")
        raise RuntimeError("gemini quota exhausted")

    try:
        with patch("litellm.completion", side_effect=fake_completion):
            r = client.post("/chat", json={"session_id": "s-or",
                                           "message": "Hi"})
    finally:
        os.environ.pop("OPENROUTER_API_KEY", None)
    assert r.status_code == 200
    assert r.json()["reply"] == "Answer via OpenRouter."
    meta = r.json()["metadata"]
    assert meta["key_id"] == "key_3"
    assert meta["fallback_attempts"] == 2
    assert meta["model"] == "gemini/gemini-3.6-flash"


def test_unknown_session_404(client):
    r = client.get("/sessions/no-such-session/messages")
    assert r.status_code == 404
    assert r.json() == {"detail": "session not found"}


def test_multi_turn_remembers_history(client):
    seen_messages = []

    def fake_completion(**kwargs):
        seen_messages.append(list(kwargs["messages"]))
        return _mock_response(reply="Noted.")

    with patch("litellm.completion", side_effect=fake_completion):
        client.post("/chat", json={"session_id": "s-mt", "message": "My name is Ashutosh"})
        client.post("/chat", json={"session_id": "s-mt", "message": "What is my name?"})
    # Second call's prompt must contain the first turn.
    second = seen_messages[1]
    texts = [m["content"] for m in second]
    assert "My name is Ashutosh" in texts
    assert "What is my name?" in texts
