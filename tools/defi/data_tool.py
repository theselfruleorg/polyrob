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
"""
from __future__ import annotations  # safe: @BaseTool.action uses explicit param_model

import logging
import types
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field

from tools.base_tool import BaseTool
from tools.defi.providers import alchemy_index, dexscreener, goplus
from tools.defi.providers.base import Candidate, PriceInfo, ScreenVerdict

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


def _chain_field(what: str) -> str:
    from core.wallet import chains
    return (f"Chain to {what} — the same address is a DIFFERENT token on a "
            f"different chain, so name it deliberately. Readable: "
            f"{', '.join(chains.names())}.")


class ResolveParams(BaseModel):
    symbol: str = Field(..., description=(
        "Ticker to look up. Returns CANDIDATES ranked by liquidity — never a "
        "single answer. You must then choose a contract address yourself; "
        "several different contracts commonly claim the same ticker."))


class SwapQuoteParams(BaseModel):
    chain: str = Field("base", description=_chain_field("quote on"))
    token_in: str = Field(..., description="CONTRACT ADDRESS of the token to sell (0x…)")
    token_out: str = Field(..., description="CONTRACT ADDRESS of the token to buy (0x…)")
    amount_in: float = Field(..., gt=0, description="Human amount of token_in to sell")


class TokenRefParams(BaseModel):
    chain: str = Field("base", description=_chain_field("look the token up on"))
    address: str = Field(..., description=(
        "The token's CONTRACT ADDRESS (0x…, 20 bytes). A ticker is not accepted "
        "— use token_resolve first to find candidate addresses."))


class PortfolioParams(BaseModel):
    chain: str = Field("base", description=_chain_field("report holdings on"))


class ReconcileParams(BaseModel):
    chain: str = Field("base", description=_chain_field("reconcile against"))
    ledger_path: str = Field(..., description=(
        "Path to the LIVE position-ledger markdown file — never a copy or an "
        "excerpt. Its '## Open positions' table is compared row by row "
        "against actual on-chain balances."))


class DiscoverParams(BaseModel):
    chain: str = Field("base", description=_chain_field("discover pools on"))
    limit: int = Field(20, ge=1, le=50, description="How many pools to list.")
    min_liquidity_usd: float = Field(0.0, ge=0.0, description=(
        "Exclude pools whose reported liquidity is BELOW this. A pool whose "
        "liquidity is UNKNOWN is never excluded by this floor — unknown is not "
        "a number, and dropping it would hide it from you silently."))
    min_volume_h24_usd: float = Field(0.0, ge=0.0, description=(
        "Exclude pools whose 24h volume is below this. Same unknown rule."))


class ContractReadParams(BaseModel):
    chain: str = Field("base", description=_chain_field("read from"))
    address: str = Field(..., description="Contract address to read from (0x…)")
    signature: str = Field(..., description="Function signature, e.g. 'totalSupply()'")
    args: List[Any] = Field(default_factory=list, description="Arguments (currently unencoded; prefer no-arg views)")


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


def _fmt_usd(value: Optional[float]) -> str:
    return "unknown" if value is None else f"${value:,.2f}"


class DefiDataTool(BaseTool):
    """Read-only token sight. Seams are injectable so unit tests never hit the network."""

    def __init__(self, name: str = "defi_data", config=None, container=None, *,
                 search_fn: Optional[Callable] = None,
                 price_fn: Optional[Callable] = None,
                 screen_fn: Optional[Callable] = None,
                 balances_fn: Optional[Callable] = None,
                 index_fn: Optional[Callable] = None,
                 identity_fn: Optional[Callable] = None,
                 call_fn: Optional[Callable] = None,
                 native_fn: Optional[Callable] = None,
                 discover_fn: Optional[Callable] = None,
                 route_fn: Optional[Callable] = None,
                 holder: Optional[str] = "__from_wallet__"):
        super().__init__(name=name, config=config if config is not None else _NULL_CONFIG,
                         container=container)
        self._search_fn = search_fn
        self._price_fn = price_fn
        self._screen_fn = screen_fn
        self._balances_fn = balances_fn
        self._index_fn = index_fn
        self._identity_fn = identity_fn
        self._call_fn = call_fn
        self._native_fn = native_fn
        self._discover_fn = discover_fn
        self._route_fn = route_fn
        self._holder = holder

    # -- seams ------------------------------------------------------------
    def _ar(self, *, content: str = None, error: str = None):
        from tools.controller.types import ActionResult
        if error is not None:
            return ActionResult(error=error)
        return ActionResult(extracted_content=content)

    def _search(self, symbol: str) -> List[Candidate]:
        return (self._search_fn or dexscreener.search)(symbol)

    def _price_for(self, chain: str, address: str) -> PriceInfo:
        return (self._price_fn or dexscreener.token)(chain, address)

    def _screen_for(self, chain: str, address: str) -> ScreenVerdict:
        return (self._screen_fn or goplus.screen)(chain, address)

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

    def _resolve_holder(self) -> Optional[str]:
        if self._holder != "__from_wallet__":
            return self._holder
        try:
            from core.wallet.factory import get_agent_wallet
            wallet = get_agent_wallet()
            return None if wallet is None else wallet.address
        except Exception:
            return None

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
        try:
            cands = self._search(params.symbol)
        except Exception as exc:
            logger.debug("token_resolve failed", exc_info=True)
            return self._ar(error=f"token_resolve failed: {exc}")

        searched = ", ".join(SUPPORTED_CHAINS)
        if not cands:
            return self._ar(content=(
                f"no candidates for ticker {params.symbol!r} on {searched}. "
                "Nothing was resolved.\n"
                "NOTE: the index returns a ranked, capped result set, so this "
                "means 'not in the top results for this ticker' — NOT 'no such "
                "token exists'. If you know the contract address, use it "
                "directly with token_info."))

        lines = [
            f"{len(cands)} candidate contract(s) claim the ticker {params.symbol!r} "
            f"(searched: {searched}).",
            "",
            "This is NOT a resolution — you must choose an address. Ranking is by "
            "liquidity, which is PURCHASABLE, so a deeper pool does not mean the "
            "token is genuine.",
            "",
        ]
        for c in cands:
            lines.append(
                f"  {c.address}  symbol={c.symbol!r} name={c.name!r} "
                f"chain={c.chain} liquidity={_fmt_usd(c.liquidity_usd)}")
        return self._ar(content="\n".join(lines))

    @BaseTool.action(
        "Full report for ONE token contract: on-chain identity, price, liquidity "
        "and safety screen. Takes a contract address, not a ticker.",
        param_model=TokenRefParams)
    async def token_info(self, params: TokenRefParams, execution_context=None):
        address, err = self._validate(params.chain, params.address)
        if err:
            return self._ar(error=err)
        try:
            ident = self._identity(params.chain, address)
        except Exception as exc:
            return self._ar(error=f"token_info failed reading identity: {exc}")

        price = self._price_for(params.chain, address)
        verdict = self._screen_for(params.chain, address)

        lines = [
            f"token {address} (chain {params.chain})",
            f"  symbol:   {ident.symbol!r}   name: {ident.name!r}",
            f"  decimals: {'unknown' if ident.decimals is None else ident.decimals}",
            f"  verified: {str(ident.verified).lower()}"
            + ("   (on the pinned canonical list)" if ident.verified
               else "   (NOT on the canonical list — identity is self-reported)"),
        ]
        if ident.metadata_changed:
            lines.append("  ⚠ metadata_changed: this contract now reports different "
                         "symbol/decimals than when first seen; the first-seen values "
                         "are shown and it is treated as unverified")
        lines += [
            f"  price:    {_fmt_usd(price.price_usd)}   confidence: {price.confidence}"
            f"   pools: {price.pool_count}   liquidity: {_fmt_usd(price.liquidity_usd)}",
        ]
        if verdict.available:
            lines.append(f"  screen:   {len(verdict.checks)} checks ran; "
                         + (f"FLAGS: {', '.join(verdict.flags)}" if verdict.flags
                            else "no risk flags raised"))
            for k, v in sorted(verdict.checks.items()):
                lines.append(f"      {k} = {v}")
        else:
            lines.append("  screen:   unavailable — the screener returned nothing. "
                         "This is NOT a clean result; the token is UNSCREENED.")
        # What the ADDRESS itself does or does not prove. On EVM a mistyped
        # address would almost certainly have failed EIP-55; on Solana nothing
        # would have caught it, and carrying the EVM habit across is how funds
        # reach a real, wrong account.
        from core.wallet.addresses import typo_protection_note
        lines.append(f"  address:  {typo_protection_note(params.chain)}")
        return self._ar(content="\n".join(lines))

    @BaseTool.action(
        "Price a SWAP before committing to one: best Uniswap V3 fee tier and the "
        "output you would receive. Read-only — quotes nothing on-chain and moves "
        "no funds. Both sides must be CONTRACT ADDRESSES.",
        param_model=SwapQuoteParams)
    async def swap_quote(self, params: SwapQuoteParams, execution_context=None):
        from core.wallet.tokens import get_token_identity

        addr_in, err = self._validate(params.chain, params.token_in)
        if err:
            return self._ar(error=err)
        addr_out, err = self._validate(params.chain, params.token_out)
        if err:
            return self._ar(error=err)
        if addr_in.lower() == addr_out.lower():
            return self._ar(error="token_in and token_out are the same token")

        id_in = self._identity(params.chain, addr_in)
        id_out = self._identity(params.chain, addr_out)
        if id_in.decimals is None or id_out.decimals is None:
            return self._ar(error=(
                "one side does not report decimals — refusing to size a quote "
                "against a token whose denomination is unknown"))

        # Solana's route_hints are empty BY DESIGN — Jupiter routes it from
        # inside defi_trade.solana_swap, not through the EVM route providers
        # this verb drives. Falling through to the generic "no route" answer
        # would tell an agent that was told to "quote it FIRST" that Solana
        # tokens are unreachable, which is the false belief this whole change
        # set exists to remove. Name the verb that DOES quote there.
        from core.wallet import chains as _chains
        _row = _chains.get(params.chain)
        if _row is not None and _row.family == "svm":
            return self._ar(content=(
                f"swap_quote does not price {params.chain} — this verb drives "
                f"the EVM route providers, and Solana is routed by Jupiter "
                f"from inside the swap verb itself.\n"
                f"  Quote it with: defi_trade.solana_swap(..., dry_run=true)\n"
                f"  That returns the Jupiter route, the simulated output and "
                f"the guard verdict without broadcasting anything. This is NOT "
                f"'no route' and NOT 'unbuyable' — it is the wrong verb."))

        amount_in_raw = int(round(params.amount_in * (10 ** id_in.decimals)))
        route, route_why = self._route(params.chain, addr_in, addr_out,
                                       amount_in_raw, holder=self._quote_holder(),
                                       slippage_bps=_quote_slippage_bps())
        if route is None:
            return self._ar(content=(
                f"cannot route {id_in.symbol or addr_in} -> "
                f"{id_out.symbol or addr_out}: {route_why} "
                f"Either way this is UNKNOWN, not a zero-value trade."))

        out_human = route.amount_out_raw / (10 ** id_out.decimals)
        rate = out_human / params.amount_in if params.amount_in else 0
        return self._ar(content=(
            f"{params.amount_in} {id_in.symbol or addr_in} -> "
            f"{out_human:.8f} {id_out.symbol or addr_out}\n"
            f"  route:  {route.venue}\n"
            f"  rate:   {rate:.8f} {id_out.symbol or 'out'} per "
            f"{id_in.symbol or 'in'}\n"
            f"  spender: {route.spender}   (this is what approve_token must "
            f"authorise — NOT the token address)\n"
            f"  NOTE: a quote is what the route says right now, not a promise. "
            f"defi_trade.swap re-quotes and bounds it with slippage before "
            f"executing, and asks this same set of providers."))

    @BaseTool.action("Spot price for ONE token contract address (not a ticker).",
                     param_model=TokenRefParams)
    async def price(self, params: TokenRefParams, execution_context=None):
        address, err = self._validate(params.chain, params.address)
        if err:
            return self._ar(error=err)
        info = self._price_for(params.chain, address)
        if info.price_usd is None:
            return self._ar(content=(
                f"{address}: price unknown (no indexed pool found). "
                "Unknown is not zero — do not treat this as worthless."))
        return self._ar(content=(
            f"{address}: {_fmt_usd(info.price_usd)} per token   "
            f"confidence: {info.confidence}   pools: {info.pool_count}   "
            f"liquidity: {_fmt_usd(info.liquidity_usd)}"
            + ("\n  ⚠ low confidence: thin or single-pool liquidity is cheap to "
               "manipulate, so this price may be fabricated."
               if info.confidence == "low" else "")))

    @BaseTool.action(
        "The agent wallet's own token holdings on ONE chain, USD-valued, with "
        "explicit coverage. Holdings on other chains are NOT included — ask per "
        "chain. Reveals own funds — treated as high-impact.",
        param_model=PortfolioParams)
    async def portfolio(self, params: PortfolioParams, execution_context=None):
        from core.wallet import chains
        row = chains.get(params.chain)
        if row is not None and row.family == "svm":
            return await self._solana_portfolio(params)
        holder = self._resolve_holder()
        if not holder:
            return self._ar(error="agent wallet not enabled (set AGENT_WALLET_ENABLED=true) "
                                  "— no address to report holdings for")
        chain = params.chain
        if chain not in SUPPORTED_CHAINS:
            return self._ar(error=(
                f"chain {chain!r} is not supported (this tier covers "
                f"{', '.join(SUPPORTED_CHAINS)})"))
        indexed = None
        if self._index_fn is not None:
            indexed = self._index_fn(holder, chain=chain)
        elif alchemy_index.available():
            indexed = alchemy_index.fetch_balances(holder, chain=chain)

        if indexed is not None:
            raw = {a: v for a, v in indexed.items()}
            coverage = "indexed (alchemy) — complete"
            scanned_note = ""
        else:
            scan = self._scan_set(chain)
            raw = self._balances(holder, chain, scan)
            coverage = f"partial — {len(scan)} candidate address(es) scanned"
            scanned_note = ("\n  (no ALCHEMY_API_KEY, so holdings could not be enumerated; "
                            "a token outside the scanned set is INVISIBLE here)")

        from core.wallet.tokens import canonical_token
        valued, unpriced_known, unvalued, total = [], [], [], 0.0
        for addr, units in sorted(raw.items()):
            # A token on the registry's CANONICAL pin list is one an operator
            # verified on-chain — it is definitionally not something a stranger
            # sent you. Its provenance and its PRICE are separate questions, and
            # a third-party indexer is allowed to fail at the second. Keeping
            # the two apart matters: on 2026-08-25 a DexScreener outage put the
            # wallet's own USDC under the dust warning, and the agent duly
            # reported its entire spendable balance as "not a position".
            known = canonical_token(chain, addr) is not None
            sink = unpriced_known if known else unvalued
            if units is None:
                sink.append(f"  {addr}  balance UNKNOWN (read failed — not zero)")
                continue
            ident = self._identity(chain, addr)
            if ident.decimals is None:
                sink.append(f"  {addr}  {units} raw units (decimals unknown — cannot value)")
                continue
            amount = units / (10 ** ident.decimals)
            info = self._price_for(chain, addr)
            if info.price_usd is None or info.confidence != "high":
                sink.append(
                    f"  {addr}  {amount:,.6f} {ident.symbol or '?'}  "
                    f"value EXCLUDED ({info.confidence} confidence price)")
                continue
            value = amount * info.price_usd
            total += value
            valued.append(f"  {addr}  {amount:,.6f} {ident.symbol or '?'}  = {_fmt_usd(value)}")

        lines = [f"holdings for {holder} (chain {chain})",
                 f"coverage: {coverage}{scanned_note}", ""]
        lines += self._gas_lines(holder, chain) + [""]
        lines += valued or ["  (no confidently-valued holdings)"]
        lines += ["", f"total (confidently valued only): {_fmt_usd(total)}"]
        if unpriced_known:
            lines += ["", "verified holdings whose PRICE is unavailable right now:",
                      "  These are canonical, operator-verified tokens on this "
                      "chain — your own balance, not something anyone sent you. "
                      "Only the price lookup failed, so they are excluded from "
                      "the total above rather than from your holdings. Do not "
                      "describe these as dust, and do not conclude you hold "
                      "nothing; re-read when pricing recovers.",
                      ""] + unpriced_known
        if unvalued:
            # The old header explained the ARITHMETIC ("excluded from the total")
            # but not what these rows ARE, and a live run duly summarised four
            # address-poisoning airdrops as "four unvalued positions". A wallet
            # accumulates tokens it never bought; saying so here is cheaper than
            # hoping every caller remembers it.
            lines += ["", "unvalued — NOT necessarily holdings you bought:",
                      "  A token can sit at this address because someone SENT it to you. "
                      "Unsolicited airdrops (dust) are common and are frequently bait: "
                      "the sell path is where they drain approvals. Treat anything here "
                      "as noise to report, never as a position to realize, and NEVER "
                      "trade or approve one you did not deliberately buy.",
                      ""] + unvalued
        return self._ar(content="\n".join(lines))

    @BaseTool.action(
        "Compare the position ledger's '## Open positions' table against "
        "ACTUAL on-chain holdings and report every disagreement, loudly. Run "
        "this at the START of every trading run and before ANY public claim "
        "about positions or track record — its output is authoritative over "
        "your memory and over the ledger's own run-log narrative.",
        param_model=ReconcileParams)
    async def reconcile(self, params: ReconcileParams, execution_context=None):
        # The §0 fix (2026-08-25): the ledger's writes were correct and its
        # RECALL was broken — runs answered from a stale or partial read and
        # nothing ever compared ledger to chain. This verb is that comparison,
        # server-side, so no step budget, context compaction, or partial file
        # read can quietly turn "three positions" into "book flat".
        import os as _os
        from pathlib import Path
        from tools.defi import reconcile as rec

        chain = params.chain
        if chain not in SUPPORTED_CHAINS:
            return self._ar(error=(
                f"chain {chain!r} is not supported (this tier covers "
                f"{', '.join(SUPPORTED_CHAINS)})"))
        from core.wallet import chains
        row = chains.get(chain)
        svm = row is not None and row.family == "svm"
        # Solana is reconciled through the SAME comparison, not sent back to the
        # agent to "compare by hand" — hand-comparison from memory is exactly
        # the recall failure this verb was built to replace (2026-08-25), and
        # excluding a chain the agent can trade on left that book exposed to the
        # identical incident.
        holder = self._solana_holder() if svm else self._resolve_holder()
        if not holder:
            return self._ar(error=(
                "agent wallet not enabled (set AGENT_WALLET_ENABLED=true) — "
                "no address to reconcile against"))

        path = Path(params.ledger_path).expanduser()
        if not path.is_absolute():
            # A relative ledger_path must resolve the SAME way filesystem_read_file/
            # write_file do (tools/filesystem.py, via execution_context.workspace_dir)
            # — otherwise a bare filename that the agent just wrote/read successfully
            # through the filesystem tool 404s here against the service's own cwd
            # instead of the session/project workspace, forcing a wasted retry with
            # the absolute path on every run.
            base = getattr(execution_context, "workspace_dir", None) or _os.getcwd()
            path = Path(base) / path
        try:
            real = path.resolve()
        except OSError as exc:
            return self._ar(error=f"cannot resolve {params.ledger_path}: {exc}")
        allowed = [Path.cwd().resolve()]
        try:
            from core.runtime_paths import resolve_data_home
            allowed.append(resolve_data_home().resolve())
        except Exception:
            pass
        if not any(real == root or str(real).startswith(str(root) + _os.sep)
                   for root in allowed):
            return self._ar(error=(
                f"{params.ledger_path} is outside the agent's data home and "
                f"working directory — refusing to read it"))
        try:
            from core.security.secret_guard import is_credential_file
            if is_credential_file(real):
                return self._ar(error=(
                    "that path is a credential file, not a ledger — refused"))
        except ImportError:
            pass
        if not real.is_file():
            return self._ar(error=f"ledger not found: {params.ledger_path}")
        if real.stat().st_size > 1_000_000:
            return self._ar(error=(
                "that file exceeds 1MB — not a position ledger; point this at "
                "the ledger markdown itself"))
        text = real.read_text(encoding="utf-8", errors="replace")

        rows, parse_err = rec.parse_open_positions(text)
        if parse_err:
            return self._ar(error=parse_err)

        # Chain side: the SAME seams `portfolio` uses. Under partial scan
        # coverage every EVM ledger address is added to the scan set, so a
        # ledger row always gets a direct read — absence can then only mean
        # "the complete index did not see it", i.e. a genuine zero.
        if svm:
            # getTokenAccountsByOwner enumerates every SPL balance directly, so
            # coverage is COMPLETE here with no indexer key. A None is a FAILED
            # read, never an empty wallet: reporting it as zero would turn "I
            # could not look" into "the position is gone", which is the precise
            # collapse reconcile exists to catch.
            enumerated = self._solana_tokens(holder)
            if enumerated is None:
                raw = {r.address: None for r in rows}
                coverage = ("UNAVAILABLE — the token-account read failed. Every "
                            "row below is unverified; this is NOT a zero book.")
            else:
                raw = dict(enumerated)
                for r in rows:
                    raw.setdefault(r.address, 0)
                coverage = ("complete (enumerated from the chain; no indexer "
                            "key needed here)")
            return self._render_reconcile(rows, raw, chain=chain, row=row,
                                          holder=holder, ledger_path=str(real),
                                          coverage=coverage)
        indexed = None
        if self._index_fn is not None:
            indexed = self._index_fn(holder, chain=chain)
        elif alchemy_index.available():
            indexed = alchemy_index.fetch_balances(holder, chain=chain)
        if indexed is not None:
            raw = {a: v for a, v in indexed.items()}
            coverage = "indexed (alchemy) — complete"
        else:
            scan = list(dict.fromkeys(
                self._scan_set(chain)
                + [r.address for r in rows if r.address.startswith("0x")]))
            raw = self._balances(holder, chain, scan)
            coverage = (f"partial — {len(scan)} address(es) scanned (no "
                        f"ALCHEMY_API_KEY; a token outside this set is "
                        f"invisible here, but every ledger row was read "
                        f"directly)")

        return self._render_reconcile(rows, raw, chain=chain, row=row,
                                      holder=holder, ledger_path=str(real),
                                      coverage=coverage)

    def _render_reconcile(self, rows, raw, *, chain, row, holder, ledger_path,
                          coverage):
        """Value the chain side and diff it against the ledger. Family-agnostic
        on purpose: only the BALANCE SOURCE differs between EVM and Solana, and
        duplicating the valuation would let the two drift."""
        from tools.defi import reconcile as rec
        holdings = []
        for addr, units in sorted((raw or {}).items()):
            ident = self._identity(chain, addr)
            qty = value = None
            if units is not None and getattr(ident, "decimals", None) is not None:
                qty = units / (10 ** ident.decimals)
                info = self._price_for(chain, addr)
                if (getattr(info, "price_usd", None) is not None
                        and getattr(info, "confidence", None) == "high"):
                    value = qty * info.price_usd
            holdings.append(rec.ChainHolding(
                address=addr, symbol=getattr(ident, "symbol", None), qty=qty,
                raw_units=units, value_usd=value,
                balance_known=units is not None))

        quote_addresses = [a for a in (
            row.usdc if row else None,
            row.wrapped_native if row else None) if a]
        # `evm_chain` decides which address FAMILY counts as this chain's rows;
        # leaving it True on Solana filed every base58 row as "other chain
        # family (not checked here)" and rendered a CLEAN verdict over an
        # unreconciled book — a false all-clear, the worst possible output here.
        report = rec.diff(rows, holdings, quote_addresses=quote_addresses,
                          evm_chain=(row is None or row.family == "evm"))
        return self._ar(content=rec.render(
            report, chain=chain, holder=holder, ledger_path=ledger_path,
            coverage=coverage))

    @BaseTool.action(
        "List the POOLS most recently indexed on a chain — the fresh-launch "
        "frontier. Returns places to look, NOT vetted tokens: nothing here is "
        "screened, and most minutes-old pools go to zero. Screen every "
        "candidate with token_info (honeypot + sell tax) before acting on it.",
        param_model=DiscoverParams)
    async def new_pools(self, params: DiscoverParams, execution_context=None):
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
                f"  {pool.name}\n"
                f"    token: {token}\n"
                f"    dex: {pool.dex}   created: {pool.created_at or 'unknown'}\n"
                f"    liquidity: {_fmt_usd(pool.liquidity_usd)}   "
                f"vol 24h: {_fmt_usd(pool.volume_h24_usd)}   "
                f"24h: {'unknown' if pool.price_change_h24_pct is None else f'{pool.price_change_h24_pct:+.1f}%'}")
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
        """Holdings on a Solana-family chain (Phase 2).

        Deliberately its own method rather than a branch inside the EVM one:
        the enumeration is a different RPC, the gas asset is SOL rather than a
        wrapped native, and — the parity win — it needs NO indexer key, because
        `getTokenAccountsByOwner` returns every SPL balance directly. Coverage
        is therefore COMPLETE here, where the EVM path has to say "partial"
        without Alchemy.
        """
        from core.wallet import solana_onchain
        holder = self._solana_holder()
        if not holder:
            return self._ar(error=(
                "the agent wallet has no Solana address — set "
                "AGENT_WALLET_ENABLED=true with a BIP-39 master seed. Reads "
                "that need no address (token_info, price, new_pools, trending) "
                "work on solana regardless."))

        native = self._solana_native(holder)
        raw = self._solana_tokens(holder)
        lines = [f"holdings for {holder} (chain {params.chain})"]
        if raw is None:
            # UNKNOWN, never an empty wallet. See solana_onchain.token_balances.
            lines += ["coverage: UNAVAILABLE — the token-account read failed. "
                      "This is NOT 'you hold no tokens'; nothing below is a "
                      "complete picture.", ""]
        else:
            lines += ["coverage: complete (enumerated from the chain; no "
                      "indexer key needed here)", ""]
        if native is None:
            lines.append("  gas (SOL): balance UNKNOWN — the read failed. This "
                         "is NOT a zero.")
        elif native == 0:
            lines.append("  gas (SOL): 0 — no SOL. Every Solana transaction "
                         "pays a fee in SOL, and a first-time token transfer "
                         "also funds rent, so nothing can be sent from here.")
        else:
            lines.append(f"  gas (SOL): {native:,.9f} SOL — pays fees and "
                         f"account rent. Not a position.")
        lines.append("")

        valued, unvalued, total = [], [], 0.0
        for mint, units in sorted((raw or {}).items()):
            info = self._price_for(params.chain, mint)
            ident = self._identity(params.chain, mint)
            decimals = ident.decimals
            amount = units / (10 ** decimals) if decimals is not None else None
            shown = f"{amount:,.6f}" if amount is not None else f"{units} raw units"
            if info.price_usd is None or info.confidence != "high" or amount is None:
                unvalued.append(f"  {mint}  {shown}  value EXCLUDED "
                                f"({info.confidence} confidence price)")
                continue
            value = amount * info.price_usd
            total += value
            valued.append(f"  {mint}  {shown}  = {_fmt_usd(value)}")
        lines += valued or ["  (no confidently-valued holdings)"]
        lines += ["", f"total (confidently valued only): {_fmt_usd(total)}"]
        if unvalued:
            lines += ["", "unvalued — NOT necessarily holdings you bought:",
                      "  A token can sit at this address because someone sent "
                      "it to you. Solana airdrop spam is rife and the sell path "
                      "is where drainers live. Treat anything here as noise to "
                      "report, never as a position to realize.", ""] + unvalued
        lines += ["", "NOTE: this verb only reads. Selling goes through "
                      "defi_trade.solana_swap (guarded, dry_run by default), "
                      "and only when SOLANA_TRADE_ENABLED is armed."]
        return self._ar(content="\n".join(lines))

    def _solana_holder(self) -> Optional[str]:
        if self._holder != "__from_wallet__":
            return None if self._holder is None else str(self._holder)
        try:
            from core.wallet.factory import get_agent_wallet
            wallet = get_agent_wallet()
            return None if wallet is None else wallet.solana_address
        except Exception:
            return None

    def _solana_native(self, holder: str):
        from core.wallet import solana_onchain
        try:
            return solana_onchain.native_balance(holder)
        except Exception:
            return None

    def _solana_tokens(self, holder: str):
        from core.wallet import solana_onchain
        try:
            return solana_onchain.token_balances(holder)
        except Exception:
            return None

    def _gas_lines(self, holder: str, chain: str) -> List[str]:
        """The native-balance row. NEVER omitted (proposal 029 R2).

        This block exists because of a live failure, not a feature request. The
        prod agent escalated "no ETH row in the portfolio — the gas tank may be
        empty" ten times over two weeks while the wallet held ~1,130 swaps worth
        of gas. `portfolio` enumerated ERC-20s only, so a MISSING row read as an
        EMPTY tank, and the agent (correctly, given what it could see) treated
        every exit as gas-blocked.

        So the row is unconditional, and the three states are kept apart in
        words: a number, "UNKNOWN" (read failed), and "EMPTY" (a real zero, the
        only one of the three that actually blocks a broadcast). Gas is a fee
        reserve rather than a position, so it is deliberately NOT summed into
        the holdings total.
        """
        from core.wallet import chains
        row = chains.get(chain)
        symbol = row.native_symbol if row else "native"
        try:
            native = self._native_balance(holder, chain)
        except Exception as exc:                       # fail-open: lose the row, not the report
            logger.debug("portfolio: native balance read failed on %s (%s)", chain, exc)
            native = None
        if native is None:
            return [f"  gas ({symbol}): balance UNKNOWN — the read failed. "
                    f"This is NOT a zero — never read a failed read as an unfunded tank."]
        if native == 0:
            return [f"  gas ({symbol}): 0 — the gas tank is EMPTY. No transaction can "
                    f"broadcast on {chain}, entries and EXITS alike, until it is funded."]
        return [f"  gas ({symbol}): {native:,.6f} {symbol} — pays fees on {chain}. "
                f"Not a position and not counted in the total below."]

    @BaseTool.action(
        "Raw read-only eth_call against a contract. Returns the raw hex; any "
        "decoding is best-effort and unverified. No state change.",
        param_model=ContractReadParams)
    async def contract_read(self, params: ContractReadParams, execution_context=None):
        address, err = self._validate(params.chain, params.address)
        if err:
            return self._ar(error=err)
        try:
            raw = self._eth_call(chain=params.chain, address=address,
                                 signature=params.signature, args=params.args)
        except Exception as exc:
            return self._ar(error=f"contract_read failed: {exc}")
        if raw is None:
            return self._ar(content=f"{address}.{params.signature}: no result (call reverted or failed)")

        truncated = False
        if len(raw) > 2 + CONTRACT_READ_MAX_BYTES * 2:
            raw = raw[:2 + CONTRACT_READ_MAX_BYTES * 2]
            truncated = True

        lines = [f"{address}.{params.signature}",
                 f"  raw: {raw}"]
        if truncated:
            lines.append(f"  truncated: true (capped at {CONTRACT_READ_MAX_BYTES} bytes)")
        decoded = self._best_effort_decode(raw)
        if decoded is not None:
            lines.append(f"  decoded (best-effort, UNVERIFIED — the interpretation may be "
                         f"wrong if the signature does not match the contract): {decoded}")
        return self._ar(content="\n".join(lines))

    # -- helpers ----------------------------------------------------------
    def _scan_set(self, chain: str) -> List[str]:
        """Canonical list ∪ every address already in the token cache.

        A superset is harmless: scanning an address never held costs one
        multicall slot and returns zero.
        """
        from core.wallet.tokens import CANONICAL_TOKENS
        out = {addr for (c, addr) in CANONICAL_TOKENS if c == chain}
        try:
            from core import sqlite_util
            from core.wallet.tokens import _db, _ensure_schema
            path = _db(None)
            _ensure_schema(path)
            conn = sqlite_util.wal_connect(path)
            try:
                for (addr,) in conn.execute(
                        "SELECT address FROM tokens WHERE chain = ?", (chain,)):
                    out.add(addr)
            finally:
                conn.close()
        except Exception:
            logger.debug("scan set: token cache unavailable", exc_info=True)
        return sorted(out)

    def _eth_call(self, *, chain: str, address: str, signature: str, args) -> Optional[str]:
        if self._call_fn is not None:
            return self._call_fn(chain=chain, address=address, signature=signature, args=args)
        from eth_utils import keccak
        from core.wallet.onchain import _rpc, rpc_url_for_chain
        selector = "0x" + keccak(text=signature.replace(" ", "")).hex()[:8]
        return _rpc(rpc_url_for_chain(chain), "eth_call",
                    [{"to": address, "data": selector, "gas": hex(CONTRACT_READ_GAS_CAP)},
                     "latest"], 8.0)

    @staticmethod
    def _best_effort_decode(raw: str) -> Optional[str]:
        if not raw or raw == "0x":
            return None
        body = raw[2:]
        if len(body) == 64:
            try:
                return str(int(body, 16))
            except ValueError:
                return None
        return None
