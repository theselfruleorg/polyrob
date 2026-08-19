"""THE chain registry — one row per chain, read by every chain-aware seam.

Before this module, "base" was written into the broadcast rail, the token pins,
the Uniswap addresses, three off-chain providers and the trade tool's door, each
with its own idea of what a chain is. Adding a second chain meant finding all of
them, and missing one does not error — a Base router address used on Ethereum
carries no code there, so the swap sends funds nowhere.

Three rules this table exists to enforce:

* **A chain's values come from its own row.** There is no default row and no
  fallback: an unknown chain resolves to ``None`` and the caller must refuse.
* **A missing capability degrades honestly.** ``swap_ready``/``money_ready``
  return ``(False, reason)`` naming what is missing for THAT chain. A refusal
  never points at another chain's value.
* **Chain ids are configuration.** They are pinned here and never read from an
  RPC — the same EOA exists on every EVM chain, so letting a repointed endpoint
  choose the id is how a transaction lands somewhere real by accident.

⚠️ Every address below was verified on-chain (``eth_getCode`` non-empty, plus
``symbol``/``decimals`` reads for tokens, plus a functional quote for each
quoter) on 2026-08-17. Never copy an address here from a document or from
memory — verify it against the chain first.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class ChainRow:
    """Everything the system knows about one chain.

    ``None`` in a capability field means "not verified here", which callers must
    render as a refusal with a reason — never as an empty string, a zero, or
    another chain's value.
    """
    name: str
    chain_id: int                       # pinned config, NEVER the RPC's claim
    native_symbol: str
    public_rpc: str                     # fallback only; the pin always wins
    max_fee_wei_per_tx: int             # per-chain: L1 gas dwarfs an L2's
    purpose: str                        # guidance the agent reads to pick a chain
    usdc: Optional[str] = None
    univ3_router: Optional[str] = None
    univ3_quoter: Optional[str] = None
    wrapped_native: Optional[str] = None
    dexscreener_id: Optional[str] = None
    goplus_id: Optional[str] = None
    alchemy_slug: Optional[str] = None
    #: Whether value may MOVE on this chain. Set only for a chain whose row is
    #: fully verified; a pinned RPC alone must never arm a chain.
    money_enabled: bool = False

    @property
    def rpc_env(self) -> str:
        return f"DEFI_EVM_RPC_{self.name.upper()}"


_ROWS: Dict[str, ChainRow] = {
    "ethereum": ChainRow(
        name="ethereum",
        chain_id=1,
        native_symbol="ETH",
        public_rpc="https://ethereum-rpc.publicnode.com",
        # L1 fees swing by an order of magnitude with the fee market. This is an
        # anomaly brake (a bad RPC or a fee spike), not an economic budget — the
        # USD caps in tx_guard/PolicyGate are what bound what a trade is worth.
        max_fee_wei_per_tx=10 ** 16,          # 0.01 ETH
        purpose=("Deepest liquidity and the canonical home of most ERC-20s, but "
                 "the most expensive gas by far — an approve+swap+revoke cycle "
                 "can cost more than a small trade is worth. Prefer it for size, "
                 "for tokens that only exist here, or when depth matters more "
                 "than fees."),
        usdc="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        univ3_router="0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",   # SwapRouter02
        univ3_quoter="0x61fFE014bA17989E743c5F6cB21bF9697530B21e",   # QuoterV2
        wrapped_native="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",  # WETH9
        dexscreener_id="ethereum",
        goplus_id="1",
        alchemy_slug="eth-mainnet",
        money_enabled=True,
    ),
    "base": ChainRow(
        name="base",
        chain_id=8453,
        native_symbol="ETH",
        public_rpc="https://mainnet.base.org",
        max_fee_wei_per_tx=2 * 10 ** 15,      # 0.002 ETH — a cheap L2
        purpose=("Cheap, fast L2 and the agent's own treasury chain: the USDC "
                 "the wallet holds and the x402 payment rail both live here. "
                 "The default choice for small trades, because gas is a "
                 "fraction of a cent."),
        usdc="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        univ3_router="0x2626664c2603336E57B271c5C0b26F421741e481",
        univ3_quoter="0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a",
        wrapped_native="0x4200000000000000000000000000000000000006",
        dexscreener_id="base",
        goplus_id="8453",
        alchemy_slug="base-mainnet",
        money_enabled=True,
    ),
    "robinhood": ChainRow(
        name="robinhood",
        chain_id=4663,
        native_symbol="ETH",                  # Arbitrum Orbit; no native token
        public_rpc="https://rpc.mainnet.chain.robinhood.com",
        max_fee_wei_per_tx=2 * 10 ** 15,
        purpose=("Robinhood's tokenized-equity L2 (Arbitrum Orbit, ETH gas). "
                 "Readable for balances and contract state, but no Uniswap V3 "
                 "deployment and no independent price feed were verified here, "
                 "so it is DATA-ONLY: reads work, value cannot move."),
        # No canonical USDC, DEX, price feed or screen verified on this chain.
        alchemy_slug="robinhood-mainnet",
        money_enabled=False,
    ),
    # Read-only rows: these exist because venues settle on them
    # (onchain.VENUE_CHAIN — hyperliquid/polymarket) and balance reads need the
    # endpoint and the USDC pin. Nothing about their money path is verified, so
    # money_enabled stays False and the trade verbs refuse them by name.
    "arbitrum": ChainRow(
        name="arbitrum",
        chain_id=42161,
        native_symbol="ETH",
        public_rpc="https://arb1.arbitrum.io/rpc",
        max_fee_wei_per_tx=2 * 10 ** 15,
        purpose=("Where the Hyperliquid venue settles. Balance reads only — no "
                 "swap route is verified here."),
        usdc="0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        goplus_id="42161",
        alchemy_slug="arb-mainnet",
        money_enabled=False,
    ),
    "polygon": ChainRow(
        name="polygon",
        chain_id=137,
        native_symbol="POL",
        public_rpc="https://polygon-rpc.com",
        max_fee_wei_per_tx=2 * 10 ** 15,
        purpose=("Where the Polymarket venue settles. Balance reads only — no "
                 "swap route is verified here."),
        usdc="0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
        goplus_id="137",
        alchemy_slug="polygon-mainnet",
        money_enabled=False,
    ),
}


def get(chain: str) -> Optional[ChainRow]:
    """The row for *chain*, or None. No default row — unknown stays unknown."""
    if not isinstance(chain, str):
        return None
    return _ROWS.get(chain.strip().lower())


def all_rows() -> List[ChainRow]:
    return list(_ROWS.values())


def names() -> List[str]:
    return list(_ROWS)


def money_chains() -> List[str]:
    """Chains where value may move. A chain joins this list only when its whole
    row is verified — never because an operator pinned an endpoint."""
    return [r.name for r in _ROWS.values() if r.money_enabled]


def swap_chains() -> List[str]:
    return [r.name for r in _ROWS.values() if r.money_enabled and r.univ3_router]


def rpc_is_pinned(chain: str) -> bool:
    row = get(chain)
    env = row.rpc_env if row else f"DEFI_EVM_RPC_{str(chain).upper()}"
    return bool(os.getenv(env, "").strip())


def money_capable(chain: str) -> Tuple[bool, str]:
    """(ok, reason) — is *chain* one where value may move at all?

    CAPABILITY only: is the chain known, and is its row verified? Whether the
    operator has pinned a trustworthy endpoint is a separate question
    (``money_ready``), deliberately left to ``tx_guard``, which fails closed on
    it for every path — checking it here as well would duplicate a policy in two
    layers and hide the guard's own refusal behind an earlier, thinner one.
    """
    row = get(chain)
    if row is None:
        return False, (f"chain {chain!r} is unknown — known chains: "
                       f"{', '.join(names())}")
    if not row.money_enabled:
        return False, (f"chain {row.name!r} is read-only here: {row.purpose} "
                       f"Nothing can be sent, approved or swapped on it.")
    return True, ""


def money_ready(chain: str) -> Tuple[bool, str]:
    """(ok, reason) — capability AND a pinned endpoint.

    The full precondition, for callers that want to know before starting
    (diagnostics, the live-gate script). Refusals name what is missing FOR THIS
    CHAIN and, where the operator can fix it, the exact environment variable.
    """
    ok, why = money_capable(chain)
    if not ok:
        return False, why
    row = get(chain)
    if not rpc_is_pinned(row.name):
        return False, (f"no pinned RPC for {row.name}. The simulation, the "
                       f"deltas and the caps all read from it, so the shared "
                       f"public endpoint cannot be the trust anchor for moving "
                       f"funds — set {row.rpc_env}")
    return True, ""


def swap_ready(chain: str) -> Tuple[bool, str]:
    """(ok, reason) — is there a verified swap ROUTE on *chain*?

    Route capability only. Whether value may move at all (money_enabled + a
    pinned RPC) is ``money_ready``'s question, and a caller that swaps must
    satisfy BOTH — kept apart so each refusal names one missing thing instead
    of a compound one.
    """
    row = get(chain)
    if row is None:
        return False, (f"chain {chain!r} is unknown — known chains: "
                       f"{', '.join(names())}")
    if not (row.univ3_router and row.univ3_quoter):
        return False, (f"no verified Uniswap V3 deployment on {row.name} — "
                       f"swapping there would send funds to an address that "
                       f"carries no code. {row.purpose}")
    return True, ""


def chain_guidance() -> str:
    """One line per chain, for a tool description the agent reads when it picks.

    The owner's directive (2026-08-15) is that the agent chooses the chain per
    task rather than inheriting a fixed default, which only works if the trade
    -offs are in front of it at the point of choice.
    """
    lines = []
    for row in _ROWS.values():
        caps = []
        if row.money_enabled:
            caps.append("swaps" if row.univ3_router else "transfers")
        else:
            caps.append("data only")
        lines.append(f"{row.name} (id {row.chain_id}, gas {row.native_symbol}, "
                     f"{'/'.join(caps)}): {row.purpose}")
    return "\n".join(lines)
