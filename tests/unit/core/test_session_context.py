"""Verify SessionContext Protocol is satisfied by a minimal implementation."""

import pytest
from typing import runtime_checkable

from core.session_context import SessionContext


class FakeContext:
    """Minimal implementation to verify Protocol shape."""

    def __init__(self):
        self.session_id = "test-session-123"
        self.user_id = "user-456"
        self.workspace_dir = "/tmp/test-workspace"

    async def add_to_feed(self, agent_id: str, entry_type: str, data: dict) -> None:
        pass

    def get_agents(self) -> dict:
        return {}

    def get_tool_call_tracker(self):
        return None

    def get_sub_agent_manager(self):
        return None

    def get_telemetry_manager(self):
        return None


def test_fake_context_satisfies_protocol():
    """A class with the right attributes/methods satisfies SessionContext."""
    ctx = FakeContext()
    assert isinstance(ctx, SessionContext)


def test_missing_attribute_fails_protocol():
    """A class missing required attributes does NOT satisfy SessionContext."""

    class Incomplete:
        session_id = "x"
        # Missing user_id, workspace_dir, methods

    obj = Incomplete()
    assert not isinstance(obj, SessionContext)


def test_orchestrator_satisfies_protocol():
    """SessionOrchestrator structurally satisfies SessionContext."""
    from agents.task.agent.orchestrator import SessionOrchestrator

    required_methods = ['add_to_feed', 'get_agents', 'get_tool_call_tracker',
                        'get_sub_agent_manager', 'get_telemetry_manager']
    required_props = ['session_id', 'user_id', 'workspace_dir']

    for method_name in required_methods:
        member = getattr(SessionOrchestrator, method_name, None)
        assert member is not None, f"SessionOrchestrator missing method: {method_name}"
        assert callable(member) or isinstance(member, property), \
            f"SessionOrchestrator.{method_name} should be callable"

    for prop_name in required_props:
        member = getattr(SessionOrchestrator, prop_name, None)
        assert member is not None, f"SessionOrchestrator missing property: {prop_name}"
