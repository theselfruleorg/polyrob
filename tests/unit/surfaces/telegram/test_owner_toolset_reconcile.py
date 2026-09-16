"""An owner grant must reach the chat the owner is already sitting in.

Live, 2026-09-12, verified from the prod journal. The owner armed
`DEFI_AGENT_AUTONOMY=true` at 09:27, the service restarted at 10:57, and
`owner_interactive_tool_ids()` correctly returned a list containing `defi_trade`.
His Telegram session still could not bridge, because a session freezes its
toolset at CREATION and his chat predates the grant. Session 8e8c1d92 had to
`load_tool("defi_data")` at 11:06 to read a balance — proof it started without
the rail — and at 11:13 `load_tool("defi_trade")` was refused, because a money
tool is never self-loadable.

So an existing owner chat could NEVER pick up a money grant, and the only way
out was `/new`, which nothing told him. The agent instead reported a "hard
architectural gate", and sent him to type `/bridge` himself — which would have
failed too.

This is NOT the agent self-granting. The list comes from the operator's own
config (`interactive_tool_ids()` — `INTERACTIVE_TOOL_IDS`, `full_autonomy_enabled`,
`DEFI_AGENT_AUTONOMY`); the agent cannot influence it. All the reconcile does is
apply a grant the owner already made to the session he already has.
"""
import asyncio
from types import SimpleNamespace

import pytest


class _Controller:
    def __init__(self, loaded):
        self._loaded = list(loaded)
        self.load_calls = []

    def list_tools(self):
        return list(self._loaded)

    async def load_tools_from_container(self, ids):
        self.load_calls.append(list(ids))
        self._loaded.extend(ids)
        return {i: object() for i in ids}


class _TaskAgent:
    def __init__(self, controller):
        self._orch = SimpleNamespace(controller=controller)

    def get_orchestrator(self, session_id):
        return self._orch


def _reconcile(task_agent, user_id="rob", session_id="s1"):
    from surfaces.telegram.interactive_tools import reconcile_owner_toolset
    return asyncio.run(reconcile_owner_toolset(task_agent, user_id, session_id))


@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setattr(
        "surfaces.telegram.interactive_tools.owner_interactive_tool_ids",
        lambda uid, env=None: (["filesystem", "task", "defi_data", "defi_trade"]
                               if uid == "rob" else None))


def test_a_money_tool_the_owner_granted_reaches_his_existing_session(owner):
    c = _Controller(["filesystem", "task"])
    added = _reconcile(_TaskAgent(c))

    assert c.load_calls == [["defi_data", "defi_trade"]]
    assert "defi_trade" in added
    assert "defi_trade" in c.list_tools()


def test_an_already_complete_session_loads_nothing(owner):
    c = _Controller(["filesystem", "task", "defi_data", "defi_trade"])
    assert _reconcile(_TaskAgent(c)) == []
    assert c.load_calls == []


def test_a_non_owner_session_is_never_touched(owner):
    """`owner_interactive_tool_ids` returns None for a stranger — tenant
    least-privilege is unchanged, and no tool is ever added to their session."""
    c = _Controller(["filesystem", "task"])
    assert _reconcile(_TaskAgent(c), user_id="u_stranger") == []
    assert c.load_calls == []


def test_a_missing_orchestrator_is_a_noop(owner):
    class _NoOrch:
        def get_orchestrator(self, session_id):
            return None
    assert _reconcile(_NoOrch()) == []


def test_a_failing_load_never_breaks_the_turn(owner):
    class _Broken(_Controller):
        async def load_tools_from_container(self, ids):
            raise RuntimeError("container is unhappy")
    assert _reconcile(_TaskAgent(_Broken(["filesystem"]))) == []


def test_the_grant_list_comes_from_config_not_from_the_agent(monkeypatch):
    """The reconcile must read the operator's configured toolset, so an agent
    that asks for a money tool cannot widen its own grant."""
    seen = {}

    def _fake(uid, env=None):
        seen["called_with"] = uid
        return ["filesystem", "task"]

    monkeypatch.setattr(
        "surfaces.telegram.interactive_tools.owner_interactive_tool_ids", _fake)
    c = _Controller(["filesystem", "task", "defi_trade"])
    _reconcile(_TaskAgent(c))
    assert seen["called_with"] == "rob"
    # A tool NOT in the configured grant is never added — and never removed either.
    assert c.load_calls == []
