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


def test_p2_12_read_path_ttl_expiry():
    """P2-12: should_inject_schema TTL-cleans on read — a stale failure record lapses
    even with no further track_failure calls (previously a session-permanent block)."""
    from datetime import datetime, timedelta
    from tools.mcp.validation_tracker import MCPValidationTracker

    t = MCPValidationTracker(failure_threshold=2, failure_ttl_minutes=30)
    t.track_failure("srv", "tool")
    t.track_failure("srv", "tool")
    assert t.should_inject_schema("srv", "tool") is True
    # age the recorded timestamp past the TTL
    t._timestamps["srv:tool"] = datetime.now() - timedelta(minutes=31)
    # read path must now lapse the expired record instead of blocking forever
    assert t.should_inject_schema("srv", "tool") is False
