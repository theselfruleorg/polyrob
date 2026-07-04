"""Regression: web_fetch must be registered as a service in the headless/CLI container.

Bug (2026-07-01, capability test): `web_fetch` is in `_CLI_REGISTERABLE_TOOLS` and has a real
tool class, but `register_cli_tools` never registered a `web_fetch` service — so a goal/headless
session with `--tools web_fetch` silently had no web_fetch action (agent reported "web_fetch is
NOT available"). Same class as the twitter-loading bug.
"""
import inspect

import core.bootstrap as bootstrap


def test_web_fetch_in_cli_registerable_tools():
    assert "web_fetch" in bootstrap._CLI_REGISTERABLE_TOOLS


def test_register_cli_tools_registers_web_fetch():
    src = inspect.getsource(bootstrap.register_cli_tools)
    assert "WebFetchTool" in src
    assert '"web_fetch"' in src
