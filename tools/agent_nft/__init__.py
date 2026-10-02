"""Agent NFTs (proposals 050, 069) — the thin core surface of the ``agent_nft`` tool.

An agent NFT is an ERC-721 whose ERC-6551 token-bound account is controlled by
whoever owns the NFT. This instance acts through an account only when its
treasury OWNS the NFT (069 v4: no grants, bindings or operator keys; the key
signs, the account holds). The package verbs (inspect, bind identity, journal,
revoke, withdraw) live in the
optional agent-NFT package (``polyrob_drop``; see
``core.tool_capabilities.AGENT_NFT_PACKAGE_MODULES``), installed separately — no
``polyrob[...]`` extra carries it; 069 WS-B moves the generic half into a pack. The
collection reveal body lives here, in core. This module is only what core's
gates must see (D25, decided 2026-09-23):

- the ``agent_nft`` capability row + the five money-verb lists name REAL action
  names, so the bidirectional ratchets (``test_money_verb_registration``,
  ``test_action_name_parity``) hold whether or not the package is installed;
- every write verb goes through ``tx_guard`` (``TxIntent.via_account`` for a
  call through the account).

Gate: ``AGENT_NFT_ENABLED`` (default OFF). Never in the default tool_ids.
"""

__all__ = ["register_agent_nft_tool", "agent_nft_enabled"]


def __getattr__(name):
    if name in __all__:
        from tools.agent_nft import registration
        return getattr(registration, name)
    raise AttributeError(name)
