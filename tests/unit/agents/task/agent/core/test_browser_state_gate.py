"""102 N-4: no state read, and no browser, until the session has used a page."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from agents.task.agent.core.resources import ResourceMixin


class _Host(ResourceMixin):
    def __init__(self, used=False, tools=None):
        self._used = used
        self.get_browser_context = AsyncMock(return_value="ctx")
        tools = tools or {}
        self.controller = SimpleNamespace(registry=SimpleNamespace(
            get_action=lambda name: SimpleNamespace(tool=tools[name]) if name in tools else None))

    def _has_active_browser_usage(self):
        return self._used


class _Action(BaseModel):
    go_to_url: dict | None = None
    tool_call: dict | None = None
    write_file: dict | None = None


@pytest.mark.asyncio
async def test_a_session_with_no_page_opens_no_browser():
    host = _Host()
    assert await host.get_used_browser_context() is None
    host.get_browser_context.assert_not_awaited()


@pytest.mark.asyncio
async def test_use_is_sticky():
    host = _Host(used=True)
    assert await host.get_used_browser_context() == "ctx"
    host._used = False  # the action window moved on, the page is still open
    assert await host.get_used_browser_context() == "ctx"


def test_a_browser_action_asks_for_the_context_directly_and_through_the_bridge():
    host = _Host(tools={"go_to_url": "browser", "write_file": "files"})
    assert host.actions_need_browser([_Action(go_to_url={"url": "x"})])
    assert host.actions_need_browser([_Action(tool_call={"name": "go_to_url", "arguments": {}})])
    assert not host.actions_need_browser([_Action(write_file={"path": "x"})])
    assert not host.actions_need_browser([])
