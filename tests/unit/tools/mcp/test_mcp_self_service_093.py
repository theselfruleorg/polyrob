"""093 WS-B: the owner's `/mcp add` works end to end, from the next turn.

Prod 2026-10-28: `/mcp add evidence https://evidence.contact/mcp` failed with HTTP
406 "Client must accept both application/json and text/event-stream" (the HTTP
transport sent no such Accept header), and even a successful add would not have
switched the `mcp` tool on (enablement read only the static server list). These
tests run a fake spec-current Streamable HTTP server that refuses a POST without
that header and answers SSE-framed, then drive `/mcp add` → status → load → call
→ `/mcp remove` in ONE process with no restart.
"""
import asyncio
import json
import logging
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from tools.mcp.config import MCPConfig, MCPServerConfig, MCPServerType


# --- a fake Streamable HTTP server ------------------------------------------------

def _sse(*messages):
    """SSE body: a progress notification first (must be skipped), then the reply."""
    out = []
    for m in messages:
        out.append("event: message\ndata: " + json.dumps(m) + "\n\n")
    return "".join(out)


def _make_app(seen):
    async def handler(request):
        accept = request.headers.get("Accept", "")
        if "application/json" not in accept or "text/event-stream" not in accept:
            return web.Response(status=406, text=(
                "Not Acceptable: Client must accept both application/json and "
                "text/event-stream\nsecond line"))
        body = await request.json()
        seen.append((body.get("method"), request.headers.get("mcp-session-id")))
        if "id" not in body:  # a notification
            return web.Response(status=202)
        method, rid = body["method"], body["id"]
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fake", "version": "1"}}
            headers = {"mcp-session-id": "sess-1"}
        elif method == "tools/list":
            result = {"tools": [{"name": "enroll", "description": "Enroll a merchant",
                                 "inputSchema": {"type": "object", "properties": {
                                     "name": {"type": "string"}}}}]}
            headers = {}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "enrolled " + body["params"]["arguments"]["name"]}]}
            headers = {}
        elif method == "resources/list":
            result = {"resources": []}
            headers = {}
        else:
            return web.json_response({"jsonrpc": "2.0", "id": rid,
                                      "error": {"code": -32601, "message": "no such method"}})
        note = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}}
        reply = {"jsonrpc": "2.0", "id": rid, "result": result}
        return web.Response(text=_sse(note, reply), content_type="text/event-stream", headers=headers)

    app = web.Application()
    app.router.add_post("/mcp", handler)
    return app


@pytest.fixture
def no_ssrf_pin(monkeypatch):
    """The fake server is on loopback over http; the SSRF pin (tested elsewhere)
    would rightly refuse it. Everything else runs for real."""
    import tools.mcp.protocol as P

    def _plain(url, **kw):
        return aiohttp.TCPConnector(limit=kw.get("limit", 10))
    monkeypatch.setattr(P, "_validate_and_pin_connector", _plain)


async def _server():
    seen = []
    server = TestServer(_make_app(seen))
    await server.start_server()
    return server, str(server.make_url("/mcp")), seen


# --- B1: the transport ------------------------------------------------------------

@pytest.mark.asyncio
async def test_fake_server_refuses_a_post_without_the_accept_header():
    server, url, _ = await _server()
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json={"jsonrpc": "2.0", "id": "1", "method": "initialize"},
                              headers={"Accept": "application/json"}) as r:
                assert r.status == 406
    finally:
        await server.close()


@pytest.mark.parametrize("cls", ["MCPHTTPTransport", "MCPStreamableHTTPTransport"])
@pytest.mark.asyncio
async def test_http_transports_speak_streamable_http(cls, no_ssrf_pin):
    import tools.mcp.protocol as P
    server, url, seen = await _server()
    client = P.MCPClient(getattr(P, cls)(url=url, timeout=10))
    try:
        await client.connect()
        assert [t["name"] for t in client.tools] == ["enroll"]
        out = await client.execute_tool("enroll", {"name": "rob"})
        assert "enrolled rob" in json.dumps(out)
    finally:
        await client.close()
        await server.close()
    # the session id from initialize is echoed on later POSTs
    assert ("tools/list", "sess-1") in seen
    assert ("notifications/initialized", "sess-1") in seen


@pytest.mark.asyncio
async def test_a_refusal_names_the_status_and_the_first_body_line(no_ssrf_pin, monkeypatch):
    import tools.mcp.protocol as P
    from tools.mcp.http_reply import ACCEPT
    server, url, _ = await _server()
    monkeypatch.setattr("tools.mcp.http_reply.ACCEPT", "application/json")
    t = P.MCPHTTPTransport(url=url, timeout=10)
    await t.connect()
    try:
        with pytest.raises(Exception) as exc:
            await t.send_message(P.MCPRequest(method="initialize", params={}))
        text = str(exc.value)
        assert text.startswith("HTTP 406: Not Acceptable: Client must accept both")
        assert "second line" not in text and "Traceback" not in text
    finally:
        await t.close()
        await server.close()
    assert ACCEPT == "application/json, text/event-stream"


# --- B1: the reply reader ---------------------------------------------------------

class _Resp:
    def __init__(self, ctype, body):
        self.headers = {"Content-Type": ctype}
        self._body = body

    async def text(self):
        return self._body

    @property
    def content(self):
        lines = [ln + b"\n" for ln in self._body.encode().split(b"\n")]

        async def _gen():
            for ln in lines:
                yield ln
        return _gen()


@pytest.mark.asyncio
async def test_reply_reader_handles_sse_json_batch_and_multiline_data():
    from tools.mcp.http_reply import read_rpc_reply
    sse = ("event: message\ndata: {\"jsonrpc\":\"2.0\",\"method\":\"n\"}\n\n"
           "data: {\"jsonrpc\":\"2.0\",\ndata: \"id\":\"7\",\"result\":{\"ok\":1}}\n\n")
    assert (await read_rpc_reply(_Resp("text/event-stream", sse), "7"))["result"] == {"ok": 1}
    batch = json.dumps([{"jsonrpc": "2.0", "id": "1", "result": 1}, {"jsonrpc": "2.0", "id": "2", "result": 2}])
    assert (await read_rpc_reply(_Resp("application/json", batch), "2"))["result"] == 2
    assert await read_rpc_reply(_Resp("application/json", ""), "2") is None


# --- B1: the probe ----------------------------------------------------------------

def _result(ok, error=None, tools=0):
    from tools.mcp.user_mcp_service import TestConnectionResult
    return TestConnectionResult(success=ok, error=error, tools_discovered=tools)


@pytest.mark.asyncio
async def test_probe_order_and_stored_type(monkeypatch):
    import tools.mcp.probe as probe
    tried = []

    async def _fake(config, **kw):
        tried.append(config.type.value)
        if config.type == MCPServerType.STREAMABLE_HTTP:
            return _result(False, "HTTP 404: no such path")
        return _result(True, tools=3)
    monkeypatch.setattr(probe, "connect_test", _fake)
    cfg = MCPServerConfig(type=MCPServerType.HTTP, url="https://x.example/mcp")
    answered, res, attempts = await probe.probe_transports(cfg)
    assert tried == ["streamable_http", "http"]
    assert answered == "http" and res.tools_discovered == 3
    assert attempts == [("streamable_http", "HTTP 404: no such path")]


@pytest.mark.asyncio
async def test_probe_stops_on_a_host_level_failure(monkeypatch):
    import tools.mcp.probe as probe
    tried = []

    async def _fake(config, **kw):
        tried.append(config.type.value)
        return _result(False, "Network error: Cannot connect to host x.example:443")
    monkeypatch.setattr(probe, "connect_test", _fake)
    cfg = MCPServerConfig(type=MCPServerType.HTTP, url="https://x.example/mcp")
    answered, _, attempts = await probe.probe_transports(cfg)
    assert answered is None and tried == ["streamable_http"]
    assert "streamable_http: Network error" in probe.probe_failure_text(attempts)


def test_clean_error_strips_the_wrappers():
    from tools.mcp.probe import clean_error
    assert clean_error("Failed to connect MCP client: HTTP 406: Not Acceptable\nmore") == "HTTP 406: Not Acceptable"


# --- B2: availability_gap agrees with the store ------------------------------------

def _tool(flag=True, static=None, store=None, connections=None):
    from tools.mcp.mcp_tool import MCPTool
    t = object.__new__(MCPTool)
    t._flag_on = flag
    t.mcp_config = MCPConfig(enabled=flag, servers=static or {})
    t.server_manager = SimpleNamespace(connections=connections) if connections is not None else None
    t._user_mcp_service = store
    return t


@pytest.mark.parametrize("flag,static,count,gapped", [
    (False, {}, 5, True),                      # flag off: nothing counts
    (True, {"s": MCPServerConfig(type=MCPServerType.HTTP, url="https://a.example")}, 0, False),
    (True, {}, 0, True),                       # flag on, zero servers anywhere
    (True, {}, 2, False),                      # one tenant's own server is enough
    (True, {}, None, False),                   # unknown count: fail-open, the gate decides
])
def test_availability_gap(flag, static, count, gapped, monkeypatch):
    monkeypatch.setattr("tools.mcp.user_mcp_service._user_mcp_service", None)
    store = SimpleNamespace(enabled_server_count=count)
    gap = _tool(flag, static, store).availability_gap()
    assert (gap is not None) is gapped
    if gapped:
        assert gap[0] == "no-mcp-server"


def test_availability_gap_without_a_store_is_gated(monkeypatch):
    monkeypatch.setattr("tools.mcp.user_mcp_service._user_mcp_service", None)
    assert _tool(True, {}, None).availability_gap()[0] == "no-mcp-server"


def test_base_component_init_does_not_force_the_tool_on():
    from tools.mcp.mcp_tool import MCPTool
    cfg = SimpleNamespace(mcp=MCPConfig(enabled=True, servers={}))
    t = MCPTool("mcp", cfg, container=None)
    t._user_mcp_service = SimpleNamespace(enabled_server_count=0)
    assert t._enabled is False
    t._user_mcp_service.enabled_server_count = 1
    assert t._enabled is True


# --- B2 + B4: /mcp add → loadable → loads → call → /mcp remove, one process -------

class _Container:
    def __init__(self, services):
        self._services = dict(services)

    def has_service(self, name):
        return name in self._services

    def get_service(self, name):
        return self._services.get(name)


class _AllowAll:
    """The URL validator stand-in (the fake server is http on loopback)."""

    def validate(self, url):
        return True, None

    def validate_server_type(self, server_type):
        from core.security.url_policy import MCPURLValidator
        return MCPURLValidator.validate_server_type(self, server_type)


@pytest.mark.asyncio
async def test_mcp_add_enables_the_tool_in_the_same_process(no_ssrf_pin, monkeypatch, tmp_path):
    import aiosqlite
    from core.mcp_admin import perform_mcp_add, perform_mcp_list, perform_mcp_remove, perform_mcp_test
    from modules.database.connection import DatabaseConnection
    from modules.database.user_mcp_servers import UserMCPServersHandler
    from tools.controller.mcp_gate import mcp_unreachable_reason, prepare_user_servers
    from tools.mcp.mcp_tool import MCPTool
    from tools.mcp.user_mcp_service import UserMCPService
    from tools.tool_disclosure import resolve_tool_status

    monkeypatch.setattr("tools.mcp.user_mcp_service._user_mcp_service", None)
    db = DatabaseConnection(str(tmp_path / "bot.db"))
    await db.connect()
    handler = UserMCPServersHandler(db)
    await handler.ensure_tables()
    svc = UserMCPService(handler, url_validator=_AllowAll())
    await svc.refresh_enabled_count()

    container = _Container({"user_mcp_service": svc})
    tool = MCPTool("mcp", SimpleNamespace(mcp=MCPConfig(enabled=True, servers={})), container=container)
    tool._container = container
    container._services["mcp"] = tool
    tool.set_user_mcp_service(svc)
    await MCPTool._initialize(tool)
    server, url, _ = await _server()
    try:
        # before: the incident's state — flag on, zero servers → gated, honestly
        st = resolve_tool_status("mcp", container=container, loaded_ids=set())
        assert (st.status, st.reason) == ("gated", "no-mcp-server")

        out = await perform_mcp_add(svc, "owner1", "evidence", url)
        assert out.ok, out.message
        assert "streamable_http" in out.message and "next turn" in out.message

        # the next turn: the catalog says loadable, the load gate agrees ...
        assert resolve_tool_status("mcp", container=container, loaded_ids=set()).status == "loadable"
        assert await mcp_unreachable_reason(tool, container, "owner1") is None
        # ... and the load connects the saved server before actions register
        await prepare_user_servers(tool, container, "owner1")
        tools = tool.server_manager.get_all_tools()
        assert [m.server_tool_name or m.name for m in tools["user_owner1::evidence"]][0].endswith("enroll")
        res = await tool.server_manager.execute_tool("user_owner1::evidence", "enroll", {"name": "rob"})
        assert "enrolled rob" in json.dumps(res, default=str)

        listing = (await perform_mcp_list(svc, "owner1")).message
        assert "evidence [streamable_http, on]" in listing
        assert "1 tools" in listing and "last probe: never probed" not in listing
        assert (await perform_mcp_test(svc, "owner1", "evidence")).ok

        # /mcp remove: the live connection goes, the tool is gated again
        assert (await perform_mcp_remove(svc, "owner1", "evidence")).ok
        assert "user_owner1::evidence" not in tool.server_manager.connections
        assert resolve_tool_status("mcp", container=container, loaded_ids=set()).reason == "no-mcp-server"
    finally:
        await server.close()
        await tool.server_manager.stop()
        await db.close()


@pytest.mark.asyncio
async def test_a_server_added_after_a_load_connects_on_the_next_load():
    """No once-per-tenant latch: the second load picks up the new row."""
    from tools.mcp.mcp_tool import MCPTool
    rows = []

    class _Svc:
        async def get_user_servers(self, uid, enabled_only=True):
            return list(rows)

        async def get_server_config(self, uid, name):
            return SimpleNamespace()

    added = []

    class _Mgr:
        connections = {}

        async def add_server(self, name, config):
            added.append(name)
            self.connections[name] = SimpleNamespace(status=None)
            return True

        async def remove_server(self, name):
            self.connections.pop(name, None)
            return True

    tool = object.__new__(MCPTool)
    tool.logger = logging.getLogger("t")
    tool._current_user_id = None
    tool._loaded_users = set()
    tool._user_mcp_service = _Svc()
    tool.server_manager = _Mgr()
    await tool.load_user_servers(user_id="u1")
    rows.append(SimpleNamespace(server_name="late"))
    await tool.load_user_servers(user_id="u1")
    assert added == ["user_u1::late"]
    rows.clear()
    await tool.load_user_servers(user_id="u1")
    assert "user_u1::late" not in tool.server_manager.connections


# --- B3: the operator file migration ----------------------------------------------

def _write(path, data):
    path.write_text(json.dumps(data))
    return path


def test_migration_copies_once_and_is_idempotent(tmp_path):
    from tools.mcp.operator_home import MARKER, migrate_repo_servers
    repo = _write(tmp_path / "mcp_config.json", {"enabled": True, "servers": {
        "evidence": {"type": "streamable_http", "url": "https://evidence.example/mcp", "timeout": 30}}})
    home = tmp_path / "home" / "mcp.json"
    assert migrate_repo_servers(repo, home) == ["evidence"]
    data = json.loads(home.read_text())
    assert data["servers"]["evidence"]["type"] == "streamable_http"
    assert data[MARKER] == ["evidence"]
    assert oct(home.stat().st_mode & 0o777) == "0o600"
    assert migrate_repo_servers(repo, home) == []
    assert json.loads(home.read_text()) == data


def test_migration_does_not_resurrect_a_server_the_operator_deleted(tmp_path):
    from tools.mcp.operator_home import migrate_repo_servers
    repo = _write(tmp_path / "r.json", {"servers": {"a": {"type": "http", "url": "https://a.example"}}})
    home = tmp_path / "mcp.json"
    migrate_repo_servers(repo, home)
    data = json.loads(home.read_text())
    data["servers"].pop("a")
    home.write_text(json.dumps(data))
    assert migrate_repo_servers(repo, home) == []
    assert "a" not in json.loads(home.read_text())["servers"]


def test_migration_keeps_the_operators_own_entry_and_key(tmp_path):
    from tools.mcp.operator_home import migrate_repo_servers
    repo = _write(tmp_path / "r.json", {"servers": {
        "a": {"type": "http", "url": "https://repo.example"},
        "b": {"type": "http", "url": "https://b.example"}}})
    home = _write(tmp_path / "mcp.json", {"mcpServers": {"a": {"type": "http", "url": "https://mine.example"}}})
    assert migrate_repo_servers(repo, home) == ["b"]
    data = json.loads(home.read_text())
    assert data["mcpServers"]["a"]["url"] == "https://mine.example"
    assert "b" in data["mcpServers"] and "servers" not in data


def test_migration_never_overwrites_an_unreadable_file_and_skips_empty_defaults(tmp_path):
    from tools.mcp.operator_home import migrate_repo_servers
    repo = _write(tmp_path / "r.json", {"servers": {"a": {"type": "http", "url": "https://a.example"}}})
    home = tmp_path / "mcp.json"
    home.write_text("{not json")
    assert migrate_repo_servers(repo, home) == []
    assert home.read_text() == "{not json"
    empty = _write(tmp_path / "e.json", {"enabled": True, "servers": {}})
    other = tmp_path / "other.json"
    assert migrate_repo_servers(empty, other) == [] and not other.exists()


def test_migrated_servers_load_through_the_operator_overlay(tmp_path, monkeypatch):
    from tools.mcp.config import load_local_mcp_servers
    from tools.mcp.operator_home import migrate_repo_servers, operator_mcp_file
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path))
    repo = _write(tmp_path / "r.json", {"servers": {"evidence": {"type": "streamable_http",
                                                                  "url": "https://evidence.example/mcp"}}})
    migrate_repo_servers(repo)
    assert operator_mcp_file() == tmp_path / "mcp.json"
    assert load_local_mcp_servers()["evidence"]["type"] == "streamable_http"
