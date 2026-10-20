"""085 A6: a REST/A2A tenant never carries a money tool in its own toolset.

The wallet-authority hook refuses every money action for a non-owner session
user, so a money tool on a tenant session is only noise. The owner's list and
every non-money tool pass unchanged.
"""
import asyncio
from pathlib import Path

import pytest

import core.instance
from api.tenant_tools import tenant_toolset

OWNER = "owner-principal"


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setattr(core.instance, "resolve_owner_principal", lambda *a, **k: OWNER)


def test_tenant_loses_only_money_tools():
    asked = ["browser", "defi_trade", "filesystem", "x402_pay", "mcp", "shell"]
    assert tenant_toolset(asked, "usr_tenant") == ["browser", "filesystem", "mcp", "shell"]


def test_owner_keeps_the_list_as_given():
    asked = ["browser", "defi_trade", "x402_pay"]
    assert tenant_toolset(asked, OWNER) == asked


def test_non_money_list_is_unchanged_for_a_tenant():
    assert tenant_toolset(["browser", "filesystem"], "usr_tenant") == ["browser", "filesystem"]
    assert tenant_toolset(None, "usr_tenant") is None


class _Stop(Exception):
    pass


class _Agent:
    def __init__(self):
        self.request = None

    async def create_session(self, user_id, request, session_id=None, creator=None):
        self.request = request
        raise _Stop()


class _Container:
    def __init__(self, agent):
        self._agent = agent

    def get_agent(self, name):
        return self._agent

    def get_service(self, name):
        return None


def _a2a_create(user_id, tools):
    from api.a2a.models import A2AMessage, SendMessageRequest
    from api.a2a.task_handler import A2ATaskHandler
    agent = _Agent()
    handler = A2ATaskHandler(_Container(agent))
    msg = A2AMessage(role="user", parts=[{"kind": "text", "text": "hi"}],
                     metadata={"tools": tools})
    with pytest.raises(_Stop):
        asyncio.run(handler.create_task(SendMessageRequest(message=msg), user_id=user_id))
    return agent.request.tools


def test_a2a_tenant_toolset_drops_money_tools():
    assert _a2a_create("usr_tenant", ["browser", "defi_trade"]) == ["browser"]


def test_a2a_owner_toolset_is_unchanged():
    assert _a2a_create(OWNER, ["browser", "defi_trade"]) == ["browser", "defi_trade"]


def test_rest_create_session_filters_the_tenant_toolset():
    src = (Path(__file__).resolve().parents[3] / "api" / "task_http_api.py").read_text()
    assert "session_config.tools = tenant_toolset(request_body.get(\"tools\"" in src
