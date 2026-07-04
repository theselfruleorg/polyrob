"""B3 — per-request model routing for /v1/chat/completions.

body.model is mapped via map_model() into (provider, model) and threaded
through to TaskAgent.chat_once so a per-request model actually routes,
instead of being echoed back unused.
"""
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from api.openai_compat.router import router


class _FakeAgent:
    def __init__(self):
        self.calls = []

    async def chat_once(self, user_id, text, chat_id=None, provider=None, model=None):
        self.calls.append({
            "user_id": user_id, "text": text, "chat_id": chat_id,
            "provider": provider, "model": model,
        })
        return "hello from rob"


class _FakeContainer:
    def __init__(self, agent):
        self._agent = agent

    def get_agent(self, name):
        return self._agent


@pytest.fixture
def client_and_agent(monkeypatch):
    agent = _FakeAgent()
    app = FastAPI()

    @app.middleware("http")
    async def _inject(request: Request, call_next):
        request.state.user_id = "u1"
        request.state.is_admin = True  # admin bypass in verify_payment_for_request
        return await call_next(request)

    import api.openai_compat.router as r
    async def _ok(request, cost_credits=1): return ("admin_bypass", {})
    monkeypatch.setattr(r, "verify_payment_for_request", _ok)
    monkeypatch.setattr(r, "_get_container", lambda: _FakeContainer(agent))
    app.include_router(router)
    return TestClient(app), agent


def test_provider_slugged_model_routes_provider_and_model(client_and_agent):
    client, agent = client_and_agent
    resp = client.post("/v1/chat/completions", json={
        "model": "anthropic/claude-sonnet-4-5",
        "messages": [{"role": "user", "content": "hey"}],
    })
    assert resp.status_code == 200, resp.text
    assert agent.calls[0]["provider"] == "anthropic"
    assert agent.calls[0]["model"] == "claude-sonnet-4-5"


def test_bare_model_prefix_routes_to_known_provider(client_and_agent):
    client, agent = client_and_agent
    resp = client.post("/v1/chat/completions", json={
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "hey"}],
    })
    assert resp.status_code == 200, resp.text
    assert agent.calls[0]["provider"] == "openai"
    assert agent.calls[0]["model"] == "gpt-4"


def test_response_still_echoes_the_requested_model_string(client_and_agent):
    client, agent = client_and_agent
    resp = client.post("/v1/chat/completions", json={
        "model": "anthropic/claude-sonnet-4-5",
        "messages": [{"role": "user", "content": "hey"}],
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # routing uses the mapped (provider, model) internally, but the response
    # still echoes back exactly what the caller sent for `model`.
    assert body["model"] == "anthropic/claude-sonnet-4-5"
