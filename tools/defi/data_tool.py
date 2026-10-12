"""defi_data — read-only token sight on Base (proposal 023 T0+T1).

No signer is constructed and nothing is broadcast from this module.

**A token is named by (chain, contract_address), never by ticker.** `token_info`,
`price` and `contract_read` take an address only. `token_resolve` is the single
verb that accepts a ticker, and it is a discovery aid rather than a resolver: it
returns ranked candidates and refuses to pick, because binding a symbol to a
contract is the primary injection surface for an agent that reads web pages
(proposal 023 §2.3.4). Ranking is a display convenience — liquidity is
purchasable, so a typosquat with a seeded pool can outrank the genuine token.

Every result from this tool is untrusted-wrapped at the result→ToolMessage seam
(`defi_data` is in UNTRUSTED_TOOL_NAMESPACES): token `name`/`symbol` are
attacker-authored strings and must never read as instructions.

Layout (W9 split): this class keeps the verbs (each `@BaseTool.action` method)
and the shared seams. The parameter models live in `data_params.py` and the
text renderers in `data_render.py` (both re-exported here); the read groups are
mixins — holdings (`data_holdings`), reconcile (`data_reconcile`), LP
(`data_lp`), NFT and contract reads (`data_chain_reads`), the swap quote
(`data_quote`). A mixin imports this module's helpers at CALL time, so a test
that patches `tools.defi.data_tool._pinned_addresses` still reaches it.
"""
from __future__ import annotations  # safe: @BaseTool.action uses explicit param_model

import asyncio
import logging
import time  # noqa: F401 — tests patch data_tool.time.monotonic
import types
from typing import Any, Callable, Dict, List, Optional, Tuple

from tools.base_tool import BaseTool
from tools.defi import wallet_intel as _wallet_intel
from tools.defi.data_chain_reads import ChainReadsMixin
from tools.defi.data_holdings import HoldingsReadsMixin
from tools.defi.data_lp import LpReadsMixin
from tools.defi.data_quote import (  # noqa: F401 — re-exported (tests)
    QUOTE_SUSPECT_RATIO, QuoteCrossCheck, QuoteReadsMixin, _usable_price, quote_cross_check)
from tools.defi.data_reconcile import ReconcileReadsMixin
from tools.defi.providers import alchemy_index, dexscreener, goplus  # noqa: F401
from tools.defi.providers.base import Candidate, ScreenVerdict
from tools.defi.token_screen import quoted
from tools.defi.data_params import (  # noqa: F401 — re-exported (tests, packs)
    _chain_field, ResolveParams, SwapQuoteParams, TokenRefParams, PortfolioParams,
    WalletHoldingsParams, ReconcileParams, PositionsParams, DiscoverParams,
    OhlcvParams, ScanParams, ContractReadParams, NftHoldingsParams, NftInfoParams,
    LpPositionsParams, LpPoolInfoParams, PoolMetricsParams, LpQuoteParams)
from tools.defi.data_render import (  # noqa: F401 — re-exported (tests, verbs)
    _RESOLVE_ROWS_SHOWN, _BUDGET_ROWS_SHOWN, _budget_exhausted_lines,
    _VALUED_ROWS_SHOWN, _REST_ROWS_LISTED, _valued_lines, _total_line, _dust_header,
    _holdings_rest_lines, _excluded_value_text, _is_finite, _fmt_usd, _fmt_pct,
    _fmt_amount, _fmt_count, _fmt_ratio, _fmt_hours, _LP_MAX_RAW, _LP_V4_NOT_YET,
    _lp_read_error_text, _lp_number_text, _lp_amount_text, _lp_invert_if_flipped,
    _render_lp_position, _fmt_trade_shape, _SCAN_RESTATEMENT, _usd0, _fmt_scan_numbers,
    _SCAN_ORDER, _render_scan, _ticker_collision_lines, _price_line, _identity_lines,
    _curve_price_lines, _holders_line, _display_confidence, _quote_source_lines,
    _quote_metadata, _holding_md, _holdings_metadata, _render_candles,
    _candle_shape_note, _fmt_price, _fmt_signed_pct, _render_holder_rows,
    _render_holders, _render_creator)

logger = logging.getLogger(__name__)

_NULL_CONFIG = types.SimpleNamespace()

#: Bounds for `contract_read`, deliberately module constants rather than flags —
#: no operator should need to tune them, and a flag would be one more thing to
#: document and catalog.
CONTRACT_READ_GAS_CAP = 2_000_000
CONTRACT_READ_MAX_BYTES = 8192

def _supported_chains():
    """Every chain the registry knows.

    The READ tier is deliberately wider than the money tier: reading a balance
    or a price on a chain is safe, and refusing to look is not caution, it is a
    blind spot. Whether value may MOVE there is a separate, stricter question
    (`core.wallet.chains.money_capable`, enforced by defi_trade).
    """
    from core.wallet import chains
    return tuple(chains.names())


SUPPORTED_CHAINS = _supported_chains()


def _operator_read_refusal(execution_context) -> Optional[str]:
    """CR-M07: defaulting a read to the OPERATOR wallet (its holdings, its
    positions, its book) is an owner read. ONE seam —
    ``core.wallet.authority.turn_refusal`` — and a probe error fails CLOSED.
    A read of an explicitly supplied address is public chain data and is not
    gated here."""
    try:
        from core.wallet.authority import turn_refusal
        refusal = turn_refusal(execution_context)
    except Exception as exc:
        refusal = f"owner probe failed ({type(exc).__name__}); failing closed"
    if refusal:
        return (f"refused: {refusal} — this agent's own wallet is the owner's "
                f"to read. Another wallet's public holdings are readable with "
                f"defi_data.wallet_holdings(address=..., chain=...).")
    return None


def _default_json_fetch(url: str):
    """One bounded JSON GET for the NFT enumeration read.

    Kept tiny and local: the only caller is an allow-listed indexer host built
    in `nft_verbs`, never a model-supplied URL, so this is not a fetch surface
    that needs the SSRF validator `web_fetch` carries.
    """
    import json
    import urllib.request
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "polyrob-nft"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


#: Wall-clock ceiling on the PRICING half of a portfolio read, in seconds.
#: 0 or negative disables it and restores the pre-2026-09-23 unbounded behaviour.
#: ⚠️ MUST stay below the controller's per-action timeout (`TOOL_TIMEOUTS['default']`
#: = 60 s), with room left to RENDER. It was 120 s until 2026-09-24, which meant
#: the budget could never produce its partial answer: measured on prod, a
#: `portfolio` read was cut by `asyncio.wait_for` at exactly 60.0 s and the caller
#: got nothing — the outcome this budget exists to prevent.
_PRICE_BUDGET_DEFAULT_SEC = 40.0


def _pricing_budget_sec() -> float:
    from core.env import float_env
    return float_env("DEFI_PORTFOLIO_PRICE_BUDGET_SEC", _PRICE_BUDGET_DEFAULT_SEC)


def _budget_spent(started: float, now: float) -> bool:
    """Has the pricing budget elapsed?

    ⚠️ Why this exists (2026-09-23). Every network call under `portfolio` is
    already bounded — `alchemy_index.fetch_balances` 10 s, the chain RPC 8 s,
    `geckoterminal.TIMEOUT_SEC` 12 s. What was unbounded was the LOOP: one price
    lookup PER HOLDING, over a wallet carrying 114 ledger rows and a 40+ address
    dust cohort. A few dozen slow-but-legal lookups reach the agent's 600 s stall
    limit without any single call misbehaving, and the SAFETY monitor died that
    way twice (18:20 and 20:20), writing nothing either time.
    """
    budget = _pricing_budget_sec()
    if budget <= 0:
        return False
    return (now - started) > budget


def _valuation_priority(chain: str, addr: str, pinned: set) -> int:
    """0 for a canonical, owner-pinned or own-launch token, 1 for everything else.

    The portfolio loop is bounded by a time budget, and a sorted-by-address walk
    let the budget run out BEFORE the wallet's own buyback token (2026-09-29:
    ``0xbBa6…`` rendered as 27 digits of raw units with no decimals). The tokens
    the instance deliberately holds are valued first; dust takes what is left.
    """
    from core.wallet.tokens import canonical_token
    if canonical_token(chain, addr) is not None:
        return 0
    return 0 if addr.lower() in pinned else 1


#: Parallel per-row valuations in ``reconcile`` (identity eth_calls + a quote).
_RECONCILE_WORKERS = 8

#: (chain, address) -> (raw_units, ChainHolding, monotonic stamp) for holdings
#: last valued as dust or unsolicited. 2026-10-08 16:00Z: re-reading ~110
#: airdrops on every call passed the 60 s action budget under slow DNS and
#: stopped the buyback at its gate. An unrecorded buy changes the raw balance,
#: so only an UNCHANGED balance reuses the verdict; booked/pinned rows and
#: failed or priced reads are never memoised.
_DUST_MEMO: Dict[Tuple[str, str], Tuple[int, Any, float]] = {}
_DUST_MEMO_TTL_S = 6 * 3600


def _pinned_addresses(chain: str) -> set:
    """Owner-pinned tokens and the instance's OWN launches on *chain*, lower-cased.

    Own launches count: the prod buyback token is not owner-pinned, it is proven
    ours by the launch record (`token_provenance`). Empty on any read failure —
    this orders the valuation loop and gates the indicative figure, it never
    grants trust to spend."""
    out = set()
    try:
        from core.wallet.token_pins import all_pins
        out |= {str(r["address"]).lower() for r in all_pins(chain=chain)}
    except Exception:
        pass
    try:
        from core.wallet.token_provenance import all_own_tokens
        out |= {str(r["address"]).lower() for r in all_own_tokens()
                if str(r.get("chain", "")).lower() == str(chain).lower()}
    except Exception:
        pass
    return out


def _quote_slippage_bps() -> int:
    """The slippage a QUOTE is checked against.

    A read-only quote must use the same bound `swap` will, or an aggregator
    route whose encoded minimum is too loose would quote cleanly here and then
    be refused at execution — the agent would see a price it can never get.
    """
    try:
        from tools.defi.trade_tool import _max_slippage_bps
        return _max_slippage_bps()
    except Exception:
        return 100


class DefiDataTool(QuoteReadsMixin, ChainReadsMixin, LpReadsMixin, ReconcileReadsMixin,
                   HoldingsReadsMixin, BaseTool):
    """Read-only token sight. Seams are injectable so unit tests never hit the network."""

    def __init__(self, name: str = "defi_data", config=None, container=None, *,
                 search_fn: Optional[Callable] = None,
                 search2_fn: Optional[Callable] = None,
                 price_fn: Optional[Callable] = None,
                 screen_fn: Optional[Callable] = None,
                 holders_fn: Optional[Callable] = None,
                 facts_fn: Optional[Callable] = None,
                 ohlcv_fn: Optional[Callable] = None,
                 pool_for_token_fn: Optional[Callable] = None,
                 balances_fn: Optional[Callable] = None,
                 index_fn: Optional[Callable] = None,
                 identity_fn: Optional[Callable] = None,
                 call_fn: Optional[Callable] = None,
                 nft_fetch: Optional[Callable] = None,
                 native_fn: Optional[Callable] = None,
                 discover_fn: Optional[Callable] = None,
                 route_fn: Optional[Callable] = None,
                 lp_rpc: Optional[Callable] = None,
                 kind_fn: Optional[Callable] = None,
                 solana_holdings_fn: Optional[Callable] = None,
                 holder: Optional[str] = "__from_wallet__"):
        super().__init__(name=name, config=config if config is not None else _NULL_CONFIG,
                         container=container)
        # 046 §4.4: the price behind a non-stable payable asset is a DeFi read,
        # so this tool's presence is enough to supply it. Idempotent and
        # fail-open, and `X402InvoiceTool` registers the same helper — either
        # tier-legal entry point suffices, neither can drift from the other.
        if container is not None:
            from tools.defi.payment_quote import register_payment_quoter
            register_payment_quoter(container)
        self._search_fn = search_fn
        self._search2_fn = search2_fn
        self._price_fn = price_fn
        self._screen_fn = screen_fn
        self._holders_fn = holders_fn
        #: 071 W2: `facts(chain, address) -> [SourceReport]` — every screen
        #: source beyond GoPlus (chain RPC, Jupiter, RugCheck, Honeypot.is).
        self._facts_fn = facts_fn
        self._ohlcv_fn = ohlcv_fn
        self._pool_for_token_fn = pool_for_token_fn
        self._balances_fn = balances_fn
        self._index_fn = index_fn
        self._identity_fn = identity_fn
        self._call_fn = call_fn
        #: HTTP fetcher for the NFT enumeration read (an indexer — a plain RPC
        #: node cannot answer "everything this address holds"). Injected in
        #: tests; the default does one bounded JSON GET.
        self._nft_fetch = nft_fetch if nft_fetch is not None else _default_json_fetch
        self._native_fn = native_fn
        self._discover_fn = discover_fn
        self._route_fn = route_fn
        #: Injectable `rpc(method, params, timeout=8.0)` for the liquidity-pool
        #: reads (`lp_positions`/`lp_pool_info`/`lp_quote`, proposal 048 P12).
        #: Tests inject a fake; production builds one per chain in
        #: `_lp_rpc_for`.
        self._lp_rpc = lp_rpc
        #: `classify(chain, address) -> {"kind": ...} | None` (071 §3.5) and the
        #: Solana display enumeration (`token_holdings`). Injected in tests.
        self._kind_fn = kind_fn
        self._solana_holdings_fn = solana_holdings_fn
        self._holder = holder

    # -- seams ------------------------------------------------------------
    #: The resolver's indexes, in the order they are asked. ONE index is not
    #: enough: DexScreener lags a fresh launch by hours, so prod found the
    #: resolver "only knows wrong-chain or established tokens" — precisely the
    #: tokens a launch hunt is not looking for.
    _INDEX_NAMES = ("dexscreener", "geckoterminal")

    def _search(self, symbol: str) -> List[Candidate]:
        return (self._search_fn or dexscreener.search)(symbol)

    def _search2(self, symbol: str) -> List[Candidate]:
        from tools.defi.providers import geckoterminal as _gt
        return (self._search2_fn or _gt.search_candidates)(symbol)

    def _search_all(self, symbol: str):
        """Both indexes, merged and deduped by (chain, address).

        Returns ``(candidates, answered, failed)``. A failing index is NAMED,
        never swallowed: "no candidates" and "one of the two indexes was down"
        are different answers and only one of them means the token is not there.
        On a duplicate the row with the larger reported liquidity wins, because
        the thinner figure is usually the index that has not caught up.
        """
        merged, answered, failed = {}, [], []
        for name, fn in (("dexscreener", self._search),
                         ("geckoterminal", self._search2)):
            try:
                rows = fn(symbol) or []
            except Exception as exc:
                logger.debug("token_resolve: %s failed", name, exc_info=True)
                failed.append(f"{name} ({exc.__class__.__name__})")
                continue
            answered.append(name)
            for cand in rows:
                key = (cand.chain, (cand.address or "").lower())
                seen = merged.get(key)
                if seen is None or (cand.liquidity_usd or 0.0) > (seen.liquidity_usd or 0.0):
                    merged[key] = cand
        out = sorted(merged.values(), key=lambda c: -(c.liquidity_usd or 0.0))
        return out, answered, failed

    @staticmethod
    def _evm_only(chain: str, verb: str) -> Optional[str]:
        """071 T3/R7: an EVM-only verb on a non-EVM chain says so by name,
        instead of failing later with an RPC error the model misreads."""
        from core.wallet import chains
        row = chains.get(chain)
        if row is not None and row.family != "evm":
            return (f"{verb} is EVM-only; chain {chain!r} is {row.family}. For a "
                    f"{chain} token use token_info / price / token_holders; for a "
                    f"{chain} wallet use wallet_holdings.")
        return None

    def _svm_identity_fill(self, chain: str, address: str, ident):
        """071: an unpinned Solana mint used to read "symbol None / decimals
        unknown" in token_info although the chain knows its decimals. Decimals
        come from the MINT ACCOUNT (a chain fact); symbol and name from Jupiter
        Tokens v2 and are labelled self-reported. `verified` is never raised —
        only a pin or the canonical list does that. token_info only: the
        holdings loops keep the cheap identity (their decimals come from the
        token accounts already)."""
        from core.wallet import chains
        row = chains.get(chain)
        if row is None or row.family != "svm":
            return ident
        decimals = symbol = name = None
        try:
            from core.wallet import spl_facts
            decimals = spl_facts.read_mint(address).decimals
        except Exception:
            logger.debug("mint decimals unreadable for %s", address, exc_info=True)
        try:
            from tools.defi.providers import token_audits
            jup = token_audits.jupiter_token(address)
            symbol = getattr(jup, "symbol", None)
            name = getattr(jup, "name", None)
        except Exception:
            logger.debug("jupiter identity unreadable for %s", address, exc_info=True)
        if decimals is None and symbol is None and name is None:
            return ident
        return types.SimpleNamespace(
            symbol=getattr(ident, "symbol", None) or symbol,
            name=getattr(ident, "name", None) or name,
            decimals=decimals,
            verified=getattr(ident, "verified", False),
            metadata_changed=getattr(ident, "metadata_changed", False),
            source=getattr(ident, "source", None) or "chain+jupiter (self-reported symbol)")

    def _address_kind(self, chain: str, address: str):
        """What the address IS, or ``None`` when that could not be read.
        ``None`` never refuses anything — callers proceed as before."""
        try:
            from core.wallet import address_kind
            return (self._kind_fn or address_kind.classify)(chain, address)
        except Exception:
            logger.debug("address kind unreadable for %s on %s", address, chain, exc_info=True)
            return None

    def _not_a_token(self, chain: str, address: str) -> Optional[str]:
        from core.wallet.address_kind import wrong_kind_for_token
        return wrong_kind_for_token(chain, address, self._address_kind(chain, address))

    def _price_for(self, chain: str, address: str):
        """The agreed quote (071: ``tools.intel.price`` over every source), or
        the injected seam's answer."""
        if self._price_fn:
            return self._price_fn(chain, address)
        from tools.defi import price_sources
        return price_sources.quote(chain, address)

    def _row_price(self, chain: str, address: str, prefetch, *, ours: bool):
        """A holdings row's quote. With the loop's batch answers a row costs at
        most one primary call; without them (a seam is injected) it is
        exactly ``_price_for``."""
        if prefetch is None:
            return self._price_for(chain, address)
        from tools.defi import price_sources
        return price_sources.quote_prefetched(chain, address, prefetch,
                                              must_ask_primary=ours)

    def _prefetch_prices(self, chain: str, addresses):
        """Batch answers for a holdings loop, or None when a price seam is
        injected (tests stub ``price_fn`` or ``_price_for``) or the batch could
        not be built."""
        if self._price_fn or "_price_for" in self.__dict__:
            return None
        try:
            from tools.defi import price_sources
            return price_sources.prefetch(chain, list(addresses))
        except Exception:
            logger.debug("price prefetch failed for %s", chain, exc_info=True)
            return None

    def _screen_for(self, chain: str, address: str) -> ScreenVerdict:
        return (self._screen_fn or goplus.screen)(chain, address)

    def _holders_for(self, chain: str, address: str):
        if self._holders_fn:
            return self._holders_fn(chain, address)
        from core.wallet import chains
        row = chains.get(chain)
        if row is not None and row.family == "svm":
            # GoPlus Solana carries authorities, not holder rows; the chain's
            # own largest-accounts read does (071 W2).
            from tools.defi import token_screen
            return token_screen.svm_holders(address)
        return goplus.holders(chain, address)

    def _merged_screen(self, chain: str, address: str, verdict):
        """GoPlus's verdict merged with every other source (071 W2). A source
        that raises is NOT CHECKED by name — never dropped, never a pass."""
        from core.wallet import chains
        from tools.defi import token_screen
        row = chains.get(chain)
        family = row.family if row is not None else "evm"
        try:
            facts = (self._facts_fn or token_screen.gather_facts)(chain, address)
        except Exception as exc:
            facts = [token_screen.failed_source("facts", exc.__class__.__name__, [])]
        return token_screen.merge([token_screen.from_goplus(verdict, family)] + list(facts or []))

    def _ohlcv_for(self, chain: str, pool: str, **kw):
        from tools.defi.providers import geckoterminal as _gt
        return (self._ohlcv_fn or _gt.ohlcv)(chain, pool, **kw)

    def _pool_for_token(self, chain: str, address: str):
        """The resolved pool — a ``PoolPick`` when the depth came with it.

        The default resolver now carries liquidity and pool count; an injected
        seam (tests, older callers) may still return a bare address string, and
        both shapes are accepted. Callers that only want the address use
        :func:`_pool_address`.
        """
        from tools.defi.providers import geckoterminal as _gt
        fn = self._pool_for_token_fn or _gt.top_pool_for_token_detailed
        return fn(chain, address)

    def _identity(self, chain: str, address: str):
        """Token identity, family-dispatched.

        The EVM path reads ``symbol``/``decimals`` from the contract itself,
        which is the strongest identity source there is. Solana has no
        equivalent here: the fields live in a metadata account this tier has no
        RPC client for, so an unrecognised mint reports UNKNOWN rather than
        borrowing the EVM reader (it would raise on base58) or inventing values.

        The PINNED table is consulted first, though, and for every family. It is
        a pure dict lookup with no RPC behind it, and skipping it was a live bug
        (2026-08-28): the agent's own Solana USDC came back decimals-unknown, so
        `portfolio` rendered it as "100000 raw units" and filed the treasury's
        own quote asset under "NOT necessarily holdings you bought" — the
        airdrop-spam block. Unknown is the honest answer for an unknown mint; it
        was never the honest answer for the mint the settlement rail pins.
        """
        if self._identity_fn:
            return self._identity_fn(chain, address)
        from core.wallet import chains
        row = chains.get(chain)
        if row is not None and row.family != "evm":
            from core.wallet.tokens import canonical_token
            pinned = canonical_token(chain, address)
            if pinned is not None:
                return types.SimpleNamespace(
                    symbol=str(pinned["symbol"]), name=str(pinned["name"]),
                    decimals=int(pinned["decimals"]),  # type: ignore[arg-type]
                    verified=True, metadata_changed=False)
            # 068 B10: an OWNER-pinned mint reads verified (source owner_pin),
            # exactly as the spend gate treats it. The pin vouches for WHICH
            # mint; it invents no decimals and no name — those stay unknown.
            try:
                from core.wallet.token_pins import owner_pin
                opin = owner_pin(chain, address)
            except Exception:
                opin = None
            if opin is not None:
                return types.SimpleNamespace(
                    symbol=opin["symbol"], name=None, decimals=None, verified=True,
                    metadata_changed=False, source="owner_pin")
            return types.SimpleNamespace(
                symbol=None, name=None, decimals=None, verified=False,
                metadata_changed=False)
        from core.wallet.tokens import get_token_identity
        return get_token_identity(chain, address)

    def _balances(self, holder: str, chain: str, tokens: List[str]) -> Dict[str, Optional[int]]:
        if self._balances_fn:
            return self._balances_fn(holder, chain, tokens)
        from core.wallet.onchain import token_balances
        return token_balances(holder, chain, tokens)

    #: Stand-in `fromAddress` for a quote when no wallet exists. NOT the zero
    #: address: LI.FI rejects that outright ("/fromAddress Zero address is
    #: provided", HTTP 400), which made this verb blind to exactly the
    #: aggregator routes it exists to surface. Any well-formed address answers.
    _QUOTE_PLACEHOLDER_HOLDER = "0x1111111111111111111111111111111111111111"

    def _quote_holder(self) -> str:
        """Who to quote FOR. The wallet's own address when there is one.

        A route can depend on the address (balances, allowances, and the
        recipient a locally built route encodes), so quoting for the signer that
        would actually execute is the closest a read can get to the truth. This
        verb broadcasts nothing either way.
        """
        return self._resolve_holder() or self._QUOTE_PLACEHOLDER_HOLDER

    def _route(self, chain, token_in, token_out, amount_in_raw, *, holder,
               slippage_bps):
        """The SAME route seam `defi_trade.swap` uses.

        These two must never diverge. When this verb asked Uniswap V3 directly
        while `swap` had moved to the seam, the read verb reported "no route"
        for exactly the Aerodrome/V2 tokens the write verb could reach — and
        the agent makes that call BEFORE it ever loads the money tool, so a
        wrong "no route" here silently rules out a trade that was available.
        """
        if self._route_fn:
            got = self._route_fn(chain, token_in, token_out, amount_in_raw,
                                 holder=holder, slippage_bps=slippage_bps)
            if isinstance(got, tuple):
                return got
            return got, ("" if got is not None else f"no route on {chain}")
        from tools.defi.providers import routes
        return routes.best_route_with_reason(
            chain, token_in, token_out, amount_in_raw, holder=holder,
            slippage_bps=slippage_bps)

    def _discover(self, chain: str, kind: str):
        if self._discover_fn:
            return self._discover_fn(chain, kind)
        from tools.defi.providers import geckoterminal
        fn = (geckoterminal.new_pools if kind == "new"
              else geckoterminal.trending_pools)
        return fn(chain)

    def _native_balance(self, holder: str, chain: str) -> Optional[float]:
        """The holder's NATIVE (gas) balance, or None when it cannot be read.

        None means UNKNOWN and 0.0 means a genuine on-chain zero — the caller
        must render them differently. `core.wallet.onchain.balances` already
        draws that distinction (`_hex_int` keeps "0x" as None), so this is a
        thin adapter over it rather than a second read path.
        """
        if self._native_fn:
            return self._native_fn(holder, chain)
        from core.wallet.onchain import balances as _onchain_balances
        native, _usdc = _onchain_balances(holder, chain)
        return native

    def _on_curve(self, chain: str, address: str, info):
        """The pump.fun curve state when a Solana token is still ON its curve
        (and the pools show no depth), else None. Display only: the price
        verb shows the curve depth instead of a $0 pool liquidity."""
        from core.wallet import chains as _chains
        row = _chains.get(chain)
        if (row is None or row.family != "svm" or info.price_usd is None
                or (info.liquidity_usd or 0) > 0):
            return None
        try:
            from core.wallet import spl_facts
            curve = spl_facts.read_pump_curve(address)
        except Exception:
            return None
        return curve if getattr(curve, "state", None) == "on_curve" else None

    def _resolve_holder(self) -> Optional[str]:
        if self._holder != "__from_wallet__":
            return self._holder
        try:
            from core.wallet.factory import get_agent_wallet
            wallet = get_agent_wallet()
            return None if wallet is None else wallet.address
        except Exception:
            return None

    def _native_as_wrapped(self, chain: str):
        """('wrapped-native address', None) for a chain whose registry row names
        one, else (None, error). The read-side quote verbs price the wrapped
        asset; the swap verb resolves the same literal on its own path."""
        from core.wallet import chains as _chains
        row = _chains.get(chain)
        if row is None:
            return None, f"chain {chain!r} is not supported (this tier covers {', '.join(SUPPORTED_CHAINS)})"
        if getattr(row, "family", "evm") != "evm":
            return None, (f"'native' on {chain} is not quoted by this verb — it drives the "
                          f"EVM route providers; quote it with defi_trade.solana_swap(..., dry_run=true)")
        wrapped = getattr(row, "wrapped_native", None)
        if not wrapped:
            return None, (f"chain {chain!r} has no wrapped-native address in the registry, so "
                          f"'native' cannot be quoted here — pass the token's contract address")
        return wrapped, None

    def _validate(self, chain: str, address: str):
        """(canonical_address, error_message).

        Family-dispatched since Solana Phase 1: EVM addresses are EIP-55
        checksummed (a failed checksum is refused, never normalized), Solana
        mints are base58 and returned byte-for-byte, because base58 is
        case-SENSITIVE and any "normalization" would be a different address.
        """
        if chain not in SUPPORTED_CHAINS:
            return None, f"chain {chain!r} is not supported (this tier covers {', '.join(SUPPORTED_CHAINS)})"
        from core.wallet.addresses import normalize_for_chain
        try:
            return normalize_for_chain(chain, address), None
        except ValueError as exc:
            return None, str(exc)

    # -- actions ----------------------------------------------------------
    @BaseTool.action(
        "Find CANDIDATE contract addresses for a ticker. Returns a ranked list, "
        "never a single answer — you must choose an address before using any "
        "other verb.", param_model=ResolveParams)
    async def token_resolve(self, params: ResolveParams, execution_context=None):
        # Blocking body on a worker thread — see the note on `token_info`.
        # Declared `async def` while doing synchronous I/O, this verb held the
        # event loop for its whole read and made the controller's 60 s guard
        # unenforceable (2026-09-23, four dead SAFETY runs).
        return await asyncio.to_thread(self._token_resolve_sync, params, execution_context)

    def _token_resolve_sync(self, params: ResolveParams, execution_context=None):
        cands, answered, failed = self._search_all(params.symbol)
        if not answered:
            return self._ar(error=(
                f"token_resolve failed: no index answered "
                f"({'; '.join(failed) or 'unknown reason'})"))

        from tools.defi.token_screen import plain_errors, quoted
        searched = ", ".join(SUPPORTED_CHAINS)
        indexes = f"indexes asked: {', '.join(answered)}"
        if failed:
            indexes += f"; did not answer: {plain_errors(', '.join(failed))}"
        if not cands:
            return self._ar(content=(
                f"no candidates for ticker {quoted(params.symbol) or '?'} on {searched}. "
                "Nothing was resolved.\n"
                f"{indexes}.\n"
                "NOTE: each index returns a ranked, capped result set, so this "
                "means 'not in the top results for this ticker' — NOT 'no such "
                "token exists'. If you know the contract address, use it "
                "directly with token_info."))

        # Exact symbol matches first (case-insensitive), then by liquidity. A
        # search for PNL used to open with TRX-USDT and BNB.
        # NOT trimmed: "USDC  " is a whitespace typosquat, never an exact match.
        want = (params.symbol or "").strip().lower()
        ranked = sorted(cands, key=lambda c: (
            (c.symbol or "").lower() != want, -(c.liquidity_usd or 0.0)))
        exact = sum(1 for c in ranked if (c.symbol or "").lower() == want)
        shown = ranked[:_RESOLVE_ROWS_SHOWN]
        lines = [
            f"{len(cands)} candidate contract(s) for the ticker {quoted(params.symbol) or '?'} "
            f"({exact} with that exact symbol; searched: {searched}; {indexes}).",
            "This is NOT a resolution — you must choose an address. Exact matches "
            "come first, then by liquidity, which is PURCHASABLE: a deeper pool "
            "does not mean the token is genuine.",
            "",
        ]
        for c in shown:
            name = quoted(c.name, name=True)
            lines.append(
                f"  {c.address}  {quoted(c.symbol) or 'no symbol'}"
                + (f" {name}" if name and name != quoted(c.symbol) else "")
                + f"  {c.chain}  liquidity {_fmt_usd(c.liquidity_usd)}")
        if len(ranked) > len(shown):
            lines.append(f"  +{len(ranked) - len(shown)} more (not shown; full list in metadata)")
        return self._ar(content="\n".join(lines), metadata={"candidates": [
            {"address": c.address, "chain": c.chain, "symbol": c.symbol, "name": c.name,
             "liquidity_usd": c.liquidity_usd} for c in ranked]})

    @BaseTool.action(
        "Full report for ONE token contract: on-chain identity, price, liquidity "
        "and safety screen. Takes a contract address, not a ticker.",
        param_model=TokenRefParams)
    async def token_info(self, params: TokenRefParams, execution_context=None):
        # The body is SYNCHRONOUS network I/O. Declared `async def` and awaited
        # on the loop it would hold the loop for the whole read, which is what
        # made the 60 s `asyncio.wait_for` in the controller unenforceable and
        # killed three SAFETY runs on 2026-09-23. `to_thread` is what the action
        # registry already does for a plain `def`; the signature stays a
        # coroutine because book.py, webview/pages.py and the tests await it.
        return await asyncio.to_thread(self._token_info_sync, params, execution_context)

    def _token_info_sync(self, params: TokenRefParams, execution_context=None):
        address, err = self._validate(params.chain, params.address)
        if err:
            return self._ar(error=err)
        try:
            ident = self._identity(params.chain, address)
        except Exception as exc:
            wrong = self._not_a_token(params.chain, address)
            if wrong:
                return self._ar(error=wrong)
            return self._ar(error=f"token_info failed reading identity: {exc}")
        if getattr(ident, "decimals", None) is None:
            # 071: a wallet passed where a token belongs used to come back
            # "symbol unknown / price unknown" — a wrong answer to the question
            # that was asked. Only checked when identity is weak, so a normal
            # token read costs no extra call.
            wrong = self._not_a_token(params.chain, address)
            if wrong:
                return self._ar(error=wrong)
            ident = self._svm_identity_fill(params.chain, address, ident)

        price = self._price_for(params.chain, address)
        verdict = self._screen_for(params.chain, address)

        # 071 W2: ONE screen over GoPlus + the chain's own facts + the keyless
        # audits. A partial screen says PARTIAL and names what did not run; a
        # source that failed never turns its checks into a pass.
        from tools.defi import token_screen
        screen = self._merged_screen(params.chain, address, verdict)
        lines = [f"token {address} (chain {params.chain})"]
        lines += _identity_lines(ident, token_screen.jupiter_verified(screen))
        lines += (_curve_price_lines(price, screen.curve) if screen.curve is not None
                  else _price_line(price).splitlines())
        lines += token_screen.render(
            screen, canonical_symbol=(ident.symbol or "this token") if ident.verified else None)

        # Concentration, from the SAME screener answer (Solana: the RPC rows
        # the screen already read). Cheap, and it is the question a
        # holder-cluster map is opened for.
        try:
            holders = (screen.holders if screen.holders is not None and not self._holders_fn
                       else self._holders_for(params.chain, address))
        except Exception:
            holders = None
        lines.append(_holders_line(holders, screen))
        # The base58/EIP-55 typo note lives on the SEND verbs, where an address
        # is about to receive funds; a read of a token does not need it.
        holders_ok = holders is not None and getattr(holders, "available", False)
        return self._ar(content="\n".join(lines), metadata={
            "chain": params.chain, "address": address,
            "symbol": ident.symbol, "name": getattr(ident, "name", None),
            "decimals": ident.decimals, "verified": bool(ident.verified),
            "quote": _quote_metadata(price),
            # 071 W2: the MERGED screen — the same facts the prose renders.
            "screen": {"available": screen.available,
                       "partial": screen.partial,
                       "material_gaps": token_screen.material_gaps(screen),
                       "flags": list(screen.flags),
                       "hard_fails": list(screen.hard_fails),
                       "checks": [{"name": c.name, "result": c.result, "source": c.source}
                                  for c in screen.checks],
                       "not_checked": [{"name": n.name, "reason": n.reason}
                                       for n in screen.not_checked],
                       "sources": list(screen.answered),
                       "failed_sources": list(screen.failed)},
            "holders": ({"available": True, "holder_count": holders.holder_count,
                         "top_percent": holders.top_percent,
                         "top_percent_wallets": holders.top_percent_wallets}
                        if holders_ok else {"available": False}),
        })

    @BaseTool.action(
        "Scan a chain's pools and return a VERDICT for each — SURVIVOR / "
        "CANDIDATE / TOO_NEW / PASS / WASH / UNSCREENABLE — from the measured "
        "separators: 24h volume over liquidity, 1h volume over liquidity, "
        "transactions per unique buyer, depth and age. This reads the MARKET "
        "around a token, never the contract: run token_info for honeypot, taxes "
        "and mint authority, and token_holders for concentration, before acting "
        "on anything here.",
        param_model=ScanParams)
    async def scan(self, params: ScanParams, execution_context=None):
        # Blocking body on a worker thread — see the note on `token_info`.
        # Declared `async def` while doing synchronous I/O, this verb held the
        # event loop for its whole read and made the controller's 60 s guard
        # unenforceable (2026-09-23, four dead SAFETY runs).
        return await asyncio.to_thread(self._scan_sync, params, execution_context)

    def _scan_sync(self, params: ScanParams, execution_context=None):
        from tools.defi import pool_screen
        if params.chain not in SUPPORTED_CHAINS:
            return self._ar(error=(
                f"chain {params.chain!r} is not supported (this tier covers "
                f"{', '.join(SUPPORTED_CHAINS)})"))
        kind = (params.kind or "trending").strip().lower()
        if kind not in ("trending", "new"):
            return self._ar(error=f"kind {params.kind!r} must be 'trending' or 'new'")
        try:
            pools = self._discover(params.chain, kind) or []
        except Exception as exc:
            return self._ar(error=f"pool discovery failed: {exc}")

        kept, excluded = [], 0
        for pool in pools:
            if (pool.liquidity_usd is not None
                    and pool.liquidity_usd < params.min_liquidity_usd):
                excluded += 1
                continue
            kept.append((pool, pool_screen.classify(pool)))
            if len(kept) >= params.limit:
                break
        return self._ar(content="\n".join(_render_scan(
            kept, chain=params.chain, kind=kind, excluded=excluded,
            floor=params.min_liquidity_usd)))

    @BaseTool.action(
        "PRICE HISTORY as candles. A 24h change figure cannot tell a steady "
        "climber from one spike twenty hours ago, and that difference is the "
        "whole read: the tokens that survived climbed over WEEKS in steps, the "
        "wash-traded ones peaked 2-6 hours after launch and lost 75-99%. "
        "Candles are POOL-scoped, so the pool actually read is named.",
        param_model=OhlcvParams)
    async def ohlcv(self, params: OhlcvParams, execution_context=None):
        # Blocking body on a worker thread — see the note on `token_info`.
        # Declared `async def` while doing synchronous I/O, this verb held the
        # event loop for its whole read and made the controller's 60 s guard
        # unenforceable (2026-09-23, four dead SAFETY runs).
        return await asyncio.to_thread(self._ohlcv_sync, params, execution_context)

    def _ohlcv_sync(self, params: OhlcvParams, execution_context=None):
        from tools.defi.providers.geckoterminal import (
            OHLCV_TIMEFRAMES, summarize_candles,
        )
        address, err = self._validate(params.chain, params.address)
        if err:
            return self._ar(error=err)
        timeframe = (params.timeframe or "hour").strip().lower()
        if timeframe not in OHLCV_TIMEFRAMES:
            return self._ar(error=(f"timeframe {params.timeframe!r} is not one of "
                                   f"{', '.join(OHLCV_TIMEFRAMES)}"))
        pool = (params.pool or "").strip()
        if pool:
            # `pool` is a SECOND caller-supplied address and it reaches the
            # indexer as a URL path segment. `address` above was validated;
            # this one was not, which is the whole finding.
            # A pool may be a bytes32 id (Robinhood Chain), not only an address —
            # the same path-safe validator the provider applies.
            from tools.defi.providers.geckoterminal import _url_safe_address
            try:
                pool = _url_safe_address(params.chain, pool, what="pool")
            except ValueError as exc:
                return self._ar(error=f"pool: {exc}")
        # `depth` is what the resolver knows about the pool it PICKED; a
        # caller-supplied pool has none and must not be rendered as if it did.
        depth = None
        reference = None
        if pool:
            try:  # best effort: only to show the caller's pool against the pick
                reference = self._pool_for_token(params.chain, address)
                if not getattr(reference, "address", None):
                    reference = None
            except Exception:
                reference = None
        if not pool:
            try:
                picked = self._pool_for_token(params.chain, address)
            except Exception as exc:
                return self._ar(error=f"could not resolve a pool for {address}: {exc}")
            pool = getattr(picked, "address", picked) or ""
            if getattr(picked, "address", None):
                depth = picked
        if not pool:
            return self._ar(error=(
                f"no indexed pool found for {address} on {params.chain}, so there "
                f"are no candles to read. For a token minutes old this is the "
                f"honest answer, not an outage — the indexer has not caught up."))
        try:
            candles = self._ohlcv_for(params.chain, pool, timeframe=timeframe,
                                      aggregate=params.aggregate, limit=params.limit)
        except Exception as exc:
            return self._ar(error=f"ohlcv failed: {exc}")
        return self._ar(content="\n".join(_render_candles(
            candles, summarize_candles(candles), chain=params.chain,
            token=address, pool=pool, timeframe=timeframe,
            aggregate=params.aggregate, depth=depth, reference=reference)))

    @BaseTool.action(
        "WHO owns this token: top holders with their share, wallet vs contract, "
        "LP holders and whether the LP is locked, and the creator's own stake. "
        "This is the concentration read — a holder-cluster map in text. It does "
        "NOT prove wallets are unrelated: one person can hold ten addresses, and "
        "nothing here traces funding between them.",
        param_model=TokenRefParams)
    async def token_holders(self, params: TokenRefParams, execution_context=None):
        # Blocking body — see the note on `token_info`.
        return await asyncio.to_thread(self._token_holders_sync, params, execution_context)

    def _token_holders_sync(self, params: TokenRefParams, execution_context=None):
        address, err = self._validate(params.chain, params.address)
        if err:
            return self._ar(error=err)
        try:
            report = self._holders_for(params.chain, address)
        except Exception as exc:
            return self._ar(error=f"token_holders failed: {exc}")
        if report is None or not getattr(report, "available", False):
            wrong = self._not_a_token(params.chain, address)
            if wrong:
                return self._ar(error=wrong)
        return self._ar(content="\n".join(
            _render_holders(report, chain=params.chain, address=address)))

    @BaseTool.action(
        "Price a SWAP before committing to one: the route the swap verb would "
        "take (Uniswap V3, then a DEX aggregator if the operator enabled one), "
        "the output, the spender approve_token must authorise and an "
        "independent-price cross-check. Read-only — moves no funds. Each side is "
        "a CONTRACT ADDRESS or 'native' (priced via the wrapped native). EVM "
        "chains only: quote Solana with defi_trade.solana_swap(dry_run=true).",
        param_model=SwapQuoteParams)
    async def swap_quote(self, params: SwapQuoteParams, execution_context=None):
        # Blocking body — see the note on `token_info`.
        return await asyncio.to_thread(self._swap_quote_sync, params, execution_context)

    @BaseTool.action("Spot price for ONE token contract address (not a ticker).",
                     param_model=TokenRefParams)
    async def price(self, params: TokenRefParams, execution_context=None):
        # Blocking body — see the note on `token_info`.
        return await asyncio.to_thread(self._price_sync, params, execution_context)

    def _price_sync(self, params: TokenRefParams, execution_context=None):
        address, err = self._validate(params.chain, params.address)
        if err:
            return self._ar(error=err)
        info = self._price_for(params.chain, address)
        if info.price_usd is None:
            wrong = self._not_a_token(params.chain, address)
            if wrong:
                return self._ar(error=wrong)
            failed = getattr(info, "failed", None) or ()
            why = ("no indexed pool found" if not failed else
                   "no source priced it; did not answer: "
                   + ", ".join(f"{n} ({e})" for n, e in failed))
            return self._ar(content=(
                f"{address}: price unknown ({why}). "
                "Unknown is not zero — do not treat this as worthless."),
                metadata={"chain": params.chain, "address": address,
                          "quote": _quote_metadata(info)})
        extra = _quote_source_lines(info, indent="  ")
        curve = self._on_curve(params.chain, address, info)
        if curve is not None:
            body = (f"{address}: {_fmt_usd(info.price_usd)} per token   "
                    f"(pump.fun curve price — it moves with every buy)\n"
                    + "\n".join(_curve_price_lines(info, curve)[1:]))
        else:
            body = (f"{address}: {_fmt_usd(info.price_usd)} per token   "
                    f"confidence: {_display_confidence(info)}   pools: {info.pool_count}   "
                    f"liquidity: {_fmt_usd(info.liquidity_usd)}"
                    + ("\n  ⚠ low confidence: thin or single-pool liquidity is cheap to "
                       "move, so treat this price as weak."
                       if info.confidence == "low" else "")
                    + ("\n" + "\n".join(extra) if extra else ""))
        return self._ar(content=body,
            metadata={"chain": params.chain, "address": address,
                      "quote": _quote_metadata(info)})

    @BaseTool.action(
        "The agent wallet's own token holdings on ONE chain, USD-valued, with "
        "explicit coverage. Holdings on other chains are NOT included — ask per "
        "chain. Reveals own funds — treated as high-impact. For SOMEONE ELSE's "
        "wallet use wallet_holdings.",
        param_model=PortfolioParams)
    async def portfolio(self, params: PortfolioParams, execution_context=None):
        # Blocking body — see the note on `token_info`. This is the verb that
        # prices ONE HOLDING PER NETWORK CALL, so it is the longest-running of
        # the four and the one the 600 s stalls were logged against. Its own
        # wall-clock pricing budget bounds the loop; `to_thread` is what makes
        # the controller's 60 s guard able to cut it short at all.
        return await asyncio.to_thread(self._portfolio_sync, params, execution_context)

    def _portfolio_sync(self, params: PortfolioParams, execution_context=None):
        from core.wallet import chains
        if self._holder == "__from_wallet__":
            refusal = _operator_read_refusal(execution_context)
            if refusal:
                return self._ar(error=refusal)
        row = chains.get(params.chain)
        if row is not None and row.family == "svm":
            return self._solana_portfolio_sync(params)
        holder = self._resolve_holder()
        if not holder:
            return self._ar(error="agent wallet not enabled (set AGENT_WALLET_ENABLED=true) "
                                  "— no address to report holdings for")
        from tools.defi import account_mode
        if account_mode.requested(params):
            # 069 v4 A3: the account of an NFT this treasury owns (verified now).
            held, why = account_mode.resolve(params, params.chain, holder)
            if why:
                return self._ar(error=why.replace(" Nothing was broadcast.", ""))
            holder = held.account
        return self._evm_holdings_report(holder, params.chain)

    @BaseTool.action(
        "Compare the position ledger's '## Open positions' table against "
        "ACTUAL on-chain holdings and report every disagreement, loudly. Run "
        "this at the START of every trading run and before ANY public claim "
        "about positions or track record — its output is authoritative over "
        "your memory and over the ledger's own run-log narrative.",
        param_model=ReconcileParams)
    async def reconcile(self, params: ReconcileParams, execution_context=None):
        """The money rails' step-1 gate. A refusal or error here is a durable
        FACT (`rail_precondition_failed`), never only the returned error text:
        on 2026-09-21 this verb refused every call for 9 h (ledger > 1 MB) and
        six rails ran without their gate while every status seat read healthy."""
        try:
            # Blocking body — see the note on `token_info`. EVERY money rail
            # begins with this read, so a reconcile that holds the loop is the
            # same 600 s stall on four rails instead of one.
            res = await asyncio.to_thread(self._reconcile_impl_sync, params, execution_context)
        except Exception as exc:
            self._record_precondition_failed(params, execution_context, f"{type(exc).__name__}: {exc}")
            raise
        if getattr(res, "error", None):
            self._record_precondition_failed(params, execution_context, str(res.error))
        return res

    async def _reconcile_impl(self, params: ReconcileParams, execution_context=None):
        """Thin delegator kept for any caller that awaits the impl directly."""
        return await asyncio.to_thread(self._reconcile_impl_sync, params, execution_context)

    @BaseTool.action(
        "This agent's OWN open positions from the money rail's book, with the "
        "arithmetic done: per token the size (measured or quoted), the cost "
        "basis (average cost) or 'basis unknown', the current price, value, "
        "unrealized P&L in USD and %, realized P&L, and the high-water price. "
        "Holdings open only in the position ledger are listed as LEDGER-ONLY, "
        "basis unknown. Repeat its figures; never compute or convert a money figure yourself. "
        "Reveals own funds — owner-only. For SOMEONE ELSE's wallet use "
        "wallet_holdings.",
        param_model=PositionsParams)
    async def positions(self, params: PositionsParams, execution_context=None):
        # Blocking body (one price read per position) — off the event loop.
        return await asyncio.to_thread(self._positions_sync, params, execution_context)

    def _positions_sync(self, params: PositionsParams, execution_context=None):
        """071 W3: read ``open_positions`` + ``position_realized`` for the
        tenant, price each row, and let ``positions_view`` do every sum."""
        refusal = _operator_read_refusal(execution_context)
        if refusal:
            return self._ar(error=refusal)
        chain = (params.chain or "").strip().lower() or None
        if chain and chain not in SUPPORTED_CHAINS:
            return self._ar(error=(
                f"chain {chain!r} is not supported (this tier covers "
                f"{', '.join(SUPPORTED_CHAINS)})"))
        uid = str(getattr(execution_context, "user_id", "") or "").strip()
        if not uid:
            try:
                from core.exec_identity import current_exec_identity
                uid = str(current_exec_identity()[0] or "").strip()
            except Exception:
                uid = ""
        if not uid:
            try:
                from core.instance import resolve_owner_principal
                uid = str(resolve_owner_principal() or "").strip()
            except Exception:
                uid = ""
        if not uid:
            return self._ar(error="no tenant on this call — cannot pick whose book to read")
        from core import open_positions
        try:
            # Every chain's rows, so a ledger holding tracked on ANOTHER chain
            # is never mistaken for ledger-only; the scope filter runs here.
            all_entries = open_positions.positions_for(uid)
            entries = [e for e in all_entries
                       if not chain or str(e.chain).strip().lower() == chain]
            realized = open_positions.realized_for(uid, chain=chain)
        except Exception as exc:
            return self._ar(error=(
                f"the rail book (open_positions.db) exists but could not be read "
                f"({type(exc).__name__}) — this is NOT an empty book; do not "
                f"report zero positions"))
        from tools.defi import positions_view

        def _observe(entry, price):
            return open_positions.observe_price(
                uid, entry.chain, entry.address, price, account=entry.account)

        def _ledger_read():
            from core.position_ledger import read_open_positions, resolve_position_ledger_path
            from core.runtime_paths import resolve_data_home
            path = resolve_position_ledger_path(str(resolve_data_home()))
            return read_open_positions(path=path) if path else (None, None)

        # Read-only: ledger holdings with no store row are listed, never backfilled.
        ledger = positions_view.ledger_only(_ledger_read, all_entries,
                                            self._price_for, chain=chain)
        text, meta = positions_view.build(
            entries, realized, self._price_for, chain=chain, observe_fn=_observe,
            ledger=ledger)
        return self._ar(content=text, metadata=meta)

    @BaseTool.action(
        "List the POOLS most recently indexed on a chain — the fresh-launch "
        "frontier. Returns places to look, NOT vetted tokens: nothing here is "
        "screened, and most minutes-old pools go to zero. Screen every "
        "candidate with token_info (honeypot + sell tax) before acting on it.",
        param_model=DiscoverParams)
    async def new_pools(self, params: DiscoverParams, execution_context=None):
        # Blocking body on a worker thread — see the note on `token_info`.
        # Declared `async def` while doing synchronous I/O, this verb held the
        # event loop for its whole read and made the controller's 60 s guard
        # unenforceable (2026-09-23, four dead SAFETY runs).
        return await asyncio.to_thread(self._new_pools_sync, params, execution_context)

    def _new_pools_sync(self, params: DiscoverParams, execution_context=None):
        return self._render_discovery(params, kind="new", caveat=(
            "These pools are MINUTES old. Liquidity and volume figures lag the "
            "chain, an unknown figure means the indexer has not caught up (it "
            "does NOT mean zero), and nothing here has been screened."))

    @BaseTool.action(
        "List the pools a public indexer currently ranks as TRENDING on a "
        "chain. Trending is a popularity claim and popularity is purchasable, "
        "so treat this as a place to look, never a verdict. Screen every "
        "candidate with token_info before acting on it.",
        param_model=DiscoverParams)
    async def trending(self, params: DiscoverParams, execution_context=None):
        # Blocking body on a worker thread — see the note on `token_info`.
        # Declared `async def` while doing synchronous I/O, this verb held the
        # event loop for its whole read and made the controller's 60 s guard
        # unenforceable (2026-09-23, four dead SAFETY runs).
        return await asyncio.to_thread(self._trending_sync, params, execution_context)

    def _trending_sync(self, params: DiscoverParams, execution_context=None):
        return self._render_discovery(params, kind="trending", caveat=(
            "Trending rank is a popularity claim, and attention is PURCHASABLE "
            "— a paid boost looks identical to organic interest here. Nothing "
            "in this list has been screened."))

    def _render_discovery(self, params: DiscoverParams, *, kind: str, caveat: str):
        if params.chain not in SUPPORTED_CHAINS:
            return self._ar(error=(
                f"chain {params.chain!r} is not supported (this tier covers "
                f"{', '.join(SUPPORTED_CHAINS)})"))
        try:
            pools = self._discover(params.chain, kind) or []
        except Exception as exc:
            logger.debug("discovery failed on %s (%s)", params.chain, exc)
            return self._ar(error=f"pool discovery failed: {exc}")

        kept, excluded = [], 0
        for pool in pools:
            # An UNKNOWN figure never fails a floor. A floor answers "is this
            # number too small"; it has no answer for "there is no number", and
            # treating the two the same is how a real candidate disappears
            # without the caller ever learning it existed.
            if (pool.liquidity_usd is not None
                    and pool.liquidity_usd < params.min_liquidity_usd):
                excluded += 1
                continue
            if (pool.volume_h24_usd is not None
                    and pool.volume_h24_usd < params.min_volume_h24_usd):
                excluded += 1
                continue
            kept.append(pool)
            if len(kept) >= params.limit:
                break

        label = "newest pools" if kind == "new" else "trending pools"
        lines = [f"{label} on {params.chain} — {len(kept)} shown", "", caveat, ""]
        if not kept:
            lines.append("  (no pools matched)")
        for pool in kept:
            token = pool.base_token or (
                "UNKNOWN — the indexer named a base token this chain's address "
                "rules do not accept, so it cannot be looked up from here")
            lines.append(
                f"  {quoted(pool.name, name=True)}\n"
                f"    token: {token}\n"
                f"    dex: {quoted(pool.dex, name=True)}   created: {quoted(pool.created_at, name=True) or 'unknown'}"
                f"   age: {_fmt_hours(getattr(pool, 'age_hours', None))}\n"
                f"    liquidity: {_fmt_usd(pool.liquidity_usd)}   "
                f"vol 24h: {_fmt_usd(pool.volume_h24_usd)}   "
                f"24h: {'unknown' if pool.price_change_h24_pct is None else f'{pool.price_change_h24_pct:+.1f}%'}\n"
                f"    {_fmt_trade_shape(pool)}")
        if excluded:
            lines += ["", f"  {excluded} pool(s) excluded by your floors "
                          f"(liquidity < {_fmt_usd(params.min_liquidity_usd)}, "
                          f"volume < {_fmt_usd(params.min_volume_h24_usd)}). "
                          f"Said out loud because a silent filter reads as "
                          f"'this is all there was'."]
        lines += ["", "NOT SCREENED. Run token_info(chain, address) on any "
                      "candidate before you act on it — that is the verb that "
                      "checks honeypot behaviour and sell tax."]
        return self._ar(content="\n".join(lines))

    async def _solana_portfolio(self, params: PortfolioParams):
        """Thin delegator kept for any out-of-tree caller that awaits it.

        The EVM path reaches `_solana_portfolio_sync` directly — it is already
        on a worker thread by then and must not hop to another one.
        """
        return await asyncio.to_thread(self._solana_portfolio_sync, params)

    @BaseTool.action(
        "Read ANY wallet's public token holdings on ONE chain: native balance, "
        "every token with human amounts, USD where the price is trustworthy, and "
        "an explicit coverage line. Works for Solana (base58) and EVM (0x) "
        "wallets. Use this when someone asks you to check / look up a wallet "
        "address. Public chain data — read-only.",
        param_model=WalletHoldingsParams)
    async def wallet_holdings(self, params: WalletHoldingsParams, execution_context=None):
        # Blocking body — see the note on `token_info`.
        return await asyncio.to_thread(self._wallet_holdings_sync, params, execution_context)

    @BaseTool.action(
        "Read ANY wallet's RECENT ACTIVITY on one chain (up to 25 transactions, "
        "newest first): receive / send / swap / failed rows with exact amounts, "
        "the counterparty when it is obvious, fees, and a per-token NET FLOW over "
        "the rows read (computed here — not a cost-basis PnL). Solana (base58) and "
        "EVM (0x, needs chain). For any address other than our own it also lists OUR "
        "recorded payments to it (the audit ledger) — use it before calling a payment "
        "unverified. Read-only.",
        param_model=_wallet_intel.WalletActivityParams)
    async def wallet_activity(self, params, execution_context=None):
        return await asyncio.to_thread(_wallet_intel.wallet_activity_sync, self, params,
                                       execution_context)

    @BaseTool.action(
        "Where a TOKEN came from: its deployer, the creation transaction, a "
        "launchpad (pump.fun) when the chain shows one, the deployer's other "
        "contract creations, whether the deployer still holds or sent it, and on "
        "Solana the launch buyers and any SHARED FUNDER one hop back (addresses, "
        "not people). Read-only; bounded reads say what they did not read.",
        param_model=_wallet_intel.TokenOriginParams)
    async def token_origin(self, params, execution_context=None):
        return await asyncio.to_thread(_wallet_intel.token_origin_sync, self, params,
                                       execution_context)

    #: How many unvalued rows a holdings render lists by address. A wallet can
    #: carry thousands of airdropped mints (an exchange hot wallet measured
    #: 3,093 on 2026-10-01); past this the COUNT is stated, never dropped.
    _UNVALUED_ROWS_SHOWN = _REST_ROWS_LISTED

    @BaseTool.action(
        "List the non-fungible tokens (NFTs) an address holds. Defaults to your "
        "own wallet. Read-only. ⚠️ If no enumeration provider is configured this "
        "says so — it never returns an empty list, because 'you own nothing' and "
        "'I could not look' are different answers.",
        param_model=NftHoldingsParams)
    async def nft_holdings(self, params: NftHoldingsParams, execution_context=None):
        # Blocking body on a worker thread — see the note on `token_info`.
        # Declared `async def` while doing synchronous I/O, this verb held the
        # event loop for its whole read and made the controller's 60 s guard
        # unenforceable (2026-09-23, four dead SAFETY runs).
        return await asyncio.to_thread(self._nft_holdings_sync, params, execution_context)

    @BaseTool.action(
        "Read one NFT straight from the chain: who owns it, which standard it "
        "implements, and its metadata URI. Keyless and exact. A field that the "
        "node did not answer is reported as NOT CHECKED, never as a default.",
        param_model=NftInfoParams)
    async def nft_info(self, params: NftInfoParams, execution_context=None):
        # Blocking body on a worker thread — see the note on `token_info`.
        # Declared `async def` while doing synchronous I/O, this verb held the
        # event loop for its whole read and made the controller's 60 s guard
        # unenforceable (2026-09-23, four dead SAFETY runs).
        return await asyncio.to_thread(self._nft_info_sync, params, execution_context)

    # -- liquidity pools (proposal 048 P12) --------------------------------

    @BaseTool.action(
        "Monitor a Pons graduated Uniswap v4 pool (090 §3): price, tick, in-range "
        "liquidity and reserves, depth to move the price ±X%, the impact of one buy, "
        "the 24h change and a median reference from earlier readings, and a code-hash "
        "check of the pinned v4 contracts and the hook. ALERT lines come first. "
        "Read-only; each reading is journaled. Safe for a cron read job.",
        param_model=PoolMetricsParams)
    async def pool_metrics(self, params: PoolMetricsParams, execution_context=None):
        return await asyncio.to_thread(self._pool_metrics_sync, params, execution_context)

    @BaseTool.action(
        "List this address's Uniswap v3 liquidity positions: pool, range, "
        "whether it is in range right now, current holdings and uncollected "
        "fees. Defaults to your own wallet. Read-only. Uniswap v4 lands in a "
        "later phase.",
        param_model=LpPositionsParams)
    async def lp_positions(self, params: LpPositionsParams, execution_context=None):
        # Blocking body on a worker thread — see the note on `token_info`.
        # Declared `async def` while doing synchronous I/O, this verb held the
        # event loop for its whole read and made the controller's 60 s guard
        # unenforceable (2026-09-23, four dead SAFETY runs).
        return await asyncio.to_thread(self._lp_positions_sync, params, execution_context)

    @BaseTool.action(
        "Read a Uniswap v3 pool's live state: tokens, fee tier, current tick, "
        "price and liquidity. Give a pool address, OR token_a + token_b + "
        "fee to locate one. A pair with no pool yet is reported, not "
        "invented. Read-only. protocol='v4' reads a Pons graduated pool "
        "(token_a/token_b = 'native' + the Pons token).",
        param_model=LpPoolInfoParams)
    async def lp_pool_info(self, params: LpPoolInfoParams, execution_context=None):
        # Blocking body on a worker thread — see the note on `token_info`.
        # Declared `async def` while doing synchronous I/O, this verb held the
        # event loop for its whole read and made the controller's 60 s guard
        # unenforceable (2026-09-23, four dead SAFETY runs).
        return await asyncio.to_thread(self._lp_pool_info_sync, params, execution_context)

    @BaseTool.action(
        "Quote a Uniswap v3 liquidity deposit: given one or both token "
        "amounts and a price range, compute the liquidity this would mint "
        "and the exact paired amounts. Works against an existing pool (its "
        "live price is read) or a not-yet-created one (give initial_price). "
        "Read-only — never deposits. protocol='v4' quotes a full-range "
        "deposit into a Pons graduated pool.",
        param_model=LpQuoteParams)
    async def lp_quote(self, params: LpQuoteParams, execution_context=None):
        # Blocking body on a worker thread — see the note on `token_info`.
        # Declared `async def` while doing synchronous I/O, this verb held the
        # event loop for its whole read and made the controller's 60 s guard
        # unenforceable (2026-09-23, four dead SAFETY runs).
        return await asyncio.to_thread(self._lp_quote_sync, params, execution_context)

    @BaseTool.action(
        "Raw read-only eth_call against a contract. Returns the raw hex; any "
        "decoding is best-effort and unverified. No state change.",
        param_model=ContractReadParams)
    async def contract_read(self, params: ContractReadParams, execution_context=None):
        # Blocking body on a worker thread — see the note on `token_info`.
        # Declared `async def` while doing synchronous I/O, this verb held the
        # event loop for its whole read and made the controller's 60 s guard
        # unenforceable (2026-09-23, four dead SAFETY runs).
        return await asyncio.to_thread(self._contract_read_sync, params, execution_context)

