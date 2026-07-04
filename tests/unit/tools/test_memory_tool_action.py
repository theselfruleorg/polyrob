"""UP-09 Step 9.4 — bounded `memory` tool (read/add/remove), flag-gated + tenant-scoped."""
import logging

import agents.task.agent.service  # noqa: F401 — avoid import cycle
import pytest

from modules.memory.sqlite_memory_provider import SqliteMemoryProvider
from modules.memory import registry as mem_registry
from tools.controller.registry.service import Registry
from tools.controller.service import Controller


def _controller(user_id="tenant-A"):
    c = object.__new__(Controller)
    c.logger = logging.getLogger("memory-tool-test")
    c.registry = Registry()
    c.user_id = user_id
    c.session_id = "s1"
    return c


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "true")
    p = SqliteMemoryProvider(str(tmp_path / "memory.db"))
    mem_registry.reset_memory_registry()
    mem_registry.set_external_memory_provider(p)
    yield p
    mem_registry.reset_memory_registry()


def test_absent_when_flag_off(provider, monkeypatch):
    monkeypatch.delenv("MEMORY_TOOL_ENABLED", raising=False)
    c = _controller()
    c._register_memory_tool_action()
    assert "memory" not in c.registry.registry.actions


def test_absent_without_external_provider(monkeypatch):
    monkeypatch.setenv("MEMORY_TOOL_ENABLED", "true")
    mem_registry.reset_memory_registry()  # default Null
    c = _controller()
    c._register_memory_tool_action()
    assert "memory" not in c.registry.registry.actions
    mem_registry.reset_memory_registry()


def test_registered_when_enabled(provider, monkeypatch):
    monkeypatch.setenv("MEMORY_TOOL_ENABLED", "true")
    c = _controller()
    c._register_memory_tool_action()
    assert "memory" in c.registry.registry.actions


@pytest.mark.asyncio
async def test_add_read_remove_roundtrip(provider, monkeypatch):
    monkeypatch.setenv("MEMORY_TOOL_ENABLED", "true")
    c = _controller()
    c._register_memory_tool_action()
    action = c.registry.registry.actions["memory"]
    M = action.param_model
    await action.function(M(action="add", content="deploy on fridays"), execution_context=None)
    read = await action.function(M(action="read"), execution_context=None)
    assert "deploy on fridays" in read.extracted_content
    await action.function(M(action="remove", content="deploy on fridays"), execution_context=None)
    read2 = await action.function(M(action="read"), execution_context=None)
    assert "deploy on fridays" not in read2.extracted_content


@pytest.mark.asyncio
async def test_tenant_isolation(provider, monkeypatch):
    monkeypatch.setenv("MEMORY_TOOL_ENABLED", "true")
    ca = _controller("alice")
    ca._register_memory_tool_action()
    await ca.registry.registry.actions["memory"].function(
        ca.registry.registry.actions["memory"].param_model(action="add", content="alice note"),
        execution_context=None)
    cb = _controller("bob")
    cb._register_memory_tool_action()
    read = await cb.registry.registry.actions["memory"].function(
        cb.registry.registry.actions["memory"].param_model(action="read"),
        execution_context=None)
    assert "alice note" not in read.extracted_content
