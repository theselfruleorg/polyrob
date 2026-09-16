"""Registration + gate for the optional ``dapp_browser`` tool (042)."""
from __future__ import annotations


def dapp_browser_enabled() -> bool:
    """Whether the injected dapp wallet is registered. Default OFF."""
    from core.config_policy.policy import _bool_env
    return _bool_env("DAPP_BROWSER_ENABLED", False)


def register_dapp_browser_tool(force: bool = False) -> bool:
    """Register the ``dapp_browser`` descriptor + class IFF enabled (or forced)."""
    from tools.dapp_browser.tool import DappBrowserTool
    from tools.descriptors import (
        ToolCategory,
        ToolDescriptor,
        register_optional_tool,
    )

    return register_optional_tool(
        "dapp_browser",
        DappBrowserTool,
        ToolDescriptor(
            name="dapp_browser",
            description=("Open a web dapp with the agent's own wallet attached "
                         "(EIP-1193 + EIP-6963), so its Connect button works "
                         "and its transactions can be signed under a declared "
                         "spend envelope. Verbs: dapp_connect / dapp_status / "
                         "dapp_disconnect."),
            category=ToolCategory.BROWSER,
            required_config=[],
            init_priority=47,
            is_optional=True,
        ),
        dapp_browser_enabled,
        force=force,
    )
