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


class ContractReadParams(BaseModel):
    chain: str = Field("base", description=_chain_field("read from"))
    address: str = Field(..., description="Contract address to read from (0x…)")
    signature: str = Field(..., description="Function signature, e.g. 'totalSupply()'")
    args: List[Any] = Field(default_factory=list, description="Arguments (currently unencoded; prefer no-arg views)")


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
        if self._identity_fn:
            return self._identity_fn(chain, address)
        from core.wallet.tokens import get_token_identity
        return get_token_identity(chain, address)

    def _balances(self, holder: str, chain: str, tokens: List[str]) -> Dict[str, Optional[int]]:
        if self._balances_fn:
            return self._balances_fn(holder, chain, tokens)
        from core.wallet.onchain import token_balances
        return token_balances(holder, chain, tokens)

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
        """(checksummed_address, error_message)."""
        if chain not in SUPPORTED_CHAINS:
            return None, f"chain {chain!r} is not supported (this tier covers {', '.join(SUPPORTED_CHAINS)})"
        from core.wallet.tokens import normalize_address
        try:
            return normalize_address(address), None
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
        return self._ar(content="\n".join(lines))

    @BaseTool.action(
        "Price a SWAP before committing to one: best Uniswap V3 fee tier and the "
        "output you would receive. Read-only — quotes nothing on-chain and moves "
        "no funds. Both sides must be CONTRACT ADDRESSES.",
        param_model=SwapQuoteParams)
    async def swap_quote(self, params: SwapQuoteParams, execution_context=None):
        from tools.defi.providers import univ3
        from core.wallet.tokens import get_token_identity

        addr_in, err = self._validate(params.chain, params.token_in)
        if err:
            return self._ar(error=err)
        addr_out, err = self._validate(params.chain, params.token_out)
        if err:
            return self._ar(error=err)
        if addr_in.lower() == addr_out.lower():
            return self._ar(error="token_in and token_out are the same token")

        id_in = get_token_identity(params.chain, addr_in)
        id_out = get_token_identity(params.chain, addr_out)
        if id_in.decimals is None or id_out.decimals is None:
            return self._ar(error=(
                "one side does not report decimals — refusing to size a quote "
                "against a token whose denomination is unknown"))

        amount_in_raw = int(round(params.amount_in * (10 ** id_in.decimals)))
        quote = univ3.best_quote(params.chain, addr_in, addr_out, amount_in_raw)
        if quote is None:
            return self._ar(content=(
                f"no Uniswap V3 route for {id_in.symbol or addr_in} -> "
                f"{id_out.symbol or addr_out} at any fee tier. "
                f"No route is UNKNOWN, not a zero-value trade."))

        out_human = quote.amount_out_raw / (10 ** id_out.decimals)
        rate = out_human / params.amount_in if params.amount_in else 0
        return self._ar(content=(
            f"{params.amount_in} {id_in.symbol or addr_in} -> "
            f"{out_human:.8f} {id_out.symbol or addr_out}\n"
            f"  route:  Uniswap V3, fee tier {quote.fee_tier}\n"
            f"  rate:   {rate:.8f} {id_out.symbol or 'out'} per "
            f"{id_in.symbol or 'in'}\n"
            f"  router: {quote.router}\n"
            f"  NOTE: a quote is what the pool says right now, not a promise. "
            f"defi_trade.swap bounds it with slippage before executing."))

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

        valued, unvalued, total = [], [], 0.0
        for addr, units in sorted(raw.items()):
            if units is None:
                unvalued.append(f"  {addr}  balance UNKNOWN (read failed — not zero)")
                continue
            ident = self._identity(chain, addr)
            if ident.decimals is None:
                unvalued.append(f"  {addr}  {units} raw units (decimals unknown — cannot value)")
                continue
            amount = units / (10 ** ident.decimals)
            info = self._price_for(chain, addr)
            if info.price_usd is None or info.confidence != "high":
                unvalued.append(
                    f"  {addr}  {amount:,.6f} {ident.symbol or '?'}  "
                    f"value EXCLUDED ({info.confidence} confidence price)")
                continue
            value = amount * info.price_usd
            total += value
            valued.append(f"  {addr}  {amount:,.6f} {ident.symbol or '?'}  = {_fmt_usd(value)}")

        lines = [f"holdings for {holder} (chain {chain})",
                 f"coverage: {coverage}{scanned_note}", ""]
        lines += valued or ["  (no confidently-valued holdings)"]
        lines += ["", f"total (confidently valued only): {_fmt_usd(total)}"]
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
