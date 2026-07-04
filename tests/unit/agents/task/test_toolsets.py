"""Tests for the named toolsets registry in agents/task/tool_defaults.py.

Covers:
- Every id in every TOOLSET is a valid VALID_TOOL_IDS member.
- code_execution is absent from every named set.
- resolve_toolset("research") returns the expected ids.
- resolve_toolset(unknown) falls back to the "default" set.
- POLYROB_AGENT_TOOLSET env drives cli_default_tools() through resolve_toolset.
- Unset POLYROB_AGENT_TOOLSET → output is byte-identical to the legacy path.
"""
import os
import importlib
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _import_tool_defaults():
    """Fresh import of tool_defaults, bypassing any cached state."""
    import agents.task.tool_defaults as m
    importlib.reload(m)
    return m


def _valid_tool_ids():
    from agents.task.agent.skill_manager import VALID_TOOL_IDS
    return VALID_TOOL_IDS


# ---------------------------------------------------------------------------
# TOOLSETS structure
# ---------------------------------------------------------------------------

def test_all_toolset_ids_are_valid():
    """Every id in every named toolset must be in VALID_TOOL_IDS."""
    m = _import_tool_defaults()
    valid = _valid_tool_ids()
    for name, ids in m.TOOLSETS.items():
        for tool_id in ids:
            assert tool_id in valid, (
                f"Toolset '{name}' contains '{tool_id}' which is NOT in VALID_TOOL_IDS"
            )


def test_code_execution_absent_from_all_toolsets():
    """code_execution must not appear in any named toolset (unsafe by default)."""
    m = _import_tool_defaults()
    for name, ids in m.TOOLSETS.items():
        assert "code_execution" not in ids, (
            f"Toolset '{name}' contains 'code_execution' — must be excluded"
        )


def test_toolsets_keys():
    """TOOLSETS has the expected set of named toolsets (incl. social)."""
    m = _import_tool_defaults()
    expected = {
        "minimal", "safe", "default", "research", "trading_research", "coding",
        "development", "browser", "social", "full",
    }
    assert set(m.TOOLSETS.keys()) == expected


def test_social_toolset_exists_with_valid_ids():
    """The 'social' toolset exists and uses only VALID_TOOL_IDS (no twitter)."""
    m = _import_tool_defaults()
    valid = _valid_tool_ids()
    assert "social" in m.TOOLSETS
    social = m.resolve_toolset("social")
    assert social  # non-empty
    for tool_id in social:
        assert tool_id in valid
    # twitter is NOT a valid tool id → must not be present.
    assert "twitter" not in social
    # social listening/research is reached via anysite + perplexity.
    assert "anysite" in social
    assert "perplexity" in social


def test_research_toolset_ids():
    """resolve_toolset('research') returns the expected tool ids."""
    m = _import_tool_defaults()
    result = m.resolve_toolset("research")
    assert result == ["filesystem", "task", "perplexity", "anysite", "web_fetch", "polymarket_data", "hyperliquid_data"]


def test_resolve_toolset_unknown_returns_default():
    """An unknown toolset name falls back to the 'default' set."""
    m = _import_tool_defaults()
    default = m.resolve_toolset("default")
    unknown = m.resolve_toolset("this_does_not_exist_xyz")
    assert unknown == default


def test_resolve_toolset_case_insensitive():
    """Toolset name lookup is case-insensitive."""
    m = _import_tool_defaults()
    assert m.resolve_toolset("RESEARCH") == m.resolve_toolset("research")
    assert m.resolve_toolset("Coding") == m.resolve_toolset("coding")


def test_resolve_toolset_returns_copy():
    """resolve_toolset returns a new list each time (mutation-safe)."""
    m = _import_tool_defaults()
    a = m.resolve_toolset("research")
    b = m.resolve_toolset("research")
    assert a == b
    a.append("EXTRA")
    assert "EXTRA" not in m.resolve_toolset("research")


# ---------------------------------------------------------------------------
# POLYROB_AGENT_TOOLSET env
# ---------------------------------------------------------------------------

def _cli_default_tools_with_env(env_value=None, monkeypatch=None):
    """Call cli_default_tools with POLYROB_AGENT_TOOLSET set/unset.

    We monkeypatch both os.environ AND mock the underlying conditional imports
    so the test doesn't need a live container.
    """
    import agents.task.tool_defaults as m
    importlib.reload(m)

    # Stub out coding_tools_enabled and anysite_cli_enabled so the legacy path
    # behaves deterministically (as if both are disabled → returns ['filesystem','task']).
    import unittest.mock as mock

    with mock.patch("tools.coding.coding_tools_enabled", return_value=False), \
         mock.patch("tools.anysite.anysite_cli_enabled", return_value=False), \
         mock.patch("core.bootstrap.cli_unavailable_tools", return_value=[]):

        if env_value is not None:
            with mock.patch.dict(os.environ, {"POLYROB_AGENT_TOOLSET": env_value}):
                importlib.reload(m)
                return m.cli_default_tools()
        else:
            # Ensure env var is absent.
            env_without = {k: v for k, v in os.environ.items() if k != "POLYROB_AGENT_TOOLSET"}
            with mock.patch.dict(os.environ, env_without, clear=True):
                importlib.reload(m)
                return m.cli_default_tools()


def test_polyrob_agent_toolset_drives_cli_default_tools():
    """When POLYROB_AGENT_TOOLSET=research, cli_default_tools returns research tools."""
    import unittest.mock as mock
    import agents.task.tool_defaults as m
    importlib.reload(m)

    with mock.patch("tools.coding.coding_tools_enabled", return_value=False), \
         mock.patch("tools.anysite.anysite_cli_enabled", return_value=False), \
         mock.patch("core.bootstrap.cli_unavailable_tools", return_value=[]), \
         mock.patch.dict(os.environ, {"POLYROB_AGENT_TOOLSET": "research"}):
        importlib.reload(m)
        result = m.cli_default_tools()

    assert result == ["filesystem", "task", "perplexity", "anysite", "web_fetch", "polymarket_data", "hyperliquid_data"]


def test_polyrob_agent_toolset_unset_legacy_path():
    """When POLYROB_AGENT_TOOLSET is unset, cli_default_tools is byte-identical to the old path."""
    import unittest.mock as mock
    import agents.task.tool_defaults as m
    importlib.reload(m)

    env_without = {k: v for k, v in os.environ.items() if k != "POLYROB_AGENT_TOOLSET"}
    with mock.patch("tools.coding.coding_tools_enabled", return_value=False), \
         mock.patch("tools.anysite.anysite_cli_enabled", return_value=False), \
         mock.patch("core.bootstrap.cli_unavailable_tools", return_value=[]), \
         mock.patch.dict(os.environ, env_without, clear=True):
        importlib.reload(m)
        result = m.cli_default_tools()

    # Legacy path with coding=False and anysite=False → [filesystem, task, web_fetch].
    # web_fetch is the lightweight default web reader (zero-dep, always CLI-registerable).
    assert result == ["filesystem", "task", "web_fetch"]


def test_polyrob_agent_toolset_pruned_via_cli_unavailable():
    """When POLYROB_AGENT_TOOLSET is set, unavailable tools are pruned from the result."""
    import unittest.mock as mock
    import agents.task.tool_defaults as m
    importlib.reload(m)

    # Simulate CLI marking 'browser' and 'perplexity' as unavailable.
    def fake_unavailable(tools):
        return [t for t in tools if t in {"browser", "perplexity"}]

    with mock.patch("tools.coding.coding_tools_enabled", return_value=False), \
         mock.patch("tools.anysite.anysite_cli_enabled", return_value=False), \
         mock.patch("core.bootstrap.cli_unavailable_tools", side_effect=fake_unavailable), \
         mock.patch.dict(os.environ, {"POLYROB_AGENT_TOOLSET": "research"}):
        importlib.reload(m)
        result = m.cli_default_tools()

    # research = [filesystem, task, perplexity, anysite, web_fetch, polymarket_data, hyperliquid_data]
    # after pruning browser + perplexity → drop perplexity (browser not present)
    assert result == ["filesystem", "task", "anysite", "web_fetch", "polymarket_data", "hyperliquid_data"]
