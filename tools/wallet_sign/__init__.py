"""``wallet_sign`` — classified off-chain signatures with the agent's wallet (proposal 090).

Registered with the on-chain money verbs: it signs with the same key ``defi_trade``
sends from (090 D8), so it exists exactly when ``DEFI_TRADE_ENABLED`` arms that key
for the agent (090 D7: no flag of its own). Never in the default tool_ids; granted
with ``defi_trade`` (``grant:defi_autonomous``).
"""


def wallet_sign_enabled() -> bool:
    from core.config_policy import defi_trade_enabled
    return defi_trade_enabled()


def register_wallet_sign_tool(force: bool = False) -> bool:
    """Register the ``wallet_sign`` descriptor + class IFF the wallet is armed (or forced)."""
    from tools.descriptors import ToolCategory, ToolDescriptor, register_optional_tool
    from tools.wallet_sign.tool import WalletSignTool

    return register_optional_tool(
        "wallet_sign",
        WalletSignTool,
        ToolDescriptor(
            name="wallet_sign",
            description=("Sign in to a site or prove wallet ownership with the agent's own "
                         "wallet: sign a login/challenge TEXT (EIP-191, SIWE, Sign-In With "
                         "Solana) bound to its site, and call that site's API with the "
                         "credential it returns. Never a hash, transaction, permit or order."),
            category=ToolCategory.INTEGRATION,
            is_optional=True,
            init_priority=81,
        ),
        wallet_sign_enabled,
        force=force,
    )


__all__ = ["register_wallet_sign_tool", "wallet_sign_enabled"]
