"""``/check`` — look at any address or ticker from the owner's seat (071).

The trigger: a chat user asked for a Solana wallet to be checked and nothing
could do it. The agent now has ``defi_data.wallet_holdings``; this is the same
read for the owner without a model turn, on Telegram and in the REPL (the REPL
imports :func:`check_reply`, so the two seats cannot answer differently).

What it does with the first word:

* a base58 address  → read on solana: a wallet's holdings, or a mint's report;
* a 0x address + chain → the same on that chain;
* a 0x address alone → find the EVM chains where it is a TOKEN (code + decimals)
  and report those; if it is a wallet everywhere, show its native balance on
  every EVM chain and ask for a chain for the token list (one address holds
  different things on each chain, and a full read of every chain is slow);
* anything else → a ticker: ranked candidate addresses, never a pick.

Read-only. It never writes a trust store and never moves value.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import List, Optional

logger = logging.getLogger(__name__)

USAGE = """Usage: /check <address> [chain]
/check <TICKER>
A wallet: what it holds. A token: identity, price, safety screen, holders.
A base58 address is read on solana. A 0x address needs a chain for the tokens it
holds — without one I find where it is a token, or show its native balance on
every EVM chain."""

_EVM_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_TOKEN_CHAINS_SHOWN = 3


def _evm_chains() -> List[str]:
    from core.wallet import chains
    return [r.name for r in chains.all_rows() if r.family == "evm"]


async def _run(user_id: str, verb: str, params):
    from core.exec_identity import reset_exec_identity, set_exec_identity
    from surfaces.telegram.token_ops import _owner_ctx
    from tools.defi.data_tool import DefiDataTool
    token = set_exec_identity(user_id, None)
    try:
        return await getattr(DefiDataTool(), verb)(params, _owner_ctx(user_id))
    finally:
        reset_exec_identity(token)


#: Operator remedies that name an env var. The owner reads /check on a phone;
#: the env-var remedy is for the operator's config, so the owner text drops it
#: (the model-facing tool result keeps it). Pure text rewrites.
_OWNER_REWRITES = (
    (re.compile(r"\s*A keyed RPC pinned in DEFI_SOLANA_RPC makes the per-address read reliable\.?"), ""),
    (re.compile(r" \(or raise DEFI_PORTFOLIO_PRICE_BUDGET_SEC\)"), ""),
    (re.compile(r"no ALCHEMY_API_KEY, so holdings could not be enumerated"),
     "no token index is configured, so holdings could not be listed in full"),
)


def _owner_text(text: str) -> str:
    for pattern, repl in _OWNER_REWRITES:
        text = pattern.sub(repl, text)
    return text


def _render(result) -> str:
    from surfaces.telegram.token_ops import _render as render
    return _owner_text(render(result, retry_hint=None))


async def _one(user_id: str, address: str, chain: str) -> str:
    """A wallet's holdings or a token's report on ONE chain."""
    from core.wallet.address_kind import classify
    from tools.defi.data_tool import TokenRefParams, WalletHoldingsParams
    kind = await asyncio.to_thread(classify, chain, address)
    k = (kind or {}).get("kind")
    if k == "token":
        return _render(await _run(user_id, "token_info",
                                  TokenRefParams(chain=chain, address=address)))
    if k == "token_account":
        mint = kind.get("mint") or "?"
        owner = kind.get("owner") or "?"
        return (f"{address} on {chain} is a token ACCOUNT (one balance slot).\n"
                f"  mint:  {mint}   → /check {mint}\n"
                f"  owner: {owner}   → /check {owner}")
    # A wallet, a contract (a Safe holds tokens too), or unknown: the holdings
    # read refuses by itself if the address turns out to be something else.
    return _render(await _run(user_id, "wallet_holdings",
                              WalletHoldingsParams(address=address, chain=chain)))


async def _evm_everywhere(user_id: str, address: str) -> str:
    """A bare 0x address: where is it a token, else native balance per chain."""
    from core.wallet.address_kind import classify
    from core.wallet.onchain import balances
    names = _evm_chains()
    kinds = await asyncio.gather(*(asyncio.to_thread(classify, c, address) for c in names),
                                 return_exceptions=True)
    token_on = [c for c, k in zip(names, kinds)
                if isinstance(k, dict) and k.get("kind") == "token"]
    if token_on:
        parts = [await _one(user_id, address, c) for c in token_on[:_TOKEN_CHAINS_SHOWN]]
        if len(token_on) > _TOKEN_CHAINS_SHOWN:
            parts.append(f"(also a token on: {', '.join(token_on[_TOKEN_CHAINS_SHOWN:])} — "
                         f"/check {address} <chain>)")
        return "\n\n".join(parts)
    natives = await asyncio.gather(*(asyncio.to_thread(balances, address, c) for c in names),
                                   return_exceptions=True)
    from core.wallet import chains
    lines = [f"{address} — native balance on each EVM chain:"]
    for c, got in zip(names, natives):
        row = chains.get(c)
        sym = row.native_symbol if row else "native"
        native = got[0] if isinstance(got, tuple) else None
        shown = "UNKNOWN (read failed — not zero)" if native is None else f"{native:,.6f} {sym}"
        lines.append(f"  {c:<18} {shown}")
    lines += ["", f"Tokens differ per chain. For the full list: /check {address} <chain>"]
    return "\n".join(lines)


async def check_reply(user_id: Optional[str], args: List[str]) -> str:
    if not user_id:
        return "Only the owner can use /check."
    words = [w for w in (args or []) if w and w.strip()]
    if not words:
        return USAGE
    target = words[0].strip()
    chain = words[1].strip().lower() if len(words) > 1 else None
    if chain is not None:
        from core.wallet import chains
        if chains.get(chain) is None:
            return f"I do not know the chain {chain!r}. Chains: {', '.join(chains.names())}."
    try:
        if _EVM_ADDR.match(target):
            if chain is None:
                return await _evm_everywhere(user_id, target)
            return await _one(user_id, target, chain)
        if _BASE58.match(target):
            return await _one(user_id, target, chain or "solana")
        from tools.defi.data_tool import ResolveParams
        return ("Not an address — searched it as a ticker.\n"
                + _render(await _run(user_id, "token_resolve", ResolveParams(symbol=target))))
    except Exception as exc:
        logger.debug("/check failed", exc_info=True)
        return (f"I could not complete the check ({type(exc).__name__}: {exc}). "
                f"That is UNKNOWN, not a clean result.")


async def check_verb(*, user_id: str, data_dir: str, args: List[str],
                     task_agent=None, result=None, board=None) -> str:
    """Contributed-verb entry (``core.money_verbs``)."""
    return await check_reply(user_id, list(args or []))
