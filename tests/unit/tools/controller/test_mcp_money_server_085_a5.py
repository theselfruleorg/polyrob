"""085 A5: an mcp.json server marked ``"money": true`` is a money server.

Its flat actions classify as money (``core.money.classify.money_action``), so
the principal, leaf and owner-approval gates apply. A server WITHOUT the key
stays exactly as it was: not money, no extra hook.
"""
import asyncio
import logging
import types

import pytest

import core.instance
from core.money import classify
from tools.controller.mcp_registrar import MCPActionRegistrar
from tools.mcp.config import MCPConfig, MCPServerConfig, resolve_config_environment_variables

OWNER = "owner-principal"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(classify, "_MONEY_MCP_ACTIONS", set())
    monkeypatch.setattr(core.instance, "resolve_owner_principal", lambda *a, **k: OWNER)


# --- config -------------------------------------------------------------------

def test_money_key_defaults_off_and_survives_env_resolution():
    plain = MCPServerConfig(type="http", url="https://mcp.example.test/rpc")
    assert plain.money is False
    marked = MCPServerConfig(type="http", url="https://mcp.example.test/rpc", money=True)
    assert marked.money is True
    cfg = resolve_config_environment_variables(
        MCPConfig(servers={"exch": marked, "docs": plain}))
    assert cfg.servers["exch"].money is True
    assert cfg.servers["docs"].money is False


# --- registrar ----------------------------------------------------------------

class _Registry:
    def __init__(self):
        self.actions = {}

    def wrap_function(self, name, function, description, tool, param_model):
        self.actions[name] = types.SimpleNamespace(function=function, tool=tool)


class _Controller:
    def __init__(self):
        self.registry = _Registry()
        self.logger = logging.getLogger("mcp-money-test")
        self.user_id = OWNER
        self.orchestrator = None
        self.container = None
        self.hooks = []

    def register_pre_tool_call_hook(self, hook, fail_mode="open"):
        self.hooks.append((hook, fail_mode))


class _MCPTool:
    def __init__(self, servers):
        self.mcp_config = MCPConfig(servers=servers)
        self.server_manager = types.SimpleNamespace(get_all_tools=lambda: {
            name: [types.SimpleNamespace(name="place_order", input_schema={},
                                         description="", annotations=None)]
            for name in servers})

    def is_money_server(self, name):
        from tools.mcp.mcp_tool import MCPTool
        return MCPTool.is_money_server(self, name)


def _servers(money):
    kw = {"money": True} if money else {}
    return {"exch": MCPServerConfig(type="http", url="https://mcp.example.test/a", **kw)}


class _Provider:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    async def request(self, action_name, params, context):
        self.calls.append(action_name)
        return self.answer


def _register(monkeypatch, money, answer=True):
    provider = _Provider(answer)
    import tools.controller.approval as approval
    monkeypatch.setattr(approval, "get_approval_provider_or_deny",
                        lambda *a, **k: provider)
    ctrl = _Controller()
    asyncio.run(MCPActionRegistrar(ctrl).register(_MCPTool(_servers(money))))
    return ctrl, provider


def _ctx(**kw):
    base = {"user_id": OWNER, "role": "main", "is_sub_agent": False, "metadata": {}}
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_server_without_the_key_is_unchanged(monkeypatch):
    ctrl, provider = _register(monkeypatch, money=False)
    assert "exch_place_order" in ctrl.registry.actions
    assert ctrl.hooks == []
    assert classify.money_action("exch_place_order") is False


def test_money_server_actions_classify_as_money(monkeypatch):
    ctrl, _ = _register(monkeypatch, money=True)
    assert classify.money_action("exch_place_order") is True
    assert len(ctrl.hooks) == 1 and ctrl.hooks[0][1] == "closed"


def test_money_gate_refuses_a_leaf_before_any_ask(monkeypatch):
    ctrl, provider = _register(monkeypatch, money=True)
    hook = ctrl.hooks[0][0]
    reason = asyncio.run(hook("exch_place_order", {}, _ctx(role="leaf", is_sub_agent=True)))
    assert reason and "sub-agent" in reason
    assert provider.calls == []


def test_money_gate_refuses_a_non_owner(monkeypatch):
    ctrl, provider = _register(monkeypatch, money=True)
    hook = ctrl.hooks[0][0]
    reason = asyncio.run(hook("exch_place_order", {}, _ctx(user_id="usr_tenant")))
    assert reason
    assert provider.calls == []


def test_money_gate_asks_the_owner(monkeypatch):
    ctrl, provider = _register(monkeypatch, money=True, answer=True)
    hook = ctrl.hooks[0][0]
    assert asyncio.run(hook("exch_place_order", {}, _ctx())) is None
    assert provider.calls == ["exch_place_order"]

    ctrl, provider = _register(monkeypatch, money=True, answer=False)
    hook = ctrl.hooks[0][0]
    assert "approval denied" in asyncio.run(hook("exch_place_order", {}, _ctx()))


def test_money_gate_ignores_other_actions(monkeypatch):
    ctrl, provider = _register(monkeypatch, money=True)
    assert asyncio.run(ctrl.hooks[0][0]("filesystem_read_file", {}, _ctx())) is None
    assert provider.calls == []


# --- the generic wrapper cannot carry a money server past the gates -------------

def _bare_tool(money):
    from tools.mcp.mcp_tool import MCPTool
    tool = object.__new__(MCPTool)
    tool.mcp_config = MCPConfig(servers=_servers(money))
    tool._enabled = True
    tool._initialized = True
    tool.ran = []

    async def _exec(server, name, args, *, execution_context=None):
        tool.ran.append((server, name))
        return "ran"
    tool._execute_validated = _exec
    return tool


def test_wrapper_refuses_a_money_server():
    from tools.mcp.views import MCPExecuteToolAction
    tool = _bare_tool(money=True)
    out = asyncio.run(tool.execute_tool(
        MCPExecuteToolAction(server_name="exch", tool_name="place_order", arguments={})))
    assert out.success is False and "exch_place_order" in out.error
    assert tool.ran == []


def test_wrapper_runs_a_plain_server_as_today():
    from tools.mcp.views import MCPExecuteToolAction
    tool = _bare_tool(money=False)
    out = asyncio.run(tool.execute_tool(
        MCPExecuteToolAction(server_name="exch", tool_name="place_order", arguments={})))
    assert out == "ran"
    assert tool.ran == [("exch", "place_order")]


# --- the flat-name parse cannot route an ungated name to a money server ----------

def _routing_tool():
    """Two servers whose flat names collide: `a` (money, tool `b_c`) and `a_b`
    (plain, tool `c`) — both flatten to `a_b_c`."""
    from tools.mcp.mcp_tool import MCPTool
    tool = object.__new__(MCPTool)
    tool.logger = logging.getLogger("mcp-route-test")
    tool.mcp_config = MCPConfig(servers={
        "a": MCPServerConfig(type="http", url="https://mcp.example.test/a", money=True),
        "a_b": MCPServerConfig(type="http", url="https://mcp.example.test/b")})
    tool.server_manager = types.SimpleNamespace(get_all_tools=lambda: {"a": [], "a_b": []})
    tool.ran = []

    async def _exec(server, name, args, *, execution_context=None):
        tool.ran.append((server, name))
        return types.SimpleNamespace(success=True, result="ran", error=None)
    tool._execute_validated = _exec
    return tool


def test_an_ungated_name_that_parses_to_a_money_server_refuses():
    from core.exceptions import ToolError
    tool = _routing_tool()
    with pytest.raises(ToolError, match="money server"):
        asyncio.run(tool.execute_mcp_tool("a_b_c", {}))
    assert tool.ran == []


def test_a_gated_name_on_a_money_server_runs():
    tool = _routing_tool()
    classify.register_money_actions(["a_b_c"])
    assert asyncio.run(tool.execute_mcp_tool("a_b_c", {})) == "ran"
    assert tool.ran == [("a", "b_c")]


def test_reregistering_after_an_install_adds_no_second_ask(monkeypatch):
    """mcp_install re-runs the registrar on the same Controller: the existing
    money actions are skipped, so one call still asks the owner once."""
    provider = _Provider(True)
    import tools.controller.approval as approval
    monkeypatch.setattr(approval, "get_approval_provider_or_deny", lambda *a, **k: provider)
    ctrl = _Controller()
    reg = MCPActionRegistrar(ctrl)
    mcp = _MCPTool(_servers(True))
    asyncio.run(reg.register(mcp))
    asyncio.run(reg.register(mcp))
    for hook, _mode in ctrl.hooks:
        assert asyncio.run(hook("exch_place_order", {}, _ctx())) is None
    assert provider.calls == ["exch_place_order"]


# --- the owner's pause stops a money MCP action (the effect gate) ---------------

class _EffectController:
    _is_sub_agent = False
    session_id = "s1"

    def __init__(self, names):
        self._acts = {n: types.SimpleNamespace(tool="mcp", function=None) for n in names}

    def get_action_details(self, name):
        return self._acts.get(name)


@pytest.fixture
def _pause_home(tmp_path, monkeypatch):
    import core.autonomy_control as ac
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(ac, "state_bases", lambda d=None: [str(home)])
    monkeypatch.setattr("tools.controller.turn_origin._is_forged_or_autonomous_turn",
                        lambda ctx, c: True)  # an autonomous run (goal / job)
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "false")
    return lambda: ac.pause(str(home), scopes=("all",), set_by="test", via="unit")


def test_a_money_mcp_action_is_a_money_effect_the_pause_stops(monkeypatch, _pause_home):
    from tools.controller.effect_hooks import make_effect_gate_hook
    ctrl, provider = _register(monkeypatch, money=True)
    gate = make_effect_gate_hook(_EffectController(["exch_place_order"]))
    # unpaused: the effect gate passes, the owner is asked as today
    assert asyncio.run(gate("exch_place_order", {}, _ctx())) is None
    assert asyncio.run(ctrl.hooks[0][0]("exch_place_order", {}, _ctx())) is None
    assert provider.calls == ["exch_place_order"]
    _pause_home()
    reason = asyncio.run(gate("exch_place_order", {}, _ctx()))
    assert reason and "money" in reason and "paused" in reason


def test_a_plain_mcp_action_is_unchanged_under_the_pause(monkeypatch, _pause_home):
    from tools.controller.effect_hooks import _classify, make_effect_gate_hook
    _register(monkeypatch, money=False)
    ctrl = _EffectController(["exch_place_order"])
    assert _classify(ctrl, "exch_place_order")[1].effect == "network"
    _pause_home()
    assert asyncio.run(make_effect_gate_hook(ctrl)("exch_place_order", {}, _ctx())) is None
