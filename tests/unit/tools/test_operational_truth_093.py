"""093 step 1 — the agent gets the TRUE cause, and the retry wall tells the truth.

Prod 2026-10-28 (the 402Mob incident): MCP_ENABLED was on but no server was
configured; the catalog said `mcp` was loadable, load_tool said "failed to load
from the container — see session logs" (the agent cannot read logs), and the
agent told the owner "MCP is already on". The retry wall said "try a different
approach", survived 5 owner turns and was hit ~35 times; the agent told the
owner the signer was "hard-blocked".
"""
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import agents.task.agent.service  # noqa: F401 — avoid controller<->orchestrator import cycle
from agents.task.agent.views import ActionResult
from core.tool_gates import REMEDIES, gate_for, remedy_text
from tools.controller.service import Controller
from tools.tool_disclosure import perform_load_tool, resolve_tool_status


class _Container:
    def __init__(self, services):
        self._services = dict(services)

    def has_service(self, name):
        return name in self._services

    def get_service(self, name):
        return self._services.get(name)


def _mcp_tool(enabled: bool):
    from tools.mcp.mcp_tool import MCPTool
    tool = object.__new__(MCPTool)
    tool._enabled = enabled
    return tool


# --- A1: one honest status --------------------------------------------------

def test_mcp_gate_lives_next_to_its_tool():
    import tools  # noqa: F401 — the tool tier registers its gates at import
    assert gate_for("mcp") is not None


def test_mcp_with_zero_servers_is_gated_not_loadable():
    st = resolve_tool_status("mcp", container=_Container({"mcp": _mcp_tool(False)}),
                             loaded_ids=set())
    assert st.status == "gated"
    assert st.reason == "no-mcp-server"
    assert st.remedy == remedy_text("no-mcp-server")


def test_mcp_with_a_server_stays_loadable():
    st = resolve_tool_status("mcp", container=_Container({"mcp": _mcp_tool(True)}),
                             loaded_ids=set())
    assert st.status == "loadable"


def test_a_raising_availability_probe_is_no_gap():
    class Boom:
        def availability_gap(self):
            raise RuntimeError("x")
    st = resolve_tool_status("web_fetch", container=_Container({"web_fetch": Boom()}),
                             loaded_ids=set())
    assert st.status == "loadable"


# --- A2: load_tool carries the recorded reason ------------------------------

def _controller(failure: str):
    c = MagicMock()
    c.container = _Container({"web_fetch": object()})
    c.list_tools.return_value = []
    c.load_tools_from_container = AsyncMock(return_value={})
    c.get_tool_load_failures.return_value = {"web_fetch": failure} if failure else {}
    c._is_sub_agent = False
    c.session_id = None
    return c


@pytest.mark.parametrize("key", sorted(REMEDIES))
def test_load_tool_refusal_carries_the_gate_remedy(monkeypatch, key):
    """Ratchet: whatever gate the load path records, the agent reads its remedy."""
    import tools.tool_disclosure as td
    monkeypatch.setattr(td, "disclosure_ceiling", lambda c: None)
    text = remedy_text(key, tool="web_fetch", flag="SOME_FLAG")
    res = asyncio.run(perform_load_tool(_controller(f"gated:{key} — {text}"), "web_fetch"))
    assert res.error is None
    assert text in res.extracted_content
    assert "session logs" not in res.extracted_content


def test_load_tool_exception_names_the_class(monkeypatch):
    import tools.tool_disclosure as td
    monkeypatch.setattr(td, "disclosure_ceiling", lambda c: None)
    res = asyncio.run(perform_load_tool(
        _controller("gated:load-failed — ValueError: bad url"), "web_fetch"))
    assert "ValueError: bad url" in res.error
    assert "session logs" not in res.error


def test_load_tool_without_a_recorded_reason_says_so(monkeypatch):
    import tools.tool_disclosure as td
    monkeypatch.setattr(td, "disclosure_ceiling", lambda c: None)
    res = asyncio.run(perform_load_tool(_controller(""), "web_fetch"))
    assert "no reason was recorded" in res.error


# --- E1/E2: the retry wall --------------------------------------------------

def _host():
    c = object.__new__(Controller)
    c.logger = logging.getLogger("wall-093")
    c._operation_attempts = {}
    c.session_id = "s"
    c.registry = MagicMock()
    c.registry.get_action.return_value = SimpleNamespace(tool="mytool")
    c.registry.execute_action = AsyncMock(return_value=ActionResult(
        error="submit_url is required when submit_body_json is set"))
    c._get_retry_limit_for_tool = lambda tool: 2
    return c


def _action():
    a = MagicMock()
    a.model_dump.return_value = {"wallet_sign_challenge": {"url": "https://example.test"}}
    return a


def test_wall_names_the_last_error_and_says_the_tool_is_fine():
    c = _host()
    for _ in range(2):
        asyncio.run(c.act(_action(), execution_context=MagicMock()))
    wall = asyncio.run(c.act(_action(), execution_context=MagicMock()))
    assert wall.metadata == {"retry_wall": True}
    assert wall.error.startswith("⚠️ Maximum retries")   # conversational_exit prefix
    assert "submit_url is required" in wall.error
    assert "not broken" in wall.error
    assert "try a different approach" not in wall.error.lower()


def test_wall_resets_on_a_new_owner_turn():
    c = _host()
    for _ in range(3):
        asyncio.run(c.act(_action(), execution_context=MagicMock()))
    assert c.registry.execute_action.await_count == 2
    c.reset_retry_wall()
    asyncio.run(c.act(_action(), execution_context=MagicMock()))
    assert c.registry.execute_action.await_count == 3


def _ingress(messages, forged=None):
    from agents.task.agent.core.user_ingress import UserIngressMixin
    agent = object.__new__(UserIngressMixin)
    agent.hitl_manager = SimpleNamespace(drain_user_messages=AsyncMock(return_value=messages))
    agent.orchestrator = SimpleNamespace(_forged_turn_kind=forged)
    agent.controller = MagicMock()
    agent.state = SimpleNamespace(n_steps=0)
    agent.logger = logging.getLogger("ingress-093")
    agent.workspace_context = MagicMock()
    agent.session_id, agent.user_id = "s", "u"
    agent._retry_wall_steps = 1
    return agent


@pytest.mark.parametrize("messages,forged,reset", [
    ([{"kind": "comment", "text": "we updated the tool, try again"}], None, True),
    ([{"kind": "continuation", "text": "try again"}], None, True),
    ([{"kind": "comment", "text": "a forged turn"}], "cron", False),
    ([], None, False),
    # 093 review: a room admin's steer and a non-owner kind are not the owner.
    ([{"kind": "comment", "text": "admin steer", "metadata": {"room_role": "admin"}}],
     None, False),
    ([{"kind": "system", "text": "continue"}], None, False),
])
def test_only_a_genuine_owner_batch_resets_the_wall(monkeypatch, messages, forged, reset):
    import agents.task.agent.core.user_ingress as ui
    monkeypatch.setattr(ui, "_update_forged_turn_marker", lambda *a: None)
    agent = _ingress(messages, forged)
    asyncio.run(agent._drain_user_messages())
    assert agent.controller.reset_retry_wall.called is reset
    assert agent._retry_wall_steps == (0 if reset else 1)


def test_a_loaded_tool_that_cannot_run_is_gated_not_loaded():
    """Same rule as the browser rail: a loaded tool that refuses every call is gated."""
    st = resolve_tool_status("mcp", container=_Container({"mcp": _mcp_tool(False)}),
                             loaded_ids={"mcp"})
    assert st.status == "gated" and st.reason == "no-mcp-server"
