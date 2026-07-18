"""Unit tests for the SessionOrchestrator concern mixins (PR8).

These exercise the moved logic in isolation via a tiny host class that
composes the mixins and supplies the instance attributes they read — no
container, browser, or LLM needed.
"""

import logging

import pytest

from agents.task.session.workspace import WorkspaceMixin
from agents.task.session.browser_pool import BrowserPoolMixin


class _Host(WorkspaceMixin, BrowserPoolMixin):
    """Minimal stand-in exposing just the attributes the mixins touch."""

    def __init__(self, workspace_dir=None, controller=None, browser_manager=None):
        self.logger = logging.getLogger("test_host")
        self._workspace_dir = workspace_dir
        self.controller = controller
        self.browser_manager = browser_manager


# --- WorkspaceMixin ---


def test_workspace_dir_property_returns_value():
    host = _Host(workspace_dir="/tmp/ws")
    assert host.workspace_dir == "/tmp/ws"


def test_workspace_dir_property_none_when_unset():
    host = _Host()
    host._workspace_dir = None
    assert host.workspace_dir is None


# --- BrowserPoolMixin ---


class _FakeController:
    def __init__(self, tools):
        self._tools = tools

    def list_tools(self):
        return list(self._tools.keys())

    def get_tool(self, name):
        return self._tools.get(name)


def test_tools_property_empty_without_controller():
    host = _Host(controller=None)
    assert host.tools == {}


def test_tools_property_reads_from_controller():
    ctrl = _FakeController({"browser": object(), "filesystem": object()})
    host = _Host(controller=ctrl)
    tools = host.tools
    assert set(tools.keys()) == {"browser", "filesystem"}
    assert tools["browser"] is ctrl._tools["browser"]


@pytest.mark.asyncio
async def test_get_browser_context_none_without_manager():
    host = _Host(browser_manager=None)
    assert await host.get_browser_context("agent1") is None
