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
