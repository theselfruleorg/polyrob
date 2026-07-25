import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from api.a2a.models import A2AErrorCode
from api.mcp_serve.handlers import TOOL_DESCRIPTORS
from api.mcp_serve.router import router


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()

    @app.middleware("http")
    async def _inject(request: Request, call_next):
        request.state.user_id = "u1"
        return await call_next(request)

    app.include_router(router)
    return TestClient(app)


def _rpc(client, method, params=None, id_=1):
    body = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        body["params"] = params
    if id_ is not None:
        body["id"] = id_
    return client.post("/mcp", json=body)


def test_initialize_shape(client):
    resp = _rpc(client, "initialize", params={"protocolVersion": "2099-01-01"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 1
    result = body["result"]
    # Our own protocolVersion is echoed honestly, never the client's.
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == "polyrob"
    assert isinstance(result["serverInfo"]["version"], str) and result["serverInfo"]["version"]
    assert result["capabilities"] == {"tools": {}}


def test_tools_list_returns_exactly_five_descriptors_with_input_schema(client):
    resp = _rpc(client, "tools/list")
    assert resp.status_code == 200
    body = resp.json()
    tools = body["result"]["tools"]
    assert len(tools) == 5
    assert tools == TOOL_DESCRIPTORS
    names = {t["name"] for t in tools}
    assert names == {
        "rob_usage_summary",
        "rob_goals_list",
        "rob_goal_show",
        "rob_conversations",
        "rob_pending_approvals",
    }
    for t in tools:
        assert "description" in t and t["description"]
        assert isinstance(t["inputSchema"], dict)
        assert t["inputSchema"]["type"] == "object"


def test_tools_call_unknown_tool_is_invalid_params(client):
    """T2.3 Task 2 replaced the Task-1 'not yet wired' stub with real
    dispatch (tests/unit/api/mcp_serve/test_tools_call.py covers the five
    tools + tenant isolation directly). At the router level, calling an
    unrecognized tool NAME is a JSON-RPC INVALID_PARAMS error — deterministic
    regardless of DI/container state, since the dispatch-table lookup fails
    before any store is touched."""
    resp = _rpc(client, "tools/call", params={"name": "rob_bogus_tool", "arguments": {}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] is None
    assert body["error"]["code"] == A2AErrorCode.INVALID_PARAMS == -32602


def test_unknown_method_returns_method_not_found(client):
    resp = _rpc(client, "bogus/method")
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] is None
    assert body["error"]["code"] == A2AErrorCode.METHOD_NOT_FOUND == -32601


def test_notification_with_no_id_gets_empty_200(client):
    resp = _rpc(client, "notifications/initialized", id_=None)
    assert resp.status_code == 200
    assert resp.text == ""


def test_notification_for_unknown_method_still_gets_empty_200(client):
    """A notification (no id) never gets a JSON-RPC response body, even when
    the method itself is unrecognized — per JSON-RPC notification semantics."""
    resp = _rpc(client, "bogus/method", id_=None)
    assert resp.status_code == 200
    assert resp.text == ""


def test_unauthenticated_request_gets_a2a_style_auth_error(monkeypatch):
    """No auth-injecting middleware -> get_user_permissive raises
    HTTPException(401) -> mapped to the same JSON-RPC error shape A2A
    produces (AUTHENTICATION_REQUIRED, HTTP 200 envelope)."""
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    resp = _rpc(client, "initialize")
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] is None
    assert body["error"]["code"] == A2AErrorCode.AUTHENTICATION_REQUIRED == -32004
