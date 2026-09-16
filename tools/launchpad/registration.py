"""Registration + gate for the optional ``launchpad`` tool (042).

Kept separate from ``tool.py`` so importing the package to read the gate never
pulls the tool module.
"""
from __future__ import annotations


def launchpad_enabled() -> bool:
    """Whether the launchpad tool is registered. Default OFF."""
    from core.config_policy.policy import _bool_env
    return _bool_env("LAUNCHPAD_ENABLED", False)


def register_launchpad_tool(force: bool = False) -> bool:
    """Register the ``launchpad`` descriptor + class IFF enabled (or forced)."""
    from tools.descriptors import (
        ToolCategory,
        ToolDescriptor,
        register_optional_tool,
    )
    from tools.launchpad.tool import LaunchpadTool

    return register_optional_tool(
        "launchpad",
        LaunchpadTool,
        ToolDescriptor(
            name="launchpad",
            description=("Launch a token on the Pons V2 launchpad (Robinhood "
                         "Chain) and trade one on its bonding curve. Verbs: "
                         "launchpad_launch / launchpad_buy / launchpad_sell / "
                         "launchpad_quote / launchpad_status."),
            category=ToolCategory.INTEGRATION,
            required_config=[],
            init_priority=46,
            is_optional=True,
        ),
        launchpad_enabled,
        force=force,
    )
