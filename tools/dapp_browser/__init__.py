"""Web dapps, driven with the agent's own wallet (042).

The gap: the agent could read any dapp and click through any dapp's UI, and
every one of them was read-only — the page found no ``window.ethereum``, so
Connect did nothing. That is most of DeFi, every NFT mint, and every launchpad
that never shipped an API.

Three pieces:

- ``js``      — the injected EIP-1193 provider (plus the EIP-6963 announcement
  modern dapps actually listen for). A POSTBOX: no key, no signing, no policy.
  A page that tampers with it can only lie to itself.
- ``bridge``  — the Python half, where the policy is. Reads go to the pinned
  RPC; ``eth_sendTransaction`` becomes a ``TxIntent`` bounded by the envelope
  the agent declared and goes through ``tx_guard``; off-chain SIGNATURES are
  refused always, because a permit is submitted by someone else later and no
  simulation can catch it.
- ``tool``    — ``dapp_connect`` / ``dapp_status`` / ``dapp_disconnect``.

⚠️ ``dapp_connect`` is the MONEY verb. The spend happens when the page calls
``eth_sendTransaction``, not in an action the Controller can see, so connecting
a wallet with a declared budget is the act of authorization and that is what is
gated. Every individual transaction is still simulated, asserted and capped.

Gate: ``DAPP_BROWSER_ENABLED`` (default OFF). Capabilities: money + high_impact
+ delegate_blocked. Never in the default tool_ids.
"""

__all__ = ["register_dapp_browser_tool", "dapp_browser_enabled"]


def __getattr__(name):
    if name in __all__:
        from tools.dapp_browser import registration
        return getattr(registration, name)
    raise AttributeError(name)
