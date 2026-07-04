import core.bootstrap as bootstrap


def test_warn_lists_unavailable_tools():
    missing = bootstrap.cli_unavailable_tools(["filesystem", "task", "browser", "mcp"])
    assert "browser" in missing
    assert "mcp" in missing
    assert "filesystem" not in missing
    assert "task" not in missing


def test_no_missing_for_core_only():
    assert bootstrap.cli_unavailable_tools(["filesystem", "task"]) == []
