import core.bootstrap as bootstrap


def test_warn_lists_unavailable_tools():
    # "browser" is CLI-available since 2026-07-19 (the actual Chromium/playwright
    # launch was always lazy; browser_manager just needed a special-cased constructor
    # call) and "mcp" since 2026-07-20 (S3: light __init__, MCP_ENABLED-gated
    # registration) — see core/bootstrap.py. "perplexity" stays server-only.
    missing = bootstrap.cli_unavailable_tools(
        ["filesystem", "task", "browser", "mcp", "perplexity"])
    assert "browser" not in missing
    assert "mcp" not in missing
    assert "perplexity" in missing
    assert "filesystem" not in missing
    assert "task" not in missing


def test_no_missing_for_core_only():
    assert bootstrap.cli_unavailable_tools(["filesystem", "task"]) == []
