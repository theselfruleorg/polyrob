"""What an address IS — a wallet, a token, or something else (071 §3.5).

The verbs that take an address used to accept any well-formed one and answer
a different question than the one asked: `token_info(<a Solana wallet>)` came
back "symbol unknown / price unknown", which reads as "a worthless token"
when the truth was "that is a wallet — read its holdings instead". One chat
user's "check this address" died exactly there.

``classify(chain, address)`` returns ``{"kind": ...}`` or ``None``:

* ``wallet``        — EVM: no code (an EOA, or EIP-7702 delegated EOA).
                      SVM: System-owned, or not yet funded.
* ``token``         — EVM: has code and answers ``decimals()``. SVM: a mint.
* ``token_account`` — SVM only; carries ``mint`` and ``owner``.
* ``contract`` / ``program`` / ``other``.

``None`` is UNKNOWN (the read failed): every caller must proceed exactly as it
did before this module existed, never refuse on it.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

#: EIP-7702: an EOA that delegated to code carries 0xef0100 || address. It is
#: still a person's wallet, not a token.
_DELEGATION_PREFIX = "0xef0100"
_DECIMALS_SELECTOR = "0x313ce567"


def _evm_probe(chain: str, method: str, params: list):
    from core.wallet.onchain import _rpc, rpc_url_for_chain
    url = rpc_url_for_chain(chain)
    if not url:
        raise RuntimeError(f"no RPC for {chain}")
    return _rpc(url, method, params, 6.0)


def _svm_probe(address: str):
    from core.wallet import solana_onchain
    return solana_onchain.account_kind(address)


def classify(chain: str, address: str) -> Optional[Dict[str, str]]:
    from core.wallet import chains
    row = chains.get(chain)
    if row is None:
        return None
    try:
        if row.family == "svm":
            got = _svm_probe(address)
            if got is None:
                return None
            if got.get("kind") == "mint":
                return {**got, "kind": "token"}
            return got
        code = _evm_probe(chain, "eth_getCode", [address, "latest"])
        if not isinstance(code, str):
            return None
        code = code.lower()
        if code in ("0x", "0x0", "") or code.startswith(_DELEGATION_PREFIX):
            return {"kind": "wallet"}
        try:
            dec = _evm_probe(chain, "eth_call",
                             [{"to": address, "data": _DECIMALS_SELECTOR}, "latest"])
        except Exception:
            dec = None
        if isinstance(dec, str) and len(dec) >= 66:
            return {"kind": "token"}
        return {"kind": "contract"}
    except Exception as exc:
        logger.debug("address_kind: %s on %s unreadable (%s)", address, chain, exc)
        return None


def wrong_kind_for_token(chain: str, address: str, got: Optional[Dict[str, str]]) -> Optional[str]:
    """The refusal a TOKEN verb gives when the address is definitely not a
    token — naming the verb that answers the real question. ``None`` = proceed."""
    if not got:
        return None
    kind = got.get("kind")
    if kind == "wallet":
        return (f"{address} on {chain} is a WALLET address, not a token contract/mint. "
                f"To see what it holds, call defi_data.wallet_holdings(address='{address}', "
                f"chain='{chain}').")
    if kind == "token_account":
        mint = got.get("mint") or "?"
        owner = got.get("owner") or "?"
        return (f"{address} on {chain} is a TOKEN ACCOUNT (one balance slot), not a mint. "
                f"Its mint is {mint} (use that for token_info/price) and its owner wallet "
                f"is {owner} (use defi_data.wallet_holdings for its holdings).")
    if kind == "program":
        return f"{address} on {chain} is a PROGRAM, not a token mint."
    return None


def wrong_kind_for_wallet(chain: str, address: str, got: Optional[Dict[str, str]]) -> Optional[str]:
    """The refusal ``wallet_holdings`` gives for a token/program address."""
    if not got:
        return None
    kind = got.get("kind")
    if kind == "token":
        return (f"{address} on {chain} is a TOKEN, not a wallet. For its identity, price "
                f"and safety screen call defi_data.token_info(address='{address}', "
                f"chain='{chain}'); for who holds it, defi_data.token_holders.")
    if kind == "token_account":
        owner = got.get("owner") or "?"
        return (f"{address} on {chain} is a TOKEN ACCOUNT, not a wallet. Its owner "
                f"wallet is {owner} — call wallet_holdings with that address.")
    if kind == "program":
        return f"{address} on {chain} is a PROGRAM, not a wallet; it holds no portfolio."
    return None
