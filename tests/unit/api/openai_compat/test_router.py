import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from api.openai_compat.router import router


class _FakeAgent:
    def __init__(self): self.calls = []
    async def chat_once(self, user_id, text, chat_id=None, provider=None, model=None):
        self.calls.append((user_id, text, chat_id, provider, model))
        return "hello from rob"


class _FakeContainer:
    def __init__(self, agent): self._agent = agent
    def get_agent(self, name): return self._agent


@pytest.fixture
def client_and_agent(monkeypatch):
    agent = _FakeAgent()
    app = FastAPI()
    # inject a fake authenticated user + container, bypass real auth/billing for the unit test
    @app.middleware("http")
    async def _inject(request: Request, call_next):
        request.state.user_id = "u1"
        request.state.is_admin = True  # admin bypass in verify_payment_for_request
        return await call_next(request)
    app.state.container = _FakeContainer(agent)
    # verify_payment_for_request reads the DI singleton; admin bypass returns early,
    # but to keep the unit test hermetic, stub it to a no-op pass.
    import api.openai_compat.router as r
    async def _ok(request, cost_credits=1): return ("admin_bypass", {})
    monkeypatch.setattr(r, "verify_payment_for_request", _ok)
    monkeypatch.setattr(r, "_get_container", lambda: _FakeContainer(agent))
    app.include_router(router)
    return TestClient(app), agent


def test_chat_completions_non_streaming(client_and_agent):
    client, agent = client_and_agent
    resp = client.post("/v1/chat/completions", json={
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "hey"}],
        "user": "conv-42",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "hello from rob"
    # last user message forwarded; chat_id = the OpenAI `user` field; B3:
    # body.model="gpt-4" is mapped by map_model() to (openai, gpt-4) and
    # threaded through to chat_once.
    assert agent.calls == [("u1", "hey", "conv-42", "openai", "gpt-4")]


def test_models_list(client_and_agent):
    client, _ = client_and_agent
    resp = client.get("/v1/models")
    assert resp.status_code == 200
    ids = [m["id"] for m in resp.json()["data"]]
    assert any("/" in i for i in ids)  # provider/model slugs listed


def test_models_list_excludes_non_initializable(client_and_agent):
    """Q6 (UX assessment 2026-08-07): /v1/models must not advertise a model
    that cannot run — deepseek's direct client is disabled."""
    client, _ = client_and_agent
    resp = client.get("/v1/models")
    ids = [m["id"] for m in resp.json()["data"]]
    assert ids  # still lists the initializable providers
    assert not any(i.startswith("deepseek/") for i in ids)


def test_models_list_includes_user_providers(client_and_agent, tmp_path, monkeypatch):
    """Q6: /v1/models is the only discovery surface an OpenAI SDK client has —
    a providers.yaml row's declared models must be listed, not just routable."""
    path = tmp_path / "providers.yaml"
    path.write_text(
        "providers:\n"
        "  ollama:\n"
        "    base_url: http://127.0.0.1:11434/v1\n"
        "    auth_type: none\n"
        "    transport: chat_completions\n"
        "    default_model: qwen3-coder:30b\n"
        "    models: [qwen3-coder:30b, llama3.3:70b]\n"
    )
    monkeypatch.setenv("LLM_CUSTOM_PROVIDERS", str(path))
    monkeypatch.setenv("LLM_PROVIDER_REGISTRY", "true")
    from modules.llm.provider_spec import reset_provider_registry_cache
    reset_provider_registry_cache()
    try:
        client, _ = client_and_agent
        ids = [m["id"] for m in client.get("/v1/models").json()["data"]]
        assert "ollama/qwen3-coder:30b" in ids
        assert "ollama/llama3.3:70b" in ids
    finally:
        reset_provider_registry_cache()


def test_stream_true_returns_sse(client_and_agent):
    client, _ = client_and_agent
    resp = client.post("/v1/chat/completions", json={
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
    })
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert '"object":"chat.completion.chunk"' in resp.text
    assert '"content":"hello from rob"' in resp.text
    assert "data: [DONE]" in resp.text


def test_no_user_message_returns_400(client_and_agent):
    client, _ = client_and_agent
    resp = client.post("/v1/chat/completions", json={
        "model": "gpt-4",
        "messages": [{"role": "assistant", "content": "I am here"}],
    })
    assert resp.status_code == 400


def test_missing_user_id_returns_401(monkeypatch):
    # Build an app that includes the router but injects NO request.state.user_id,
    # so the 401 guard fires before payment/agent are ever reached.
    agent = _FakeAgent()
    app = FastAPI()
    import api.openai_compat.router as r
    async def _ok(request, cost_credits=1): return ("admin_bypass", {})
    monkeypatch.setattr(r, "verify_payment_for_request", _ok)
    monkeypatch.setattr(r, "_get_container", lambda: _FakeContainer(agent))
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/v1/chat/completions", json={
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "hello"}],
    })
    assert resp.status_code == 401


def test_stream_emits_keepalive_while_chat_runs(monkeypatch):
    """019 P4: a slow chat_once produces SSE keep-alive comments BEFORE the
    reply chunks, so long turns don't idle-timeout clients."""
    import asyncio

    import api.openai_compat.router as r
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    class _SlowAgent:
        async def chat_once(self, user_id, text, chat_id=None, provider=None, model=None):
            await asyncio.sleep(0.12)
            return "slow reply"

    agent = _SlowAgent()
    app = FastAPI()

    @app.middleware("http")
    async def _inject(request: Request, call_next):
        request.state.user_id = "u1"
        request.state.is_admin = True
        return await call_next(request)

    async def _ok(request, cost_credits=1):
        return ("admin_bypass", {})

    monkeypatch.setattr(r, "verify_payment_for_request", _ok)
    monkeypatch.setattr(r, "_get_container", lambda: _FakeContainer(agent))
    monkeypatch.setattr(r, "STREAM_KEEPALIVE_SEC", 0.03)
    app.include_router(r.router)
    client = TestClient(app)

    resp = client.post("/v1/chat/completions", json={
        "model": "gpt-4o", "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert resp.status_code == 200
    body = resp.text
    assert ": keep-alive" in body
    assert '"content":"slow reply"' in body
    assert "data: [DONE]" in body
    # keep-alives arrive BEFORE the reply chunk
    assert body.index(": keep-alive") < body.index('"content":"slow reply"')


def test_stream_error_after_headers_surfaces_in_stream(monkeypatch):
    """A chat_once failure mid-stream yields an error chunk + DONE, not a dead socket."""
    import api.openai_compat.router as r
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    class _BoomAgent:
        async def chat_once(self, user_id, text, chat_id=None, provider=None, model=None):
            raise RuntimeError("provider down")

    agent = _BoomAgent()
    app = FastAPI()

    @app.middleware("http")
    async def _inject(request: Request, call_next):
        request.state.user_id = "u1"
        request.state.is_admin = True
        return await call_next(request)

    async def _ok(request, cost_credits=1):
        return ("admin_bypass", {})

    monkeypatch.setattr(r, "verify_payment_for_request", _ok)
    monkeypatch.setattr(r, "_get_container", lambda: _FakeContainer(agent))
    app.include_router(r.router)
    client = TestClient(app)

    resp = client.post("/v1/chat/completions", json={
        "model": "gpt-4o", "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert resp.status_code == 200
    assert "[error] agent turn failed" in resp.text
    assert "data: [DONE]" in resp.text
