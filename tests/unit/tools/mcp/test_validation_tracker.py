"""Unit tests for MCPValidationTracker (PR11 — extracted from ToolCallTracker)."""

from tools.mcp.validation_tracker import MCPValidationTracker


def test_threshold_triggers_schema_injection():
    t = MCPValidationTracker(failure_threshold=2)
    assert t.should_inject_schema("srv", "tool") is False
    assert t.track_failure("srv", "tool") == 1
    assert t.should_inject_schema("srv", "tool") is False
    assert t.track_failure("srv", "tool") == 2
    assert t.should_inject_schema("srv", "tool") is True


def test_clear_resets_single_tool():
    t = MCPValidationTracker(failure_threshold=1)
    t.track_failure("srv", "tool")
    assert t.should_inject_schema("srv", "tool") is True
    t.clear_failures("srv", "tool")
    assert t.should_inject_schema("srv", "tool") is False


def test_reset_clears_all():
    t = MCPValidationTracker(failure_threshold=1)
    t.track_failure("a", "x")
    t.track_failure("b", "y")
    t.reset()
    assert t.should_inject_schema("a", "x") is False
    assert t.should_inject_schema("b", "y") is False


def test_per_tool_isolation():
    t = MCPValidationTracker(failure_threshold=2)
    t.track_failure("srv", "tool1")
    t.track_failure("srv", "tool1")
    assert t.should_inject_schema("srv", "tool1") is True
    assert t.should_inject_schema("srv", "tool2") is False
