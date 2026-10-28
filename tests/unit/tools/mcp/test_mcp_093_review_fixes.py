"""093 review fixes for MCP self-service — one regression per finding.

1. the `/mcp` admin path is async end to end; a store change never touches the
   live manager from another loop;
2. a session that already holds `mcp` sees an added/removed server (actions
   registered/unregistered) without a restart;
3. an explicit MCP_ENABLED=false wins over the operator file;
4. one shared manager: another tenant's servers never open the gate, register
   actions, or execute;
5. one session's end does not unload servers another live session still uses;
6. a tool turned off by a missing service says so (not "no server");
7. the migration writes into the key that holds the operator's entries;
8. a JSON-RPC error with `id: null` answers the pending request;
9. the API add defaults to `auto` (probe) and accepts `streamable_http`.
"""
import asyncio
import json
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from tools.mcp.server_manager import ServerStatus


def _conn(*tool_names, status=ServerStatus.CONNECTED):
    return SimpleNamespace(status=status, last_error=None, tools=[
        SimpleNamespace(name=n, input_schema={}, description="", annotations=None)
        for n in tool_names])


class _Mgr:
    def __init__(self, connections=None):
        self.connections = dict(connections or {})
        self.removed = []

    def get_all_tools(self):
        return {n: list(c.tools) for n, c in self.connections.items()
                if c.status == ServerStatus.CONNECTED}

    async def remove_server(self, name):
        self.removed.append((name, asyncio.get_running_loop()))
        self.connections.pop(name, None)
        return True

    async def disconnect_server(self, name):
        self.removed.append((name, asyncio.get_running_loop()))
        return True


def _tool(manager=None, *, store=None):
    from tools.mcp.mcp_tool import MCPTool
    t = object.__new__(MCPTool)
    t.logger = logging.getLogger("mcp-review")
    t._current_user_id = None
    t._loaded_users = set()
    t._user_mcp_service = store
    t._controller_links = {}
    t._loop = None
    t._flag_on = True
    t.server_manager = manager
    return t


def _controller(user_id, session_id="s1"):
    from tools.controller.mcp_registrar import MCPActionRegistrar
    from tools.controller.registry.service import Registry

    class _Ctrl:
        pass

    c = _Ctrl()
    c.registry = Registry()          # the REAL registry (it has no `.actions`)
    c.logger = logging.getLogger("mcp-review-ctrl")
    c.user_id = user_id
    c.session_id = session_id
    reg = MCPActionRegistrar(c)

    async def _sync(tool):
        await reg.register(tool)
    c._register_mcp_tools_as_direct_actions = _sync
    return c


def _names(ctrl):
    return {n for n, a in ctrl.registry.get_actions().items()
            if getattr(getattr(a, "function", None), "_mcp_server", None)}


# --- 1. async admin path; the listener stays on the tool's loop --------------

def test_the_sync_wrapper_refuses_to_block_a_running_loop():
    from core.mcp_admin import mcp_reply, mcp_reply_async

    async def _inside():
        with pytest.raises(RuntimeError):
            mcp_reply("owner1", ["help"])
        return await mcp_reply_async("owner1", ["help"])
    assert "Usage" in asyncio.run(_inside())
    assert "Usage" in mcp_reply("owner1", ["help"])  # no loop: fine


def test_a_store_change_from_a_foreign_loop_runs_on_the_tools_loop():
    """The prod shape: the change arrives on a throwaway loop in a worker
    thread. The manager must be touched on ITS loop, and the caller not block."""
    from tools.mcp.user_servers import schedule_store_change
    tool_loop = asyncio.new_event_loop()
    thread = threading.Thread(target=tool_loop.run_forever, daemon=True)
    thread.start()
    try:
        mgr = _Mgr({"user_u1::a": _conn("t")})
        tool = _tool(mgr)
        tool._loop = tool_loop

        async def _foreign():
            return schedule_store_change(tool, "u1", "a", "removed")
        assert asyncio.run(_foreign()) is None          # handed off, not awaited
        deadline = time.time() + 5
        while not mgr.removed and time.time() < deadline:
            time.sleep(0.01)
        assert mgr.removed and mgr.removed[0][0] == "user_u1::a"
        assert mgr.removed[0][1] is tool_loop
    finally:
        tool_loop.call_soon_threadsafe(tool_loop.stop)
        thread.join(5)
        tool_loop.close()


def test_a_store_change_with_no_running_tool_loop_touches_nothing():
    from tools.mcp.user_servers import schedule_store_change
    mgr = _Mgr({"user_u1::a": _conn("t")})
    tool = _tool(mgr)

    async def _go():
        return schedule_store_change(tool, "u1", "a", "removed")
    assert asyncio.run(_go()) is None
    assert mgr.removed == []                             # the next load reconciles


# --- 2 + 4. a session that holds `mcp` follows the store; tenants stay apart --

@pytest.mark.asyncio
async def test_registrar_reconciles_and_filters_by_tenant():
    from tools.controller.mcp_registrar import MCPActionRegistrar
    mgr = _Mgr({"docs": _conn("search"),
                "user_u1::a": _conn("t1"),
                "user_u2::secret": _conn("leak"),
                "dead": _conn("x", status=ServerStatus.ERROR)})
    tool = _tool(mgr)
    tool.mcp_config = SimpleNamespace(servers={})
    ctrl = _controller("u1")
    reg = MCPActionRegistrar(ctrl)
    await reg.register(tool)
    assert _names(ctrl) == {"docs_search", "user_u1::a_t1"}
    mgr.connections.pop("user_u1::a")                    # /mcp remove
    mgr.connections["user_u1::b"] = _conn("t2")          # /mcp add
    await reg.register(tool)
    assert _names(ctrl) == {"docs_search", "user_u1::b_t2"}


@pytest.mark.asyncio
async def test_a_store_change_reaches_a_session_that_already_holds_mcp():
    rows = [SimpleNamespace(server_name="a")]

    class _Svc:
        async def get_user_servers(self, uid, enabled_only=True):
            return list(rows)

        async def get_server_config(self, uid, name):
            return SimpleNamespace()

    mgr = _Mgr()

    async def _add(name, config):
        mgr.connections[name] = _conn("t")
        return True
    mgr.add_server = _add
    tool = _tool(mgr, store=_Svc())
    tool.mcp_config = SimpleNamespace(servers={})
    tool._loop = asyncio.get_running_loop()
    ctrl = _controller("u1")
    tool.attach_controller(ctrl)

    await tool._on_user_server_change("u1", "a", "added")
    assert _names(ctrl) == {"user_u1::a_t"}
    rows.clear()
    await tool._on_user_server_change("u1", "a", "removed")
    assert _names(ctrl) == set()
    assert "user_u1::a" not in mgr.connections


@pytest.mark.asyncio
async def test_load_tool_on_a_loaded_mcp_resyncs_the_session():
    from tools.tool_disclosure import perform_load_tool
    seen = []

    class _Ctrl:
        container = None
        _is_sub_agent = False

        def list_tools(self):
            return ["mcp"]

        def get_tool(self, name):
            return "the-tool"

        async def sync_mcp_session(self, tool):
            seen.append(tool)
    res = await perform_load_tool(_Ctrl(), "mcp")
    assert "already loaded" in res.extracted_content
    assert seen == ["the-tool"]


def test_reachability_ignores_another_tenants_connection(monkeypatch):
    from tools.mcp.user_servers import has_reachable_server
    monkeypatch.setattr("tools.mcp.user_mcp_service._user_mcp_service", None)
    tool = _tool(_Mgr({"user_u2::x": _conn("t")}), store=SimpleNamespace(enabled_server_count=0))
    tool.mcp_config = SimpleNamespace(servers={})
    assert has_reachable_server(tool) is False
    tool.server_manager.connections["installed"] = _conn("t")
    assert has_reachable_server(tool) is True


@pytest.mark.asyncio
async def test_another_tenants_server_does_not_execute():
    tool = _tool(_Mgr({"user_u2::x": _conn("t")}))
    res = await tool._execute_validated("user_u2::x", "t", {"a": 1},
                                        execution_context=SimpleNamespace(user_id="u1"))
    assert res.success is False and "not one of yours" in res.error


# --- 5. one session's end does not unload another's servers ------------------

@pytest.mark.asyncio
async def test_session_end_unloads_only_when_no_other_session_holds_the_tool():
    mgr = _Mgr({"user_u1::a": _conn("t")})
    tool = _tool(mgr)
    s1, s2 = _controller("u1", "s1"), _controller("u1", "s2")
    tool.attach_controller(s1)
    tool.attach_controller(s2)
    assert await tool.release_session(s1, "u1") == 0
    assert mgr.removed == []
    assert await tool.release_session(s2, "u1") == 1
    assert [n for n, _ in mgr.removed] == ["user_u1::a"]


# --- 6. the real cause when a required service is missing --------------------

def test_a_missing_required_service_is_named_not_no_server(monkeypatch):
    import core.base_component as bc

    async def _no_container_check(self):
        return None
    monkeypatch.setattr(bc.BaseComponent, "_validate_dependencies", _no_container_check)
    tool = _tool(_Mgr())
    tool._services = {}
    asyncio.run(tool._validate_dependencies())   # BaseTool turns the tool off
    assert tool._enabled is False
    reason, remedy = tool.availability_gap()
    assert reason == "unavailable-on-this-deploy"
    assert "rate_limit_manager" in remedy


# --- 3. an explicit off wins --------------------------------------------------

def test_explicit_mcp_enabled_false_beats_the_operator_file(tmp_path, monkeypatch):
    from tools.mcp.config import mcp_gate_on
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path))
    (tmp_path / "mcp.json").write_text(json.dumps(
        {"servers": {"evidence": {"type": "streamable_http", "url": "https://e.example/mcp"}}}))
    monkeypatch.delenv("POLYROB_OVERRIDES", raising=False)
    monkeypatch.delenv("AUTONOMY_MODE", raising=False)
    assert mcp_gate_on() is True                          # unset: the file enables
    monkeypatch.setenv("POLYROB_OVERRIDES", "MCP_ENABLED=false")
    assert mcp_gate_on() is False

    from core.config import BotConfig
    cfg = BotConfig.model_construct(mcp=None, mcp_enabled=False, mcp_servers_config=None,
                                    base_dir=str(tmp_path))
    BotConfig._build_mcp_config_from_env(cfg)
    assert cfg.mcp is None


# --- 7. the migration and the reader agree on the key ------------------------

def test_migration_writes_into_the_key_that_holds_entries(tmp_path, monkeypatch):
    from tools.mcp.config import load_local_mcp_servers
    from tools.mcp.operator_home import migrate_repo_servers
    repo = tmp_path / "r.json"
    repo.write_text(json.dumps({"servers": {"b": {"type": "http", "url": "https://b.example"}}}))
    home = tmp_path / "mcp.json"
    home.write_text(json.dumps({"servers": {},
                                "mcpServers": {"mine": {"type": "http", "url": "https://m.example"}}}))
    assert migrate_repo_servers(repo, home) == ["b"]
    data = json.loads(home.read_text())
    assert set(data["mcpServers"]) == {"mine", "b"} and data["servers"] == {}
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path))
    assert set(load_local_mcp_servers()) == {"mine", "b"}
    # and a file split across both keys loads both
    home.write_text(json.dumps({"servers": {"x": {}}, "mcpServers": {"y": {}}}))
    assert set(load_local_mcp_servers()) == {"x", "y"}


# --- 8. id: null ------------------------------------------------------------------

def test_an_error_with_a_null_id_answers_the_pending_request():
    from tools.mcp.http_reply import pick_reply
    err = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
    assert pick_reply([err], 7) is err
    assert pick_reply([{"jsonrpc": "2.0", "id": None, "result": {}}], 7) is None


# --- 9. the API add ----------------------------------------------------------------

def test_api_add_defaults_to_auto_and_accepts_streamable_http():
    from api.mcp_models import AddServerRequest
    req = AddServerRequest(server_name="evidence", server_url="https://e.example/mcp")
    assert req.server_type == "auto"
    for kind in ("auto", "streamable_http", "http", "sse"):
        AddServerRequest(server_name="evidence", server_url="https://e.example", server_type=kind)
    with pytest.raises(Exception):
        AddServerRequest(server_name="evidence", server_url="https://e.example", server_type="stdio")


@pytest.mark.asyncio
async def test_auto_probes_even_without_verify_connection(monkeypatch):
    from tools.mcp import probe
    from tools.mcp.user_mcp_service import UserMCPService
    calls = []

    async def _probe(cfg, **kw):
        calls.append(cfg.url)
        return "http", SimpleNamespace(success=True, tools_discovered=2, error=None), []
    monkeypatch.setattr(probe, "probe_transports", _probe)
    stored = {}

    class _Db:
        async def get_user_settings(self, uid):
            return SimpleNamespace(max_servers=5)

        async def server_count(self, uid):
            return 0

        async def get_server(self, uid, name):
            return None

        async def add_server(self, **kw):
            stored.update(kw)
            return SimpleNamespace(server_name=kw["server_name"], server_type=kw["server_type"])

        async def update_connection_status(self, *a, **k):
            return None

    class _Ok:
        def validate(self, url):
            return True, None

        def validate_server_type(self, t):
            return True, None

    svc = UserMCPService(_Db(), url_validator=_Ok())
    res = await svc.add_server("u1", "evidence", "https://e.example/mcp",
                               server_type="auto", auth_method="none")
    assert res.success and calls == ["https://e.example/mcp"]
    assert stored["server_type"] == "http"


def test_listings_show_only_global_and_own_servers():
    from types import SimpleNamespace
    from tools.mcp.user_servers import user_prefix, visible_tools
    mine, theirs = user_prefix("alice") + "srv", user_prefix("bob") + "srv"
    manager = SimpleNamespace(get_all_tools=lambda: {"global": [1], mine: [2], theirs: [3]})
    assert set(visible_tools(manager, "alice")) == {"global", mine}
    assert set(visible_tools(manager, None)) == {"global"}
