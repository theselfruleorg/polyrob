from core.tool_catalog import build_tool_catalog, find_tool, permission_catalog


def test_tool_catalog_uses_descriptor_registry():
    entries = build_tool_catalog(env={})
    ids = {entry.id for entry in entries}

    assert "filesystem" in ids
    assert "mcp" in ids
    assert all(entry.category for entry in entries)
    assert all(entry.model_description for entry in entries)


def test_tool_catalog_reports_missing_config():
    entry = find_tool("perplexity", env={})

    assert entry is not None
    assert entry.enabled is False
    assert "perplexity_api_key" in (entry.disabled_reason or "")


def test_tool_catalog_reports_gated_dangerous_tools():
    twitter = find_tool("twitter", env={})
    polymarket = find_tool("polymarket", env={})

    assert twitter is not None
    assert twitter.enabled is False
    assert "TWITTER_ENABLED" in (twitter.disabled_reason or "")
    assert polymarket is not None
    assert polymarket.enabled is False
    assert "trading credentials" in (polymarket.disabled_reason or "")


def test_permission_catalog_maps_core_permissions():
    permissions = permission_catalog()

    assert "fs.read" in permissions
    assert "filesystem" in permissions["fs.read"]
