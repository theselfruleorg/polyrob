"""Tests for tools.descriptors.register_optional_tool (Task 1.2)."""
import pytest
from unittest.mock import patch

from tools import descriptors
from tools.descriptors import ToolDescriptor, ToolCategory, TOOL_DESCRIPTORS


def _make_desc(name: str) -> ToolDescriptor:
    """Minimal valid ToolDescriptor for test use."""
    return ToolDescriptor(
        name=name,
        description="test tool",
        category=ToolCategory.INTEGRATION,
        is_optional=True,
        init_priority=80,
    )


class DummyTool:
    pass


class TestRegisterOptionalTool:
    def test_disabled_returns_false_and_does_not_register(self):
        """When enabled_fn returns False, register_optional_tool returns False and
        does not touch TOOL_DESCRIPTORS or call register_tool_class."""
        desc = _make_desc("_test_dummy_disabled")
        called = {"count": 0}

        def fake_register(name, cls):
            called["count"] += 1

        with patch.object(descriptors, "register_tool_class", fake_register):
            result = descriptors.register_optional_tool(
                "_test_dummy_disabled", DummyTool, desc, lambda: False
            )

        assert result is False
        assert called["count"] == 0
        assert "_test_dummy_disabled" not in TOOL_DESCRIPTORS

    def test_enabled_returns_true_and_registers(self):
        """When enabled_fn returns True, returns True and registers the descriptor + class."""
        tool_name = "_test_dummy_enabled"
        # Ensure clean state
        TOOL_DESCRIPTORS.pop(tool_name, None)
        desc = _make_desc(tool_name)
        registered_args = {}

        def fake_register(name, cls):
            registered_args["name"] = name
            registered_args["cls"] = cls

        with patch.object(descriptors, "register_tool_class", fake_register):
            result = descriptors.register_optional_tool(
                tool_name, DummyTool, desc, lambda: True
            )

        assert result is True
        assert registered_args.get("name") == tool_name
        assert registered_args.get("cls") is DummyTool
        # Descriptor was inserted
        assert tool_name in TOOL_DESCRIPTORS
        assert TOOL_DESCRIPTORS[tool_name] is desc

        # Cleanup
        TOOL_DESCRIPTORS.pop(tool_name, None)

    def test_descriptor_insertion_is_idempotent(self):
        """If descriptor already in TOOL_DESCRIPTORS, existing one is not overwritten."""
        tool_name = "_test_dummy_idempotent"
        TOOL_DESCRIPTORS.pop(tool_name, None)
        original_desc = _make_desc(tool_name)
        TOOL_DESCRIPTORS[tool_name] = original_desc

        new_desc = _make_desc(tool_name)
        registered_args = {}

        def fake_register(name, cls):
            registered_args["name"] = name

        with patch.object(descriptors, "register_tool_class", fake_register):
            result = descriptors.register_optional_tool(
                tool_name, DummyTool, new_desc, lambda: True
            )

        assert result is True
        # Original descriptor preserved (not replaced by new_desc)
        assert TOOL_DESCRIPTORS[tool_name] is original_desc

        # Cleanup
        TOOL_DESCRIPTORS.pop(tool_name, None)

    def test_force_overrides_disabled_gate(self):
        """force=True registers even when enabled_fn returns False."""
        tool_name = "_test_dummy_force"
        TOOL_DESCRIPTORS.pop(tool_name, None)
        desc = _make_desc(tool_name)
        registered_args = {}

        def fake_register(name, cls):
            registered_args["name"] = name
            registered_args["cls"] = cls

        with patch.object(descriptors, "register_tool_class", fake_register):
            result = descriptors.register_optional_tool(
                tool_name, DummyTool, desc, lambda: False, force=True
            )

        assert result is True
        assert registered_args.get("name") == tool_name
        assert tool_name in TOOL_DESCRIPTORS

        # Cleanup
        TOOL_DESCRIPTORS.pop(tool_name, None)
