"""Uniswap V3 routing — quotes and swap calldata, keyless, per chain.

Proposal 023 T4 named 0x as the EVM route. 0x API v2 now requires an API key,
and prod holds none, so this reads the chain directly instead: ``QuoterV2`` for
the quote and ``SwapRouter02`` for execution. That is strictly better for our
purposes — the quote comes from the same contracts that will execute it, with
no third party able to return a number the pools do not support.

Trust semantics follow ``providers/base.py``:

  * a quote FAILS OPEN to ``None`` — "no route" renders as unknown, never as a
    zero-output trade;
  * the caller must still bound the result. A quote is what the pool says right
    now; it is not a promise, which is why ``amount_out_min`` (slippage) and the
    DexScreener cross-check live at the call site, not here.

⚠️ Addresses are PER CHAIN and come from ``core.wallet.chains`` — Uniswap V3 is
deployed at different addresses on Ethereum than on Base. A wrong router address
does not error; it sends funds nowhere. Every address in the registry was
verified with ``eth_getCode`` plus a functional quote before being pinned, and a
chain with no verified deployment quotes ``None`` (no route) rather than
borrowing another chain's address.
"""
from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

# Verified on Base mainnet (eth_getCode non-empty), 2026-08-14. These remain as
# the BASE constants; every chain's live addresses come from the registry via
# `addresses_for()` — Ethereum's Uniswap V3 deployment is at entirely different
# addresses, and calling Base's on Ethereum reads (or pays) empty space.
QUOTER_V2 = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
SWAP_ROUTER_02 = "0x2626664c2603336E57B271c5C0b26F421741e481"
WETH_BASE = "0x4200000000000000000000000000000000000006"


def addresses_for(chain: str):
    """(quoter, router) for *chain*, or (None, None) when none is verified.

    ``None`` is the honest answer for a chain with no verified deployment
    (e.g. Robinhood): the caller must render "no route", never fall back to
    another chain's address.
    """
    from core.wallet import chains
    row = chains.get(chain)
    if row is None:
        return None, None
    return row.univ3_quoter, row.univ3_router

#: The standard V3 fee tiers, in bps*100. Tried in full because a memecoin's
#: only pool is frequently the 1% tier, while majors sit at 0.01–0.05%.
FEE_TIERS: Sequence[int] = (100, 500, 3000, 10000)

#: Fallback endpoints only. The operator-pinned DEFI_EVM_RPC_<CHAIN> always
#: wins (resolved via core.wallet.onchain.rpc_url_for_chain): the quote from
#: this module anchors amountOutMinimum — the swap's only on-chain price
#: protection — so it must come from the SAME trust anchor the guard
#: simulates against, not from the shared public endpoint a money path
#: already refuses to arm on.
_RPC_URLS = {"base": "https://mainnet.base.org"}

# keccak("quoteExactInputSingle((address,address,uint256,uint24,uint160))")[:4]
_QUOTE_SELECTOR = "0xc6a5026a"
# keccak("exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))")[:4]
_EXACT_INPUT_SINGLE_SELECTOR = "0x04e45aaf"
# keccak("approve(address,uint256)")[:4]
_APPROVE_SELECTOR = "0x095ea7b3"


@dataclass(frozen=True)
class SwapQuote:
    """One routable quote. ``quoted_at`` exists so the caller can enforce a
    freshness window — an old quote is a stale price, and executing against it
    is how a swap silently eats slippage."""
    chain: str
    token_in: str
    token_out: str
    amount_in_raw: int
    amount_out_raw: int
    fee_tier: int
    router: str
    quoted_at: float


def _rpc(chain: str, method: str, params: list, timeout: float = 15.0):
    from core.wallet.onchain import rpc_url_for_chain
    url = rpc_url_for_chain(chain) or _RPC_URLS.get(chain)
    if not url:
        raise ValueError(f"no RPC configured for chain {chain!r}")
    req = urllib.request.Request(
        url,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                         "params": params}).encode(),
        # The public endpoint 403s a default urllib agent.
        headers={"content-type": "application/json", "user-agent": "polyrob/defi"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def _addr(a: str) -> str:
    return a.lower().replace("0x", "").rjust(64, "0")


def _uint(n: int) -> str:
    return f"{int(n):064x}"


def quote_single(chain: str, token_in: str, token_out: str, amount_in_raw: int,
                 fee: int, *, rpc: Optional[Callable] = None) -> Optional[int]:
    """Output amount for ONE fee tier, or None when that pool cannot route it."""
    quoter, _router = addresses_for(chain)
    if not quoter:
        return None          # no verified deployment on this chain => no route
    call = rpc or (lambda m, p: _rpc(chain, m, p))
    data = ("0x" + _QUOTE_SELECTOR[2:] + _addr(token_in) + _addr(token_out)
            + _uint(amount_in_raw) + _uint(fee) + _uint(0))
    try:
        res = call("eth_call", [{"to": quoter, "data": data}, "latest"])
    except Exception:
        return None
    raw = (res or {}).get("result")
    if not raw or len(raw) < 66:
        return None          # revert => no pool at this tier
    try:
        out = int(raw[2:66], 16)
    except ValueError:
        return None
    return out or None


def best_quote(chain: str, token_in: str, token_out: str, amount_in_raw: int,
               *, rpc: Optional[Callable] = None) -> Optional[SwapQuote]:
    """The best-output tier across ``FEE_TIERS``, or None when nothing routes.

    Fails OPEN to None: no route is "unknown", never a zero-output swap.
    """
    _quoter, router = addresses_for(chain)
    if not router:
        return None          # no verified deployment on this chain => no route
    best_fee, best_out = None, 0
    for fee in FEE_TIERS:
        out = quote_single(chain, token_in, token_out, amount_in_raw, fee, rpc=rpc)
        if out and out > best_out:
            best_fee, best_out = fee, out
    if best_fee is None:
        return None
    return SwapQuote(chain=chain, token_in=token_in, token_out=token_out,
                     amount_in_raw=amount_in_raw, amount_out_raw=best_out,
                     fee_tier=best_fee, router=router,
                     quoted_at=time.time())


def build_exact_input_single_data(*, token_in: str, token_out: str, fee: int,
                                  recipient: str, amount_in_raw: int,
                                  amount_out_min_raw: int) -> str:
    """Calldata for ``SwapRouter02.exactInputSingle``.

    NOTE: SwapRouter02's struct has NO deadline field (SwapRouter01 did).
    Adding one shifts every subsequent word and the call reverts.
    """
    return ("0x" + _EXACT_INPUT_SINGLE_SELECTOR[2:]
            + _addr(token_in) + _addr(token_out) + _uint(fee) + _addr(recipient)
            + _uint(amount_in_raw) + _uint(amount_out_min_raw) + _uint(0))


def build_approve_data(*, spender: str, amount_raw: int) -> str:
    """Calldata for ``ERC20.approve``. The caller supplies an EXACT amount —
    this module never encodes an unlimited approval."""
    return "0x" + _APPROVE_SELECTOR[2:] + _addr(spender) + _uint(amount_raw)


def read_allowance(chain: str, token: str, owner: str, spender: str,
                   *, rpc: Optional[Callable] = None) -> Optional[int]:
    """Current allowance, or None when it cannot be read (fails OPEN: unknown)."""
    call = rpc or (lambda m, p: _rpc(chain, m, p))
    # keccak("allowance(address,address)")[:4] = 0xdd62ed3e
    data = "0xdd62ed3e" + _addr(owner) + _addr(spender)
    try:
        res = call("eth_call", [{"to": token, "data": data}, "latest"])
        raw = (res or {}).get("result")
        return int(raw, 16) if raw and len(raw) > 2 else None
    except Exception:
        return None
