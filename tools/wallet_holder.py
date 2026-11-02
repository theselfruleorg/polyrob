"""The agent wallet, resolved once per tool — a mixin for the money tools.

``defi_trade``, ``launchpad`` and ``dapp_browser`` each carried the same
three-line ``_get_wallet``: an injected ``_wallet`` (the test seam) wins,
else the ONE factory. It lives here so a fourth money tool cannot resolve the
wallet a different way.
"""
from __future__ import annotations


class WalletHolderMixin:
    _wallet = None

    def _get_wallet(self):
        if self._wallet is not None:
            return self._wallet
        from core.wallet.factory import get_agent_wallet
        return get_agent_wallet()

    def _wallet_gap(self):
        """095 W2: ``("wallet-off", remedy)`` when the agent wallet is off on
        this instance (every call of a money tool refuses), else None. An
        injected wallet (the test seam) is never a gap. Fail-open."""
        if self._wallet is not None:
            return None
        try:
            import os
            from core.env import bool_from
            if bool_from(os.environ, "AGENT_WALLET_ENABLED", False):
                return None
            from core.tool_gates import remedy_text
            return ("wallet-off", remedy_text("wallet-off"))
        except Exception:
            return None
