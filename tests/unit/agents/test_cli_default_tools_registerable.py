"""F2 (043 A44 / A9, 2026-09-14): ``_dynamic_default_tools()`` appends ``defi_data``
when ``DEFI_DATA_ENABLED``, but ``cli_default_tools()`` used to strip it right back
out via ``cli_unavailable_tools`` because ``defi_data`` was absent from
``core.bootstrap._CLI_OPTIONAL_REGISTRARS`` / ``_CLI_REGISTERABLE_TOOLS`` — the CLI
advertised a tool it could never actually register. ``core/bootstrap.py:553-560``'s
self-check exists for exactly this class of drift.
"""


def test_cli_default_tools_are_all_registerable(monkeypatch):
    monkeypatch.setenv("DEFI_DATA_ENABLED", "true")
    monkeypatch.delenv("POLYROB_AGENT_TOOLSET", raising=False)
    from agents.task.tool_defaults import cli_default_tools
    from core.bootstrap import _CLI_REGISTERABLE_TOOLS
    assert set(cli_default_tools()) <= set(_CLI_REGISTERABLE_TOOLS)


def test_defi_data_reaches_the_cli_default_toolset_when_enabled(monkeypatch):
    monkeypatch.setenv("DEFI_DATA_ENABLED", "true")
    monkeypatch.delenv("POLYROB_AGENT_TOOLSET", raising=False)
    from agents.task.tool_defaults import cli_default_tools
    assert "defi_data" in cli_default_tools()


def test_defi_data_is_in_cli_registerable_tools():
    from core.bootstrap import _CLI_REGISTERABLE_TOOLS
    assert "defi_data" in _CLI_REGISTERABLE_TOOLS


def test_money_and_high_impact_defi_tools_stay_off_the_cli_default_rig():
    """Reach, never policy: only the read-only book joins the CLI registrar table.
    defi_trade/launchpad/dapp_browser/x_browser move real funds or otherwise carry
    money/delegate-blocked capability — they stay explicit-grant-only."""
    from core.bootstrap import _CLI_OPTIONAL_TOOLS
    for tool_id in ("defi_trade", "launchpad", "dapp_browser", "x_browser"):
        assert tool_id not in _CLI_OPTIONAL_TOOLS, tool_id
