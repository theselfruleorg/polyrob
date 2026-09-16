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
from typing import Dict, List, Literal, Optional, Tuple


@dataclass(frozen=True)
class ChainRow:
    """Everything the system knows about one chain.

    ``None`` in a capability field means "not verified here", which callers must
    render as a refusal with a reason — never as an empty string, a zero, or
    another chain's value.
    """
    name: str
    #: ``"evm"`` or ``"svm"``. Everything else on this row is read through this:
    #: address rules, the broadcast rail, what ``chain_id`` means. Defaulting to
    #: ``"evm"`` keeps every existing row byte-identical.
    family: str = "evm"
    #: EVM ONLY: the EIP-155 chain id, pinned config and NEVER the RPC's claim.
    #: A non-EVM row carries ``0``, which is not a lie by accident — ``0`` is
    #: falsey, and ``signer.sign_transaction`` refuses to sign a transaction
    #: with no chainId, so a non-EVM row can never be signed through the EVM
    #: rail even if a caller reached it.
    chain_id: int = 0
    native_symbol: str = ""
    #: Decimals of the WRAPPED native. 18 on every EVM chain; 9 on Solana. It is
    #: a per-row field because the pin builder used to hardcode 18, which would
    #: size a wSOL amount a billion times wrong.
    native_decimals: int = 18
    public_rpc: str = ""                # fallback only; the pin always wins
    max_fee_wei_per_tx: int = 0         # per-chain: L1 gas dwarfs an L2's
    purpose: str = ""                   # guidance the agent reads to pick a chain
    usdc: Optional[str] = None
    univ3_router: Optional[str] = None
    univ3_quoter: Optional[str] = None
    wrapped_native: Optional[str] = None
    dexscreener_id: Optional[str] = None
    #: GeckoTerminal's own network slug. Deliberately a separate field from
    #: ``dexscreener_id``: the two indexers disagree about names (``eth`` vs
    #: ``ethereum``, ``polygon_pos`` vs ``polygon``), and deriving one from the
    #: other would work for Base and silently query the wrong chain elsewhere.
    #: Verified against ``/api/v2/networks``, 2026-08-24.
    geckoterminal_id: Optional[str] = None
    goplus_id: Optional[str] = None
    alchemy_slug: Optional[str] = None
    #: The block explorer's base URL, no trailing slash. `None` means no
    #: explorer is pinned for this chain, which `explorer_url` must render as a
    #: refusal (`None`) rather than guessing a host.
    explorer: Optional[str] = None
    #: The LI.FI Diamond on THIS chain — the one address a third-party route may
    #: be approved to spend from (proposal 029). Pinned per chain for the same
    #: reason the routers are: the address is deterministic across deployments
    #: TODAY, and pinning it means a repointed API cannot nominate its own
    #: spender tomorrow. `None` = no aggregator route here, which is a refusal,
    #: never a fallback to another chain's value.
    aggregator_spender: Optional[str] = None
    #: Route providers to try on this chain, IN ORDER. "univ3" builds the
    #: calldata locally from the pinned quoter/router and is always tried first
    #: where it exists; "lifi" is a third party and is consulted only when local
    #: construction finds no pool.
    route_hints: Tuple[str, ...] = ("univ3", "lifi")
    #: Whether value may MOVE on this chain. Set only for a chain whose row is
    #: fully verified; a pinned RPC alone must never arm a chain.
    money_enabled: bool = False
    #: Whether this row's TOKEN ADDRESSES were checked on-chain. It is what the
    #: canonical pin table means by "verified", and it is deliberately separate
    #: from ``money_enabled``: Solana moves value through its own rail rather
    #: than the EVM one, so it is not ``money_enabled``, yet its USDC mint is
    #: the same constant ``core/wallet/solana_x402.py`` asset-pins the live
    #: settlement rail against. Every ``money_enabled`` row is implicitly
    #: verified (arming a chain required checking its addresses first), so only
    #: a non-money row needs to set this explicitly.
    assets_verified: bool = False

    @property
    def rpc_env(self) -> str:
        return f"DEFI_EVM_RPC_{self.name.upper()}"


#: LI.FI's Diamond proxy. Deterministic across deployments, so the SAME address
#: on every chain it is pinned to below — but "the same" is a claim, so each
#: chain's row was verified independently: ``eth_getCode`` non-empty (10,354 hex
#: chars, byte-identical across all four) PLUS a functional USDC->wrapped-native
#: quote naming it as ``approvalAddress``, on ethereum / base / arbitrum /
#: polygon, 2026-08-24. Robinhood is deliberately absent: LI.FI does not index
#: it, and a chain with no verified spender must refuse, not borrow.
#:
#: ⚠️ One endpoint (rpc.ankr.com/polygon) returned EMPTY code for this address
#: while publicnode returned the full bytecode. Verify against an endpoint you
#: trust; a proxying RPC that answers "no code" would silently disarm this pin.
_LIFI_DIAMOND = "0x1231DEB6f5749EF6cE6943a275A1D3E7486F4EaE"

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
        geckoterminal_id="eth",
        goplus_id="1",
        alchemy_slug="eth-mainnet",
        explorer="https://etherscan.io",
        aggregator_spender=_LIFI_DIAMOND,
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
        geckoterminal_id="base",
        goplus_id="8453",
        alchemy_slug="base-mainnet",
        explorer="https://basescan.org",
        aggregator_spender=_LIFI_DIAMOND,
        money_enabled=True,
    ),
    # Promoted from DATA-ONLY on 2026-08-25. The old row said "no Uniswap V3
    # deployment and no independent price feed were verified here" — the first
    # half is still true and the second went stale: DexScreener, GeckoTerminal
    # AND GoPlus all index this chain now. It is also, measured that day, the #2
    # chain by paid attention on DexScreener's boost surface (13 boosted tokens
    # against Base's 0), with minutes-old pools and CASHCAT trading $35M/24h.
    #
    # Verified on-chain that day: eth_chainId == 4663, WETH symbol+decimals, the
    # aggregator spender carries code, and a live WETH->CASHCAT quote routes.
    #
    # ⚠️ THE QUOTE ASSET IS ETH/WETH, NOT USDC. The chain's stablecoin is USDG
    # (0x5fc5360d0400a0fd4f2af552add042d716f1d168, Global Dollar) — deliberately
    # NOT set as this row's `usdc`, because `tokens.py` stamps that field with
    # the literal identity {"symbol": "USDC", "name": "USD Coin"}, and putting a
    # false name on a token in the one table whose whole value is that
    # verified=True means somebody checked is worse than leaving it unpinned.
    # `_chain_quote_asset` therefore falls through to WETH here, which is the
    # right answer anyway: the pons-v2 memecoin pools quote in ETH/WETH.
    #
    # ⚠️ An earlier version of this comment said LI.FI does not index this chain
    # and that no stablecoin leg routes. BOTH went stale. Re-verified live
    # 2026-09-10: LI.FI carries chain 4663 (its key is "out"), and WETH->USDG,
    # WETH->NVDA, WETH->meme, USDG->meme and native-ETH->meme all quote, each
    # naming the aggregator spender pinned below. Native ETH in is the cheapest
    # entry and needs no allowance at all. Funding this chain with ETH therefore
    # buys gas and buying power in one asset.
    "robinhood": ChainRow(
        name="robinhood",
        chain_id=4663,
        native_symbol="ETH",                  # Arbitrum Orbit; no native token
        public_rpc="https://rpc.mainnet.chain.robinhood.com",
        # Measured 0.024 gwei: a 1.2M-gas aggregator route costs ~0.0000287 ETH,
        # so this is ~70 such routes of headroom as an anomaly brake.
        max_fee_wei_per_tx=2 * 10 ** 15,
        purpose=("Robinhood's L2 (Arbitrum Orbit, ETH gas, gas ~free) and the "
                 "primary memecoin hunting ground — minutes-old pools on the "
                 "pons-v2 launchpad, real depth on the leaders. It also hosts "
                 "TOKENIZED US EQUITIES (NVDA, AAPL, GME, SPY, SPCX) as ERC-20s, "
                 "and memecoins here pair against WETH *or against a stock "
                 "token* — so such a position is two bets stacked, the meme "
                 "against its stock and the stock itself. The stablecoin is "
                 "USDG, NOT USDC. No Uniswap V3 router is pinned, so everything "
                 "routes through the aggregator; that is normal here, not a "
                 "degraded path. Native ETH routes straight into a memecoin "
                 "with no allowance, so ETH on this chain is gas and buying "
                 "power at once."),
        # No canonical stablecoin routes on this chain; WETH is the quote asset.
        # ⚠️ CHECKSUM. This was stored mixed-case with a BROKEN EIP-55 checksum,
        # and `onchain.token_balances` refuses a failed checksum by design (a bad
        # one usually means a typo or a swapped address). Every WETH balance read
        # on this chain therefore raised, and `bridge_guard.token_balance_raw`
        # caught it and returned None -- which the bridge correctly reads as "the
        # arrival cannot be measured" and refuses. Live 2026-09-13: three
        # SOL->robinhood WETH bridges refused at the pre-send baseline with
        # "could not read 0xcAda... on robinhood", while the agent's own
        # defi_data.portfolio(robinhood) read fine (it reads NATIVE, never this
        # constant). Verified on-chain before correcting the case: code present,
        # symbol WETH, decimals 18, supply ~41,195.
        wrapped_native="0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",
        dexscreener_id="robinhood",
        geckoterminal_id="robinhood",
        goplus_id="4663",
        alchemy_slug="robinhood-mainnet",
        # ⚠️ MEASURED 2026-09-15, not assumed. The old value here
        # (explorer.mainnet.chain.robinhood.com, guessed from the RPC host)
        # is a redirector: `GET /` answers 301 to
        # https://robinhoodchain.blockscout.com/. A redirected link still
        # opens, but every address and hash the owner is shown carries the
        # extra hop, and a redirector can stop redirecting. The canonical
        # host answers 200 directly on both paths we render:
        #   /address/0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73 -> 200
        #   /token/0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73   -> 200
        explorer="https://robinhoodchain.blockscout.com",
        # NOT the Diamond every other chain uses — that address carries no code
        # here. Read from a live quote and verified with eth_getCode.
        aggregator_spender="0xB477751B76CF82d00a686A1232f5fCD772414Af3",
        route_hints=("lifi",),
        money_enabled=True,
    ),
    # Read-only rows: these exist because venues settle on them
    # (onchain.VENUE_CHAIN — hyperliquid/polymarket) and balance reads need the
    # endpoint and the USDC pin. Nothing about their money path is verified, so
    # money_enabled stays False and the trade verbs refuse them by name.
    # Armed 2026-08-25 (029 §4). Neither carries a verified Uniswap V3
    # deployment, and before the route seam that meant "no swap is possible
    # here" — the aggregator IS the route on both, which is what made arming
    # them a one-row change rather than a research task. Every address below was
    # re-verified on-chain that day: USDC symbol+decimals, wrapped-native
    # symbol+decimals, the pinned chain id against eth_chainId, and a functional
    # USDC->wrapped-native quote naming the aggregator spender.
    "arbitrum": ChainRow(
        name="arbitrum",
        chain_id=42161,
        native_symbol="ETH",
        public_rpc="https://arb1.arbitrum.io/rpc",
        # Measured 0.02 gwei; a 250k-gas tx costs ~0.000005 ETH, so this is
        # ~400 transactions of headroom as an anomaly brake.
        max_fee_wei_per_tx=2 * 10 ** 15,      # 0.002 ETH
        purpose=("Cheap L2 with deep majors liquidity, and where the Hyperliquid "
                 "venue settles. Gas is ETH and costs a fraction of a cent. No "
                 "Uniswap V3 deployment is pinned here, so swaps route through "
                 "the aggregator — which reaches Camelot, Ramses and the rest."),
        usdc="0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        wrapped_native="0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",   # WETH, verified
        geckoterminal_id="arbitrum",
        goplus_id="42161",
        alchemy_slug="arb-mainnet",
        explorer="https://arbiscan.io",
        aggregator_spender=_LIFI_DIAMOND,
        money_enabled=True,
    ),
    "polygon": ChainRow(
        name="polygon",
        chain_id=137,
        native_symbol="POL",
        public_rpc="https://polygon-rpc.com",
        # ⚠️ NOT the 0.002 default the other rows use. Polygon gas is ~276 gwei
        # (measured 2026-08-25), so a 250k-gas transaction costs ~0.069 POL —
        # the inherited 0.002 covered ZERO transactions and would have made this
        # a chain that looked armed and refused everything. 0.5 POL is ~7 such
        # transactions: still an anomaly brake, not a budget. The USD caps in
        # tx_guard/PolicyGate are what bound what a trade is worth.
        max_fee_wei_per_tx=5 * 10 ** 17,      # 0.5 POL
        purpose=("Cheap chain with a large long-tail token market, and where the "
                 "Polymarket venue settles. Gas is POL, NOT ether, and is far "
                 "pricier per unit than an L2's — budget for it. No Uniswap V3 "
                 "deployment is pinned here, so swaps route through the "
                 "aggregator (QuickSwap, SushiSwap and the rest)."),
        usdc="0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
        wrapped_native="0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",   # WPOL, verified
        geckoterminal_id="polygon_pos",
        goplus_id="137",
        alchemy_slug="polygon-mainnet",
        explorer="https://polygonscan.com",
        aggregator_spender=_LIFI_DIAMOND,
        money_enabled=True,
    ),
    # ---- non-EVM ---------------------------------------------------------
    # Solana. `money_enabled=False` and `route_hints=()` are NOT "nothing is
    # armed here" — they say value never moves through the EVM rail or the EVM
    # route providers. Solana has its own signer (`solana_signer.py`), rail
    # (`solana_rail.py`), simulation (`solana_simulation.py`) and swap verb
    # (`defi_trade.solana_swap`, Jupiter route, SOLANA_TRADE_ENABLED), and the
    # EVM money verbs refuse this chain BY NAME so the two can never be
    # confused. The 2026-08-22 crypto security audit records the parity
    # mismatches the money side had to decide.
    #
    # The READ tier is wider still: three of our providers (DexScreener,
    # GeckoTerminal, GoPlus) index Solana, and refusing to look was a blind
    # spot, not caution.
    "solana": ChainRow(
        name="solana",
        family="svm",
        # chain_id is EIP-155 and has no Solana analogue. 0 is falsey, and
        # `signer.sign_transaction` refuses a transaction with no chainId, so
        # this row cannot be signed through the EVM rail by accident.
        chain_id=0,
        native_symbol="SOL",
        # wSOL is 9 decimals, not 18. See ChainRow.native_decimals.
        native_decimals=9,
        public_rpc="https://api.mainnet-beta.solana.com",
        max_fee_wei_per_tx=0,           # not a wei-denominated chain
        purpose=("Solana. Reads (prices, screens, pool discovery, portfolio) "
                 "work here; the EVM money verbs do NOT — swaps go through the "
                 "dedicated defi_trade.solana_swap verb (Jupiter route, "
                 "simulate-and-assert guard, SOLANA_TRADE_ENABLED). Its "
                 "addresses are base58 with NO checksum, so a mistyped one is "
                 "a valid different account — verify before trusting any "
                 "address here."),
        # The circulating USDC mint — the SAME constant solana_x402.py pins the
        # live settlement rail against, which is why `assets_verified` is True.
        usdc="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        # The wSOL system mint. Jupiter wraps through it, so a SOL sell is
        # backed by native SOL plus any wrapped account. Carrying it here kills
        # the second copy that lived in tools/defi/trade_tool.py.
        wrapped_native="So11111111111111111111111111111111111111112",
        dexscreener_id="solana",
        geckoterminal_id="solana",
        goplus_id="solana",
        explorer="https://solscan.io",
        route_hints=(),
        money_enabled=False,
        assets_verified=True,
    ),
}


def get(chain: str) -> Optional[ChainRow]:
    """The row for *chain*, or None. No default row — unknown stays unknown."""
    if not isinstance(chain, str):
        return None
    return _ROWS.get(chain.strip().lower())


#: The Solana path segment for each kind — `solscan.io` names an account (never
#: "address") and a token the same way it names any other account.
_SVM_PATH = {"address": "account", "tx": "tx", "token": "token"}
#: The EVM path segment for each kind, unchanged across every EVM explorer
#: pinned above (etherscan/basescan/arbiscan/polygonscan all fork the same UI).
_EVM_PATH = {"address": "address", "tx": "tx", "token": "token"}


def explorer_url(chain: str, kind: Literal["address", "tx", "token"], ref: str) -> Optional[str]:
    """A block-explorer link for *ref*, or `None` — never raises.

    `None` on an unknown chain, a chain with no `explorer` pinned, an unknown
    `kind`, or an empty `ref`: a helper that renders a broken link is worse
    than a caller that omits the line entirely.
    """
    row = get(chain)
    if row is None or not row.explorer:
        return None
    ref = str(ref or "").strip()
    if not ref:
        return None
    path_map = _SVM_PATH if row.family == "svm" else _EVM_PATH
    segment = path_map.get(kind)
    if segment is None:
        return None
    return f"{row.explorer}/{segment}/{ref}"


def all_rows() -> List[ChainRow]:
    return list(_ROWS.values())


def rows_of_family(family: str) -> List[ChainRow]:
    """Every row in one chain family.

    Exists so an EVM-only consumer (the JSON-RPC read table, the EIP-55 token
    pins, the EVM broadcast rail) can say so explicitly instead of iterating
    every row and quietly assuming. Iterating all_rows() from EVM code is how a
    Solana mint ends up in an eth_call.
    """
    return [r for r in _ROWS.values() if r.family == family]


def evm_rows() -> List[ChainRow]:
    return rows_of_family("evm")


def names() -> List[str]:
    return list(_ROWS)


def money_chains() -> List[str]:
    """Chains where value may move. A chain joins this list only when its whole
    row is verified — never because an operator pinned an endpoint."""
    return [r.name for r in _ROWS.values() if r.money_enabled]


def swap_chains() -> List[str]:
    """Chains where a swap may both move value and find a route."""
    return [r.name for r in _ROWS.values() if r.money_enabled and swap_ready(r.name)[0]]


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
        # The tail is family-aware. "Nothing can be sent, approved or swapped"
        # is true of a row with NO rail, and false of one that simply moves
        # value somewhere else: Solana has its own signer, simulation and swap
        # verb, so the blanket wording contradicted the same message's own
        # pointer to solana_swap. A self-contradicting refusal is how the agent
        # concluded the chain was unusable and stopped reporting it at all.
        if row.family == "evm":
            tail = "Nothing can be sent, approved or swapped on it."
        else:
            tail = (f"These EVM money verbs do not reach it — use the "
                    f"{row.family}-native verb named above instead.")
        return False, f"chain {row.name!r} is read-only here: {row.purpose} {tail}"
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
    """(ok, reason) — is a swap ROUTE configured for *chain* at all?

    Route capability only. Whether value may move at all (money_enabled + a
    pinned RPC) is ``money_ready``'s question, and a caller that swaps must
    satisfy BOTH — kept apart so each refusal names one missing thing instead
    of a compound one.

    Since proposal 029 this is a REGISTRY-level question, not a Uniswap one: a
    chain is route-capable if it has a verified local DEX deployment **or** a
    pinned aggregator spender. Which providers are actually live at runtime is
    a tools-tier question (``tools.defi.providers.routes``) — ``core`` cannot
    import ``tools`` (5-tier ratchet), and it should not: a flag being off is
    not the same fact as a chain having no route.
    """
    row = get(chain)
    if row is None:
        return False, (f"chain {chain!r} is unknown — known chains: "
                       f"{', '.join(names())}")
    has_local = bool(row.univ3_router and row.univ3_quoter)
    has_aggregator = bool(row.aggregator_spender)
    if not row.route_hints or not (has_local or has_aggregator):
        return False, (f"no verified swap route on {row.name} — neither a "
                       f"Uniswap V3 deployment nor an aggregator spender is "
                       f"pinned for it, and swapping without one would send "
                       f"funds to an address that carries no code. "
                       f"{row.purpose}")
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
