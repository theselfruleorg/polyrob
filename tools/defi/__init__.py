"""Agent DeFi tooling — read-only token sight (proposal 023 T0+T1).

No signer is constructed and nothing is broadcast from this package.
"""


def defi_data_enabled() -> bool:
    """Gate for the `defi_data` read tool. Default OFF.

    Thin re-export of the tier-0 SSOT (`core.config_policy.policy`) so this and
    `agents/task/tool_defaults` can never disagree about whether the tool is on.
    """
    from core.config_policy import defi_data_enabled as _enabled
    return _enabled()


def defi_trade_enabled() -> bool:
    """Gate for the on-chain money verbs. Default OFF, never in the local safe
    group — this one can move real funds."""
    from core.config_policy import defi_trade_enabled as _enabled
    return _enabled()


def register_defi_trade_tool(force: bool = False) -> bool:
    """Register `defi_trade` IFF DEFI_TRADE_ENABLED. Never in default tool_ids —
    money tools are explicit-grant-only."""
    from tools.descriptors import ToolCategory, ToolDescriptor, register_optional_tool
    from tools.defi.trade_tool import DefiTradeTool

    return register_optional_tool(
        "defi_trade",
        DefiTradeTool,
        ToolDescriptor(
            name="defi_trade",
            description="Move tokens on-chain from the agent wallet (simulated and capped)",
            category=ToolCategory.INTEGRATION,
            is_optional=True,
            init_priority=80,
        ),
        defi_trade_enabled,
        force=force,
    )


def register_defi_data_tool(force: bool = False) -> bool:
    """Register the `defi_data` descriptor + class IFF DEFI_DATA_ENABLED.

    Mirrors tools/x402/__init__.py::register_x402_tool, but routes through
    `register_optional_tool` so the capability-classification guard actually
    fires (a tool with no row in core/tool_capabilities.py raises).
    """
    from tools.descriptors import ToolCategory, ToolDescriptor, register_optional_tool
    from tools.defi.data_tool import DefiDataTool

    return register_optional_tool(
        "defi_data",
        DefiDataTool,
        ToolDescriptor(
            name="defi_data",
            description=("Read-only token sight on Base: contract identity, price, "
                         "liquidity, safety screen, own holdings, raw contract reads"),
            category=ToolCategory.INTEGRATION,
            is_optional=True,
            init_priority=80,
        ),
        defi_data_enabled,
        force=force,
    )
