"""UP-05 — sub-agent least-privilege toolset.

Covers the pure narrowing/blocklist helpers, the Registry exclude_actions lever
(the real mechanism that suppresses delegation actions — they are NOT tool_ids),
the AgentDeps.controller injection wiring, and SubAgentManager._build_child_controller.
"""
import logging
import types

import pytest

import agents.task.agent.service  # noqa: F401 — avoid controller<->orchestrator import cycle
from tools.controller.delegation import (
    DELEGATE_BLOCKED_TOOLS,
    DELEGATION_ACTION_NAMES,
    LEAF,
    ORCHESTRATOR,
    delegation_exclusions_for_child,
    get_blocked_child_tools,
    narrow_child_tools,
)
from tools.controller.registry.service import Registry


# --- pure: blocklist + env override -----------------------------------------

def test_default_blocklist_is_code_exec_and_cronjob():
    # code_execution + cronjob are always blocked; crypto tools (x402_pay/hyperliquid/
    # polymarket) were added later for least-privilege — assert both invariants hold.
    assert {"code_execution", "cronjob"} <= DELEGATE_BLOCKED_TOOLS
    assert {"x402_pay", "hyperliquid", "polymarket"} <= DELEGATE_BLOCKED_TOOLS
    # "task" (the TODO tool) must NOT be blocked — it is not a delegation tool.
    assert "task" not in DELEGATE_BLOCKED_TOOLS


def test_blocklist_env_unset_keeps_default(monkeypatch):
    monkeypatch.delenv("DELEGATE_BLOCKED_TOOLS", raising=False)
    assert get_blocked_child_tools() == DELEGATE_BLOCKED_TOOLS


def test_blocklist_env_empty_keeps_default(monkeypatch):
    monkeypatch.setenv("DELEGATE_BLOCKED_TOOLS", "")
    assert get_blocked_child_tools() == DELEGATE_BLOCKED_TOOLS


def test_blocklist_env_replaces(monkeypatch):
    monkeypatch.setenv("DELEGATE_BLOCKED_TOOLS", " foo , bar ,")
    assert get_blocked_child_tools() == frozenset({"foo", "bar"})


# --- pure: narrow_child_tools ------------------------------------------------

def test_narrow_inherit_drops_blocked_keeps_rest():
    out = narrow_child_tools(
        parent_tools=["filesystem", "task", "code_execution", "cronjob", "browser"],
        requested_tools=None,
        child_role=LEAF,
    )
    assert out == ["filesystem", "task", "browser"]  # code_execution/cronjob dropped


def test_narrow_requested_is_intersected_with_parent():
    out = narrow_child_tools(
        parent_tools=["filesystem", "task", "browser"],
        requested_tools=["filesystem", "mcp", "evil"],  # mcp/evil not in parent
        child_role=LEAF,
    )
    assert out == ["filesystem"]


def test_narrow_blocklist_wins_over_request():
    out = narrow_child_tools(
        parent_tools=["filesystem", "code_execution"],
        requested_tools=["code_execution"],
        child_role=LEAF,
    )
    assert out == []  # explicit request can't re-add a blocked tool


# --- pure: delegation exclusions --------------------------------------------

def test_leaf_excludes_delegation_actions():
    assert delegation_exclusions_for_child(LEAF) == DELEGATION_ACTION_NAMES
    assert delegation_exclusions_for_child(LEAF) == frozenset(
        {"subtask", "parallel_subtasks", "delegate_task"}
    )


def test_orchestrator_keeps_delegation_actions():
    assert delegation_exclusions_for_child(ORCHESTRATOR) == frozenset()


# --- mechanism: Registry honours exclude_actions for delegation names --------

def test_registry_exclude_actions_skips_delegation_registration():
    """The real lever: delegation actions are registered via @registry.action and
    must be skippable by name (they are NOT container tool_ids)."""
    reg = Registry(exclude_actions=["subtask", "parallel_subtasks", "delegate_task"])

    @reg.action("delegate a subtask")
    def subtask(params=None, execution_context=None):  # noqa: ARG001
        return "ran"

    @reg.action("a normal action")
    def read_file(params=None, execution_context=None):  # noqa: ARG001
        return "ok"

    names = reg.list_action_names()
    assert "subtask" not in names      # excluded by name
    assert "read_file" in names        # unrelated action still registered


# --- wiring: AgentDeps.controller injection ---------------------------------

def test_agentdeps_carries_controller_and_routes_via_from_params():
    from agents.task.agent.service import AgentDeps, _AGENT_DEP_KEYS
    assert "controller" in _AGENT_DEP_KEYS
    sentinel = object()
    deps = AgentDeps(llm=object(), orchestrator=object(), controller=sentinel)
    assert deps.controller is sentinel
    # default is None (legacy: fall back to orchestrator.controller)
    assert AgentDeps(llm=object(), orchestrator=object()).controller is None


# --- SubAgentManager._build_child_controller --------------------------------

def _bare_manager(monkeypatch, *, flag, parent_tools=None):
    from agents.task.agent.sub_agent_manager import SubAgentManager
    from agents.task.constants import TimeoutConfig
    monkeypatch.setattr(TimeoutConfig, "get_subagent_least_privilege", classmethod(lambda cls: flag))
    m = object.__new__(SubAgentManager)
    m.logger = logging.getLogger("subagent-lp-test")
    parent_ctl = types.SimpleNamespace(list_tools=lambda: parent_tools or ["filesystem", "code_execution", "cronjob", "browser"])
    m.orchestrator = types.SimpleNamespace(controller=parent_ctl, container=object())
    return m


@pytest.mark.asyncio
async def test_build_child_controller_returns_none_when_flag_off(monkeypatch):
    m = _bare_manager(monkeypatch, flag=False)
    assert await m._build_child_controller() is None  # legacy: shared parent controller


@pytest.mark.asyncio
async def test_build_child_controller_narrows_and_excludes(monkeypatch):
    """Flag ON: child Controller built with narrowed tool_ids + delegation exclusions."""
    m = _bare_manager(monkeypatch, flag=True,
                       parent_tools=["filesystem", "code_execution", "cronjob", "browser"])

    captured = {}

    class _FakeController:
        def __init__(self, *, exclude_actions=None, container=None, orchestrator=None):
            captured["exclude_actions"] = exclude_actions
            self._loaded = None

        async def load_tools_from_container(self, tool_ids):
            captured["tool_ids"] = tool_ids
            self._loaded = tool_ids

        def list_tools(self):
            return self._loaded or []

    import tools.controller.service as svc
    monkeypatch.setattr(svc, "Controller", _FakeController)

    child = await m._build_child_controller()
    assert child is not None
    # code_execution + cronjob stripped from tool_ids; browser/filesystem kept
    assert set(captured["tool_ids"]) == {"filesystem", "browser"}
    # delegation actions excluded for the leaf child
    assert set(captured["exclude_actions"]) == {"subtask", "parallel_subtasks", "delegate_task"}
