"""098 TS-04: ONE money-MCP gate per controller.

A money server that leaves and re-enters the tool list re-registers its actions,
but the controller keeps one gate (an OnceLatch), and one call files one owner
ask. New money actions extend the existing gate; a failed approval wiring is
retried on the next sync while the gate refuses meanwhile."""
import asyncio
import logging
import types

import pytest

import core.instance
from core.money import classify
from tools.controller.mcp_registrar import MCPActionRegistrar
from tools.mcp.config import MCPConfig, MCPServerConfig

OWNER = "owner-principal"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(classify, "_MONEY_MCP_ACTIONS", set())
    monkeypatch.setattr(core.instance, "resolve_owner_principal", lambda *a, **k: OWNER)


class _Registry:
    def __init__(self):
        self.actions = {}

    def wrap_function(self, name, function, description, tool, param_model):
        self.actions[name] = types.SimpleNamespace(function=function, tool=tool)

    def remove_action(self, name):
        self.actions.pop(name, None)


class _Controller:
    def __init__(self):
        self.registry = _Registry()
        self.logger = logging.getLogger("money-gate-single")
        self.user_id = OWNER
        self.orchestrator = None
        self.container = None
        self.hooks = []

    def register_pre_tool_call_hook(self, hook, fail_mode="open"):
        self.hooks.append((hook, fail_mode))


class _MCPTool:
    def __init__(self, tools_by_server):
        self.tools_by_server = tools_by_server
        self.mcp_config = MCPConfig(servers={
            n: MCPServerConfig(type="http", url=f"https://{n}.test", money=True)
            for n in ("pay", "exch")})
        self.server_manager = types.SimpleNamespace(get_all_tools=lambda: {
            srv: [types.SimpleNamespace(name=t, input_schema={}, description="",
                                        annotations=None) for t in tools]
            for srv, tools in self.tools_by_server.items()})

    def is_money_server(self, name):
        return True


class _Provider:
    def __init__(self):
        self.calls = []

    async def request(self, action_name, params, context):
        self.calls.append(action_name)
        return True


def _ctx():
    return types.SimpleNamespace(user_id=OWNER, role="main", is_sub_agent=False, metadata={})


def _wire(monkeypatch, provider):
    import tools.controller.approval as approval
    monkeypatch.setattr(approval, "get_approval_provider_or_deny", lambda *a, **k: provider)


async def _run(ctrl, name):
    for hook, _ in ctrl.hooks:
        reason = await hook(name, {}, _ctx())
        if reason:
            return reason
    return None


def test_resync_after_server_flap_installs_one_gate(monkeypatch):
    _wire(monkeypatch, _Provider())
    ctrl = _Controller()
    reg = MCPActionRegistrar(ctrl)
    mcp = _MCPTool({"pay": ["send"]})
    asyncio.run(reg.register(mcp))
    mcp.tools_by_server = {}  # the server drops: its actions are unregistered
    asyncio.run(reg.register(mcp))
    assert "pay_send" not in ctrl.registry.actions
    mcp.tools_by_server = {"pay": ["send"]}  # it reconnects
    asyncio.run(reg.register(mcp))
    assert "pay_send" in ctrl.registry.actions
    assert len(ctrl.hooks) == 1


def test_new_money_tools_extend_existing_gate(monkeypatch):
    provider = _Provider()
    _wire(monkeypatch, provider)
    ctrl = _Controller()
    reg = MCPActionRegistrar(ctrl)
    mcp = _MCPTool({"pay": ["send"]})
    asyncio.run(reg.register(mcp))
    mcp.tools_by_server = {"pay": ["send"], "exch": ["place_order"]}
    asyncio.run(reg.register(mcp))
    assert len(ctrl.hooks) == 1
    assert asyncio.run(_run(ctrl, "exch_place_order")) is None
    assert provider.calls == ["exch_place_order"]


def test_one_call_files_one_owner_ask(monkeypatch):
    provider = _Provider()
    _wire(monkeypatch, provider)
    ctrl = _Controller()
    reg = MCPActionRegistrar(ctrl)
    mcp = _MCPTool({"pay": ["send"]})
    asyncio.run(reg.register(mcp))
    mcp.tools_by_server = {}
    asyncio.run(reg.register(mcp))
    mcp.tools_by_server = {"pay": ["send"]}
    asyncio.run(reg.register(mcp))
    assert asyncio.run(_run(ctrl, "pay_send")) is None
    assert provider.calls == ["pay_send"]


def test_wiring_failure_retries_next_sync(monkeypatch):
    import tools.controller.approval as approval
    provider = _Provider()
    state = {"fail": True}

    def _get(*a, **k):
        if state["fail"]:
            raise RuntimeError("queue down")
        return provider

    monkeypatch.setattr(approval, "get_approval_provider_or_deny", _get)
    ctrl = _Controller()
    reg = MCPActionRegistrar(ctrl)
    mcp = _MCPTool({"pay": ["send"]})
    asyncio.run(reg.register(mcp))
    assert len(ctrl.hooks) == 1
    reason = asyncio.run(_run(ctrl, "pay_send"))
    assert reason and "could not be wired" in reason
    assert provider.calls == []
    # The next sync (a new money tool appears) retries the wiring.
    state["fail"] = False
    mcp.tools_by_server = {"pay": ["send", "refund"]}
    asyncio.run(reg.register(mcp))
    assert len(ctrl.hooks) == 1
    assert asyncio.run(_run(ctrl, "pay_send")) is None
    assert provider.calls == ["pay_send"]
