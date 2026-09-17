"""Registration + gate for the optional ``x_browser`` tool (Task 9 fills the tool).

Kept separate from ``tool.py`` so ``tools/x_browser/__init__.py`` (and the
session-store, which imports the package) never pulls the Playwright-heavy tool
module just to read the gate.
"""
from __future__ import annotations


def x_browser_enabled() -> bool:
    """Whether the browser-based X tool is registered. Default OFF."""
    from core.config_policy.policy import _bool_env
    return _bool_env("X_BROWSER_ENABLED", False)


def register_x_browser_tool(force: bool = False) -> bool:
    """Register the ``x_browser`` descriptor + class IFF enabled (or forced)."""
    from tools.descriptors import (
        ToolCategory,
        ToolDescriptor,
        register_optional_tool,
    )
    from tools.x_browser.tool import XBrowserTool

    return register_optional_tool(
        "x_browser",
        XBrowserTool,
        ToolDescriptor(
            name="x_browser",
            description=("Read and send X DMs, post to X, and register an "
                         "account through a real browser on a saved login. Verbs: "
                         "x_read_dms / x_dm / x_post / x_login_check / "
                         "x_signup_start."),
            category=ToolCategory.COMMUNICATION,
            required_config=[],
            init_priority=45,
            is_optional=True,
        ),
        x_browser_enabled,
        force=force,
    )
