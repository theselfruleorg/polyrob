"""Agent NFTs (proposals 050, 069, 123, 124) — the core ``agent_nft`` tool.

An agent NFT is an ERC-721 whose ERC-6551 token-bound account is controlled by whoever owns the
NFT. This instance acts through an account only when its treasury OWNS the NFT (069 v4: no
grants, bindings or operator keys; the key signs, the account holds). Every verb is core (123
S4: there is no agent-NFT package seam); every write verb goes through ``tx_guard``
(``TxIntent.via_account`` for a call through the account).

Gate: ``AGENT_NFT_ENABLED`` (default OFF). Never in the default tool_ids.
"""

__all__ = ["register_agent_nft_tool", "agent_nft_enabled"]


def __getattr__(name):
    if name in __all__:
        from tools.agent_nft import registration
        return getattr(registration, name)
    raise AttributeError(name)
