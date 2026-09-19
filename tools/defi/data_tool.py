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


class OhlcvParams(BaseModel):
    chain: str = Field("base", description=_chain_field("read candles on"))
    address: str = Field(..., description=(
        "The TOKEN contract address. Candles are POOL-scoped on this indexer, so "
        "the deepest pool for this token is resolved and named in the answer."))
    pool: Optional[str] = Field(None, description=(
        "Read this exact POOL instead of resolving one. Use it when you already "
        "know which pool you mean — a token's pools disagree."))
    timeframe: str = Field("hour", description=(
        "Candle window: 'day', 'hour' or 'minute'. Use 'day' to see whether a "
        "token climbed over weeks, 'hour' to see whether it went vertical."))
    aggregate: int = Field(1, ge=1, le=60, description=(
        "Bars to merge per candle, e.g. timeframe='minute' aggregate=15."))
    limit: int = Field(48, ge=1, le=300, description="How many candles to return.")


class ScanParams(BaseModel):
    chain: str = Field("base", description=_chain_field("scan pools on"))
    kind: str = Field("trending", description=(
        "'trending' (what the indexer currently ranks) or 'new' (the "
        "fresh-launch frontier). Trending is a popularity claim and popularity "
        "is purchasable; new is unproven by definition."))
    limit: int = Field(20, ge=1, le=50, description="How many pools to classify.")
    min_liquidity_usd: float = Field(0.0, ge=0.0, description=(
        "Exclude pools whose reported liquidity is BELOW this. A pool whose "
        "liquidity is UNKNOWN is never excluded — unknown is not a number."))


class ContractReadParams(BaseModel):
    chain: str = Field("base", description=_chain_field("read from"))
    address: str = Field(..., description="Contract address to read from (0x…)")
    signature: str = Field(..., description="Function signature, e.g. 'totalSupply()'")
    args: List[Any] = Field(default_factory=list, description="Arguments (currently unencoded; prefer no-arg views)")


class NftHoldingsParams(BaseModel):
    chain: str = Field("base", description=_chain_field("list NFT holdings on"))
    address: Optional[str] = Field(
        None, description="Address to inspect. Defaults to this agent's own wallet.")


class NftInfoParams(BaseModel):
    chain: str = Field("base", description=_chain_field("read the token on"))
    contract: str = Field(..., description="The NFT collection contract (0x…)")
    token_id: int = Field(..., ge=0, description="The token id to read.")


class LpPositionsParams(BaseModel):
    chain: str = Field("robinhood", description=_chain_field("read Uniswap v3 positions on"))
    protocol: str = Field("v3", description="Uniswap protocol: v3 (v4 in a later phase).")
    address: Optional[str] = Field(None, description="Owner address; defaults to the agent wallet.")


class LpPoolInfoParams(BaseModel):
    chain: str = Field("robinhood", description=_chain_field("read a Uniswap v3 pool on"))
    protocol: str = Field("v3", description="v3 only for now.")
    pool: Optional[str] = Field(None, description="Pool address, OR give token_a+token_b+fee.")
    token_a: Optional[str] = None
    token_b: Optional[str] = None
    fee: Optional[int] = Field(None, description="Fee tier in hundredths of a bip: 100, 500, 3000, 10000.")


class LpQuoteParams(BaseModel):
    chain: str = Field("robinhood", description=_chain_field("quote a Uniswap v3 deposit on"))
    protocol: str = Field("v3", description="v3 only for now.")
    token_a: str
    token_b: str
    fee: int = Field(3000, description="100 | 500 | 3000 | 10000")
    amount_a: Optional[float] = Field(None, description="Human units of token_a to deposit (give one or both).")
    amount_b: Optional[float] = None
    range: str = Field("full", description="'full' or 'price_lo,price_hi' in token_b per token_a.")
    initial_price: Optional[float] = Field(
        None, description="token_b per token_a — REQUIRED when the pool does not exist yet.")


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


def _is_finite(value) -> bool:
    """False for None, NaN and ±inf — the three values that are not a figure.

    A feed that yields NaN once will render it everywhere a number goes unless
    every formatter agrees to call it what it is.
    """
    try:
        import math
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


# --- quote cross-check (2026-09-19) ------------------------------------------
# A route quote is INPUT to the swap rail's min-out floor. On 2026-09-19 the
# `lifi:fly` route answered ~4.1 WETH for four unrelated small tokens on a cold
# first response, and the EXIT rail burned steps re-quoting "implausible"
# numbers by eye. The keyless indexer price both sides already carry gives an
# implied output; when the route disagrees by more than QUOTE_SUSPECT_RATIO
# either way the quote is SUSPECT. Fail-open: no price, zero price, or a
# pool with no real liquidity (a liquidity:0 pool once priced a token at
# 1e35) ⇒ "unavailable", never a verdict.
QUOTE_SUSPECT_RATIO = 5.0


class QuoteCrossCheck:
    __slots__ = ("verdict", "ratio", "implied_out", "why")

    def __init__(self, verdict: str, ratio: Optional[float], implied_out: Optional[float], why: str):
        self.verdict = verdict          # "consistent" | "suspect" | "unavailable"
        self.ratio = ratio              # quoted / implied
        self.implied_out = implied_out
        self.why = why


def _usable_price(info) -> Optional[float]:
    try:
        usd = float(getattr(info, "price_usd", None) or 0)
        liq = getattr(info, "liquidity_usd", None)
    except (TypeError, ValueError):
        return None
    if usd <= 0 or liq is None or float(liq) <= 0:
        return None
    return usd


def quote_cross_check(amount_in: float, quoted_out: float, price_in, price_out) -> QuoteCrossCheck:
    """Compare a route's output with the indexer-implied output. Pure."""
    p_in = _usable_price(price_in)
    p_out = _usable_price(price_out)
    if p_in is None or p_out is None:
        missing = "token_in" if p_in is None else "token_out"
        return QuoteCrossCheck("unavailable", None, None,
                               f"no indexed price with real liquidity for {missing}")
    implied = amount_in * p_in / p_out
    if implied <= 0 or quoted_out <= 0:
        return QuoteCrossCheck("unavailable", None, implied, "non-positive side")
    ratio = quoted_out / implied
    if ratio > QUOTE_SUSPECT_RATIO or ratio < 1.0 / QUOTE_SUSPECT_RATIO:
        return QuoteCrossCheck("suspect", ratio, implied,
                               f"route output is {ratio:.1f}x the indexer-implied output")
    return QuoteCrossCheck("consistent", ratio, implied,
                           f"within {ratio:.2f}x of the indexer-implied output")


def _fmt_usd(value: Optional[float]) -> str:
    """Money, with a real sub-cent figure kept instead of rounded to zero.

    A memecoin trades at 1e-5 to 1e-8 almost by definition, so the plain
    two-decimal money format turned nearly every token this path exists to
    assess into "$0.00" — a real price, with earned confidence, rendered as the
    one number that means "worthless". Observed live on a Robinhood token, and
    already filed by the agent as a token property ("price feed shows $0.00
    (edge case)") rather than as the formatting bug it is.

    A TRUE zero still renders "$0.00", and unknown still renders the word.
    """
    if value is None or not _is_finite(value):
        # NaN IS the canonical "not a number". Rendering it `$nan` puts a
        # non-figure where a figure goes; an infinity is no better.
        return "unknown"
    if value == 0:
        return "$0.00"          # also normalises -0.0
    if abs(value) < 0.01:
        import math
        # Four significant digits, written out. Decimal rather than scientific
        # because the figure is compared by eye against a ledger and an
        # explorer, both of which write it out.
        places = min(18, 3 - int(math.floor(math.log10(abs(value)))))
        text = f"{value:,.{places}f}".rstrip("0").rstrip(".")
        if float(text.replace(",", "")) == 0:
            # Below what 18 places can show. Writing it out would strip back to
            # "0" — the confident zero this whole branch exists to remove — so
            # the compact form is the honest one.
            return f"${value:.4g}"
        return f"${text}" if not text.startswith("-") else f"-${text[1:]}"
    return f"${value:,.2f}"


def _fmt_pct(fraction: Optional[float]) -> str:
    """A FRACTION (0.30) as a percentage. Unknown stays the word, never 0%:
    "this wallet holds 0%" and "the screener did not say" are different facts."""
    return "unknown" if not _is_finite(fraction) else f"{fraction * 100:.1f}%"


def _fmt_count(value: Optional[int]) -> str:
    return "unknown" if value is None else f"{value:,}"


def _fmt_ratio(value: Optional[float], places: int = 1) -> str:
    return "unknown" if not _is_finite(value) else f"{value:,.{places}f}"


def _fmt_hours(hours: Optional[float]) -> str:
    if not _is_finite(hours):
        return "unknown"
    if hours < 48:
        return f"{hours:.0f}h"
    return f"{hours / 24:.0f}d"


#: "no cap" sentinel for a single-sided liquidity quote (proposal 048 P12) —
#: astronomically larger than any real deposit, so it is never the binding
#: side of `univ3_math.liquidity_for_amounts` when only one amount is given.
_LP_MAX_RAW = 2 ** 128 - 1

#: The one honest refusal for every LP read action until phase 3 of 048 ships
#: Uniswap v4 support — never a stub answer for a protocol this tier cannot
#: read yet.
_LP_V4_NOT_YET = "Uniswap v4 reads land in phase 3 of proposal 048; v3 only today"


def _lp_read_error_text(exc: Exception) -> str:
    """The one wording every LP action uses when `lp_reads.LpReadError` is
    raised — a failed chain read, never a silently-guessed 0/[]/None."""
    return f"the position read could not be completed ({exc}) — unknown is not zero"


def _lp_number_text(value: Optional[float]) -> str:
    """A plain figure (a price, an implied ratio) at up to 8 significant
    decimals — mirrors `_fmt_usd`'s refusal to round a real nonzero value to
    a confident 0, without `_fmt_usd`'s `$` framing (an LP figure is not
    always USD)."""
    if value is None or not _is_finite(value):
        return "unknown"
    if value == 0:
        return "0"
    text = f"{value:,.8f}".rstrip("0").rstrip(".")
    if text in ("", "-", "0", "-0"):
        import math
        places = min(24, 7 - int(math.floor(math.log10(abs(value)))))
        text = f"{value:.{places}f}".rstrip("0").rstrip(".")
    return text or "0"


def _lp_amount_text(raw: Optional[int], dec: Optional[int]) -> str:
    """Raw base units -> human units, at up to 8 significant decimals."""
    if raw is None or dec is None:
        return "unknown"
    return _lp_number_text(raw / (10 ** dec))


def _lp_invert_if_flipped(price: float, flipped: bool) -> float:
    """token_b-per-token_a <-> token1-per-token0 (or the reverse — an
    involution, since inverting twice is a no-op). `flipped` is
    `univ3_math.sort_tokens(token_a, token_b)`'s third element: True means
    token_a is the numerically HIGHER address, so it became token1."""
    return (1.0 / price) if flipped else price


def _render_lp_position(pv, *, sym0: str, sym1: str) -> List[str]:
    fee_pct = pv.fee / 1e4
    state = "IN RANGE" if pv.in_range else "OUT OF RANGE"
    return [
        f"#{pv.token_id} pool {pv.pool} {sym0}/{sym1} fee {fee_pct:g}% "
        f"ticks [{pv.tick_lower},{pv.tick_upper}] {state}",
        f"  holds:  {_lp_amount_text(pv.amount0, pv.dec0)} {sym0} + "
        f"{_lp_amount_text(pv.amount1, pv.dec1)} {sym1}",
        f"  fees:   {_lp_amount_text(pv.fees0, pv.dec0)} {sym0} + "
        f"{_lp_amount_text(pv.fees1, pv.dec1)} {sym1} uncollected",
    ]


def _fmt_trade_shape(pool) -> str:
    """The separators, on one line, from fields the indexer already sent.

    These are the measured discriminators between a token that survived and one
    that was wash-traded into a chart: volume over liquidity, hourly volume over
    liquidity (a wash in progress that the 24h figure smears out), transactions
    per UNIQUE buyer, and market cap against liquidity. Every one renders
    "unknown" rather than a number it cannot compute.
    """
    trades = getattr(pool, "trades_h24", None)
    buyers = "unknown" if trades is None or trades.buyers is None else f"{trades.buyers:,}"
    buys = "unknown" if trades is None or trades.buys is None else f"{trades.buys:,}"
    sells = "unknown" if trades is None or trades.sells is None else f"{trades.sells:,}"
    return (f"V/L {_fmt_ratio(getattr(pool, 'vol_liq_ratio', None))}   "
            f"V/L 1h {_fmt_ratio(getattr(pool, 'hourly_vol_liq_ratio', None), 2)}   "
            f"mcap {_fmt_usd(getattr(pool, 'market_cap_usd', None))} "
            f"(mcap/liq {_fmt_ratio(getattr(pool, 'mcap_liq_ratio', None))})   "
            f"unique buyers 24h {buyers}   buys/sells {buys}/{sells}   "
            f"txns per buyer {_fmt_ratio(getattr(pool, 'txns_per_buyer', None))}")


#: Worst news first. A scan is read top-down and the rows that cost money are
#: the ones that must not be below the fold.
_SCAN_ORDER = ("SURVIVOR", "CANDIDATE", "TOO_NEW", "PASS", "UNSCREENABLE", "WASH")


def _render_scan(rows, *, chain: str, kind: str, excluded: int, floor: float) -> List[str]:
    """Pure. Every classified pool, ordered by verdict, nothing dropped."""
    label = "newest" if kind == "new" else "trending"
    head = [
        f"{label} pools on {chain} — {len(rows)} classified",
        "",
        "A verdict here reads the MARKET (depth, flow, age, participation). It "
        "says NOTHING about the contract: honeypot, sell tax, mint authority and "
        "holder concentration are token_info and token_holders, and a SURVIVOR "
        "is not a safety claim.",
        "",
    ]
    if not rows:
        return head + [
            "  no pools were returned for this chain and window.",
            "  That is an empty answer from the indexer, not a verdict on the chain.",
        ]

    counts = {}
    for _, verdict in rows:
        counts[verdict.verdict] = counts.get(verdict.verdict, 0) + 1
    head.append("  " + "   ".join(
        f"{name} {counts[name]}" for name in _SCAN_ORDER if name in counts))
    stock = sum(1 for _, v in rows if v.stock_pair)
    if stock:
        head.append(f"  {stock} pool(s) quoted against a TOKENIZED STOCK — such a "
                    f"position is two bets stacked, the meme and the stock.")
    head.append("")

    order = {name: i for i, name in enumerate(_SCAN_ORDER)}
    lines = list(head)
    for pool, verdict in sorted(rows, key=lambda r: order.get(r[1].verdict, 99)):
        tag = "  [STOCK PAIR]" if verdict.stock_pair else ""
        lines.append(f"  {verdict.verdict}{tag}  {pool.name}")
        lines.append(f"      token: {pool.base_token or 'UNKNOWN — the indexer named a base token this chain does not accept'}")
        lines.append(f"      {_fmt_trade_shape(pool)}")
        lines.append(f"      age: {_fmt_hours(getattr(pool, 'age_hours', None))}"
                     f"   liquidity: {_fmt_usd(pool.liquidity_usd)}"
                     f"   vol 24h: {_fmt_usd(pool.volume_h24_usd)}")
        for reason in verdict.reasons:
            lines.append(f"      · {reason}")
        for unknown in verdict.unknowns:
            lines.append(f"      ? NOT CHECKED: {unknown}")
        lines.append("")
    if excluded:
        lines.append(f"  {excluded} pool(s) excluded by your liquidity floor "
                     f"({_fmt_usd(floor)}). Said out loud because a silent filter "
                     f"reads as 'this is all there was'.")
    return lines


def _render_candles(candles, summary, *, chain: str, token: str, pool: str,
                    timeframe: str, aggregate: int) -> List[str]:
    """Pure. The series plus the shape read; no candles is SAID, never drawn flat."""
    head = [
        f"price history for {token} (chain {chain})",
        f"  pool read: {pool}   window: {aggregate}x{timeframe}",
        "  ⚠ Candles are POOL-scoped. Another pool for the same token can show a "
        "different history; this is the one named above.",
        "",
    ]
    if not candles:
        return head + [
            "  NO CANDLES — the indexer returned no price history for this pool.",
            "  That is not a flat chart and not a price of zero; it is an absence "
            "of data, and for a pool minutes old it is the expected answer.",
        ]

    lines = head + [
        f"  {summary.count} candles   "
        f"first {_fmt_price(summary.first)} -> last {_fmt_price(summary.last)} "
        f"({_fmt_signed_pct(summary.pct_from_first)})",
        f"  peak {_fmt_price(summary.peak)} at candle {summary.candles_to_peak} of "
        f"{summary.count - 1}   now {_fmt_signed_pct(summary.pct_off_peak)} off peak"
        f"   trough {_fmt_price(summary.trough)}",
        f"  shape: {_candle_shape_note(summary)}",
        "",
        "  ts                    open        high         low       close      vol usd",
    ]
    for c in candles:
        from datetime import datetime, timezone
        when = datetime.fromtimestamp(c.timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M")
        lines.append(f"  {when}  {_fmt_price(c.open):>10} {_fmt_price(c.high):>11} "
                     f"{_fmt_price(c.low):>11} {_fmt_price(c.close):>11} "
                     f"{_fmt_usd(c.volume_usd):>13}")
    return lines


def _candle_shape_note(summary) -> str:
    """Name the shape without turning it into a verdict.

    Where the peak SITS in the series is the measured separator; this states it
    and stops. Deciding is the caller's job, with the screen and the holders.
    """
    if summary.peak_in_first_half is None or summary.pct_off_peak is None:
        return "unknown"
    if summary.peak_in_first_half and summary.pct_off_peak < -50:
        return ("peaked EARLY in this window and sits more than 50% below it — "
                "the round-trip shape, where the exit was the only trade")
    if summary.peak_in_first_half:
        return "peaked early in this window and has held most of it"
    if summary.pct_off_peak > -20:
        return "peaked LATE in this window and sits near that peak — still climbing"
    return "peaked late in this window and has pulled back from it"


def _fmt_price(value: Optional[float]) -> str:
    if value is None:
        return "unknown"
    if value == 0:
        return "0"
    return f"{value:,.8g}"


def _fmt_signed_pct(value: Optional[float]) -> str:
    return "unknown" if not _is_finite(value) else f"{value:+.1f}%"


def _render_holder_rows(rows, indent: str = "      ") -> List[str]:
    lines = []
    for row in rows:
        kind = ("contract" if row.is_contract is True
                else "wallet" if row.is_contract is False
                else "wallet-or-contract unknown")
        bits = [f"{_fmt_pct(row.percent)}", kind]
        if row.is_locked is True:
            bits.append("locked/burned")
        if row.tag:
            bits.append(f"tagged {row.tag!r}")
        lines.append(f"{indent}{row.address}  " + "  ".join(bits))
    return lines


def _render_holders(report, *, chain: str, address: str) -> List[str]:
    """Pure. The concentration read, with every unknown stated as unknown."""
    head = [f"holders of {address} (chain {chain})", ""]
    if not report.available:
        return head + [
            f"  UNAVAILABLE — {report.reason or 'the screener said nothing'}.",
            "  This is NOT a clean result and NOT evidence of a wide holder base: "
            "nobody reported the distribution, so it is unknown.",
        ] + _render_creator(report)

    lines = head + [
        f"  holder count: {_fmt_count(report.holder_count)}"
        f"   total supply: {'unknown' if report.total_supply is None else f'{report.total_supply:,.0f}'}",
        f"  top {len(report.top_holders)} reported hold {_fmt_pct(report.top_percent)} "
        f"of supply ({_fmt_pct(report.top_percent_wallets)} of it in non-contract wallets)",
    ]
    if report.top_holders:
        lines.append("  top holders:")
        lines += _render_holder_rows(report.top_holders)
    lines.append("")
    if report.lp_holders or report.lp_holder_count is not None:
        lines.append(f"  LP holders: {_fmt_count(report.lp_holder_count)}"
                     f"   LP supply: {'unknown' if report.lp_total_supply is None else f'{report.lp_total_supply:,.6f}'}"
                     f"   locked/burned share: {_fmt_pct(report.lp_locked_percent)}")
        lines += _render_holder_rows(report.lp_holders)
    else:
        lines.append("  LP holders: unknown — the screener reported none, which is "
                     "NOT the same as an unlocked pool")
    lines += _render_creator(report)
    lines += [
        "",
        "  ⚠ Concentration is a claim about ADDRESSES, not people. One holder can "
        "split across many addresses, and this source does not trace funding "
        "between them, so a flat-looking distribution can still be one party.",
    ]
    return lines


def _render_creator(report) -> List[str]:
    lines = []
    if report.creator_address:
        lines.append(f"  creator: {report.creator_address}  holds "
                     f"{_fmt_pct(report.creator_percent)}")
    if report.owner_address:
        lines.append(f"  owner:   {report.owner_address}  holds "
                     f"{_fmt_pct(report.owner_percent)}")
    if report.honeypot_with_same_creator is True:
        lines.append("  ⚠ FLAG: the same creator has already deployed a honeypot.")
    elif report.honeypot_with_same_creator is None:
        lines.append("  same-creator honeypot history: unknown (not checked here)")
    return lines


class DefiDataTool(BaseTool):
    """Read-only token sight. Seams are injectable so unit tests never hit the network."""

    def __init__(self, name: str = "defi_data", config=None, container=None, *,
                 search_fn: Optional[Callable] = None,
                 search2_fn: Optional[Callable] = None,
                 price_fn: Optional[Callable] = None,
                 screen_fn: Optional[Callable] = None,
                 holders_fn: Optional[Callable] = None,
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

    def _price_for(self, chain: str, address: str) -> PriceInfo:
        return (self._price_fn or dexscreener.token)(chain, address)

    def _screen_for(self, chain: str, address: str) -> ScreenVerdict:
        return (self._screen_fn or goplus.screen)(chain, address)

    def _holders_for(self, chain: str, address: str):
        return (self._holders_fn or goplus.holders)(chain, address)

    def _ohlcv_for(self, chain: str, pool: str, **kw):
        from tools.defi.providers import geckoterminal as _gt
        return (self._ohlcv_fn or _gt.ohlcv)(chain, pool, **kw)

    def _pool_for_token(self, chain: str, address: str):
        from tools.defi.providers import geckoterminal as _gt
        return (self._pool_for_token_fn or _gt.top_pool_for_token)(chain, address)

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
        cands, answered, failed = self._search_all(params.symbol)
        if not answered:
            return self._ar(error=(
                f"token_resolve failed: no index answered "
                f"({'; '.join(failed) or 'unknown reason'})"))

        searched = ", ".join(SUPPORTED_CHAINS)
        indexes = f"indexes asked: {', '.join(answered)}"
        if failed:
            indexes += f"; DID NOT ANSWER: {', '.join(failed)}"
        if not cands:
            return self._ar(content=(
                f"no candidates for ticker {params.symbol!r} on {searched}. "
                "Nothing was resolved.\n"
                f"{indexes}.\n"
                "NOTE: each index returns a ranked, capped result set, so this "
                "means 'not in the top results for this ticker' — NOT 'no such "
                "token exists'. If you know the contract address, use it "
                "directly with token_info."))

        lines = [
            f"{len(cands)} candidate contract(s) claim the ticker {params.symbol!r} "
            f"(searched: {searched}; {indexes}).",
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
            # A screener can cover a chain PARTIALLY. Rendering "N checks ran;
            # no risk flags raised" over a payload that never carried
            # is_honeypot makes a partial screen read exactly like a clean one,
            # which is the worst failure a screen has: it looks like safety.
            partial = bool(getattr(verdict, "missing", None))
            head = f"  screen:   {len(verdict.checks)} checks ran"
            if partial:
                head += " — PARTIAL"
            head += "; " + (f"FLAGS: {', '.join(verdict.flags)}" if verdict.flags
                            else ("no risk flags raised among the checks THAT RAN"
                                  if partial else "no risk flags raised"))
            lines.append(head)
            for k, v in sorted(verdict.checks.items()):
                lines.append(f"      {k} = {v}")
            if partial:
                lines.append(
                    f"      ⚠ {len(verdict.missing)} check(s) did NOT run on this "
                    f"chain: {', '.join(sorted(verdict.missing))}")
                lines.append(
                    "      A check that did not run is not a check that passed. "
                    "Do not read this as a clean screen.")
        else:
            lines.append("  screen:   unavailable — the screener returned nothing. "
                         "This is NOT a clean result; the token is UNSCREENED.")

        # Concentration, from the SAME screener answer. Cheap, and it is the
        # question a holder-cluster map is opened for.
        try:
            holders = self._holders_for(params.chain, address)
        except Exception:
            holders = None
        if holders is None or not getattr(holders, "available", False):
            reason = getattr(holders, "reason", None) if holders is not None else None
            lines.append(f"  holders:  unknown — {reason or 'no holder data was reported'}. "
                         "Unknown distribution is NOT a wide one.")
        else:
            lines.append(
                f"  holders:  {_fmt_count(holders.holder_count)} addresses; "
                f"top {len(holders.top_holders)} hold {_fmt_pct(holders.top_percent)} "
                f"({_fmt_pct(holders.top_percent_wallets)} in non-contract wallets)"
                + ("   ⚠ same creator has deployed a honeypot before"
                   if holders.honeypot_with_same_creator is True else "")
                + "   — run token_holders for the per-address breakdown")
        # What the ADDRESS itself does or does not prove. On EVM a mistyped
        # address would almost certainly have failed EIP-55; on Solana nothing
        # would have caught it, and carrying the EVM habit across is how funds
        # reach a real, wrong account.
        from core.wallet.addresses import typo_protection_note
        lines.append(f"  address:  {typo_protection_note(params.chain)}")
        return self._ar(content="\n".join(lines))

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
        if not pool:
            try:
                pool = self._pool_for_token(params.chain, address)
            except Exception as exc:
                return self._ar(error=f"could not resolve a pool for {address}: {exc}")
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
            aggregate=params.aggregate)))

    @BaseTool.action(
        "WHO owns this token: top holders with their share, wallet vs contract, "
        "LP holders and whether the LP is locked, and the creator's own stake. "
        "This is the concentration read — a holder-cluster map in text. It does "
        "NOT prove wallets are unrelated: one person can hold ten addresses, and "
        "nothing here traces funding between them.",
        param_model=TokenRefParams)
    async def token_holders(self, params: TokenRefParams, execution_context=None):
        address, err = self._validate(params.chain, params.address)
        if err:
            return self._ar(error=err)
        try:
            report = self._holders_for(params.chain, address)
        except Exception as exc:
            return self._ar(error=f"token_holders failed: {exc}")
        return self._ar(content="\n".join(
            _render_holders(report, chain=params.chain, address=address)))

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
        # 2026-09-19: a quote is a time-stamped observation. The history
        # compaction pass collapses byte-identical tool outputs into a
        # back-reference, so two re-quotes minutes apart that agreed became
        # "[duplicate of an earlier tool result]" and a fresh read was
        # indistinguishable from a suppressed call. Stamp the read time and
        # name both addresses so no two quotes are identical bytes.
        import time as _time
        from datetime import datetime as _dt, timezone as _tz
        _qt = float(getattr(route, "quoted_at", 0) or 0) or _time.time()
        _stamp = _dt.fromtimestamp(_qt, _tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Cross-check against the indexer-implied output (fail-open).
        try:
            xc = quote_cross_check(params.amount_in, out_human,
                                   self._price_for(params.chain, addr_in),
                                   self._price_for(params.chain, addr_out))
        except Exception as e:  # noqa: BLE001 — a read-side check never blocks the quote
            xc = QuoteCrossCheck("unavailable", None, None, f"price lookup failed: {e}")
        if xc.verdict == "suspect":
            head = (f"  ⚠ SUSPECT QUOTE — {xc.why} (implied "
                    f"{xc.implied_out:.8f} {id_out.symbol or 'out'}, quoted "
                    f"{out_human:.8f}, {xc.ratio:.1f}x). Re-quote before acting and "
                    f"do NOT use this number as a min-out floor.\n")
        elif xc.verdict == "consistent":
            head = f"  cross-check: consistent — {xc.why}\n"
        else:
            head = f"  cross-check: unavailable — {xc.why}\n"
        return self._ar(content=(
            f"{params.amount_in} {id_in.symbol or addr_in} -> "
            f"{out_human:.8f} {id_out.symbol or addr_out}\n"
            + head +
            f"  quoted_at: {_stamp}   pair: {addr_in} -> {addr_out}\n"
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
            coverage=coverage),
            metadata={"report": report.to_dict(), "coverage": coverage})

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
                f"    dex: {pool.dex}   created: {pool.created_at or 'unknown'}"
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
        "List the non-fungible tokens (NFTs) an address holds. Defaults to your "
        "own wallet. Read-only. ⚠️ If no enumeration provider is configured this "
        "says so — it never returns an empty list, because 'you own nothing' and "
        "'I could not look' are different answers.",
        param_model=NftHoldingsParams)
    async def nft_holdings(self, params: NftHoldingsParams, execution_context=None):
        from tools.defi import nft_verbs

        address = params.address
        if not address:
            try:
                from core.wallet.factory import get_agent_wallet
                w = get_agent_wallet()
                address = w.operational_signer().address if w else None
            except Exception:
                address = None
        if not address:
            return self._ar(error=(
                "no address given and this agent's own wallet could not be "
                "resolved (AGENT_WALLET_ENABLED)"))
        try:
            held = nft_verbs.enumerate_holdings(
                chain=params.chain, address=address, fetch=self._nft_fetch)
        except nft_verbs.EnumerationUnavailable as exc:
            # UNAVAILABLE is not EMPTY. Rendering [] here would read as a
            # confident "you hold nothing".
            return self._ar(error=str(exc))
        except Exception as exc:
            return self._ar(error=f"the holdings read failed: {exc}")
        if not held:
            return self._ar(content=(
                f"{address} on {params.chain}: the indexer returned no NFTs. "
                f"This IS an answer from a working read, not a failed one."))
        lines = [f"{address} on {params.chain} — {len(held)} NFT(s):"]
        for item in held[:50]:
            name = item.get("name") or "(unnamed)"
            std = item.get("standard") or "standard unknown"
            bal = f" x{item['balance']}" if item.get("balance") not in (None, "1") else ""
            lines.append(f"  {name} — {item['contract']} #{item.get('token_id')} "
                         f"[{std}]{bal}")
        if len(held) > 50:
            lines.append(f"  …and {len(held) - 50} more")
        return self._ar(content="\n".join(lines))

    @BaseTool.action(
        "Read one NFT straight from the chain: who owns it, which standard it "
        "implements, and its metadata URI. Keyless and exact. A field that the "
        "node did not answer is reported as NOT CHECKED, never as a default.",
        param_model=NftInfoParams)
    async def nft_info(self, params: NftInfoParams, execution_context=None):
        from core.wallet.onchain import _rpc, rpc_url_for_chain
        from tools.defi import nft_verbs

        contract, err = self._validate(params.chain, params.contract)
        if err:
            return self._ar(error=err)

        def _call(method, args, timeout=8.0):
            return _rpc(rpc_url_for_chain(params.chain), method, args, timeout)

        try:
            facts = nft_verbs.read_token_facts(
                _call, chain=params.chain, contract=contract,
                token_id=params.token_id)
        except Exception as exc:
            return self._ar(error=f"the token read failed: {exc}")
        lines = [f"{contract} #{params.token_id} on {params.chain}"]
        if facts.get("standard"):
            lines.append(f"  standard: {facts['standard']}")
        if facts.get("owner"):
            lines.append(f"  owner: {facts['owner']}")
        if facts.get("uri"):
            lines.append(f"  metadata: {facts['uri']}")
        if facts.get("not_checked"):
            # ⚠️ A check that did not run is not a check that passed.
            lines.append("  NOT CHECKED: " + "; ".join(facts["not_checked"]))
        return self._ar(content="\n".join(lines))

    # -- liquidity pools (proposal 048 P12) --------------------------------

    def _lp_rpc_for(self, chain: str) -> Callable:
        """The `rpc(method, params, timeout=8.0)` the LP reads call. Tests
        inject `lp_rpc` at construction; production builds one per chain,
        matching the existing `_eth_call` pattern."""
        if self._lp_rpc is not None:
            return self._lp_rpc
        from core.wallet.onchain import _rpc, rpc_url_for_chain
        return lambda method, params, timeout=8.0: _rpc(
            rpc_url_for_chain(chain), method, params, timeout)

    def _lp_symbol(self, chain: str, address: str) -> str:
        """Best-effort token symbol via the existing `_identity` seam — falls
        back to the raw address when the read is unavailable or fails, so a
        symbol lookup never blocks or breaks an LP render."""
        try:
            ident = self._identity(chain, address)
            return getattr(ident, "symbol", None) or address
        except Exception:
            return address

    @BaseTool.action(
        "List this address's Uniswap v3 liquidity positions: pool, range, "
        "whether it is in range right now, current holdings and uncollected "
        "fees. Defaults to your own wallet. Read-only. Uniswap v4 lands in a "
        "later phase.",
        param_model=LpPositionsParams)
    async def lp_positions(self, params: LpPositionsParams, execution_context=None):
        if params.protocol != "v3":
            return self._ar(error=_LP_V4_NOT_YET)
        address = params.address
        if not address:
            try:
                from core.wallet.factory import get_agent_wallet
                w = get_agent_wallet()
                address = w.operational_signer().address if w else None
            except Exception:
                address = None
        if not address:
            return self._ar(error=(
                "no address given and this agent's own wallet could not be "
                "resolved (AGENT_WALLET_ENABLED)"))

        from tools.defi import lp_reads

        rpc = self._lp_rpc_for(params.chain)
        try:
            ids = lp_reads.owned_position_ids(rpc, params.chain, address)
        except lp_reads.LpReadError as exc:
            return self._ar(error=_lp_read_error_text(exc))
        if not ids:
            return self._ar(content=(
                f"{address} on {params.chain}: no Uniswap v3 positions. This "
                f"IS an answer from a working read, not a failed one."))

        lines = [f"{address} on {params.chain} — {len(ids)} Uniswap v3 "
                 f"position(s):"]
        try:
            for token_id in ids:
                pv = lp_reads.position(rpc, params.chain, token_id)
                sym0 = self._lp_symbol(params.chain, pv.token0)
                sym1 = self._lp_symbol(params.chain, pv.token1)
                lines.extend(_render_lp_position(pv, sym0=sym0, sym1=sym1))
                from tools.defi.lp_valuation import value_lines
                lines.extend(value_lines(self, params.chain, pv, execution_context))
        except lp_reads.LpReadError as exc:
            return self._ar(error=_lp_read_error_text(exc))
        return self._ar(content="\n".join(lines))

    @BaseTool.action(
        "Read a Uniswap v3 pool's live state: tokens, fee tier, current tick, "
        "price and liquidity. Give a pool address, OR token_a + token_b + "
        "fee to locate one. A pair with no pool yet is reported, not "
        "invented. Read-only. Uniswap v4 lands in a later phase.",
        param_model=LpPoolInfoParams)
    async def lp_pool_info(self, params: LpPoolInfoParams, execution_context=None):
        if params.protocol != "v3":
            return self._ar(error=_LP_V4_NOT_YET)

        from tools.defi import lp_reads

        rpc = self._lp_rpc_for(params.chain)
        pool = params.pool
        if not pool:
            if not (params.token_a and params.token_b and params.fee):
                return self._ar(error=(
                    "give either pool, OR token_a + token_b + fee to locate "
                    "a Uniswap v3 pool."))
            try:
                pool = lp_reads.pool_address(
                    rpc, params.chain, params.token_a, params.token_b, params.fee)
            except lp_reads.LpReadError as exc:
                return self._ar(error=_lp_read_error_text(exc))
            if pool is None:
                return self._ar(content=(
                    f"no Uniswap v3 pool exists yet for {params.token_a}/"
                    f"{params.token_b} at fee {params.fee} on {params.chain} "
                    f"— this pool does not exist. Use lp_add with "
                    f"initial_price to create and seed one."))

        try:
            ps = lp_reads.pool_state(rpc, params.chain, pool)
        except lp_reads.LpReadError as exc:
            return self._ar(error=_lp_read_error_text(exc))

        sym0 = self._lp_symbol(params.chain, ps.token0)
        sym1 = self._lp_symbol(params.chain, ps.token1)
        fee_pct = ps.fee / 1e4
        inv_price = None if not ps.price0_in_1 else 1.0 / ps.price0_in_1
        lines = [
            f"pool {ps.pool} on {params.chain} — {sym0}/{sym1} fee {fee_pct:g}%",
            f"  tick: {ps.tick} (spacing {ps.tick_spacing})",
            f"  liquidity: {ps.liquidity:,}",
            f"  price: 1 {sym0} = {_lp_number_text(ps.price0_in_1)} {sym1}   "
            f"|   1 {sym1} = {_lp_number_text(inv_price)} {sym0}",
        ]
        return self._ar(content="\n".join(lines))

    @BaseTool.action(
        "Quote a Uniswap v3 liquidity deposit: given one or both token "
        "amounts and a price range, compute the liquidity this would mint "
        "and the exact paired amounts. Works against an existing pool (its "
        "live price is read) or a not-yet-created one (give initial_price). "
        "Read-only — never deposits. Uniswap v4 lands in a later phase.",
        param_model=LpQuoteParams)
    async def lp_quote(self, params: LpQuoteParams, execution_context=None):
        if params.protocol != "v3":
            return self._ar(error=_LP_V4_NOT_YET)

        from tools.defi import lp_abi as A
        from tools.defi import lp_reads
        from core.wallet import univ3_math as M

        if params.fee not in A.FEE_TIERS:
            return self._ar(error=(
                f"fee {params.fee} is not a Uniswap v3 tier — use one of "
                f"{', '.join(str(f) for f in sorted(A.FEE_TIERS))}."))
        if params.amount_a is None and params.amount_b is None:
            return self._ar(error=(
                "give amount_a and/or amount_b to quote a deposit."))

        rpc = self._lp_rpc_for(params.chain)
        token0, token1, flipped = M.sort_tokens(params.token_a, params.token_b)

        try:
            pool = lp_reads.pool_address(
                rpc, params.chain, params.token_a, params.token_b, params.fee)
        except lp_reads.LpReadError as exc:
            return self._ar(error=_lp_read_error_text(exc))

        if pool is not None:
            try:
                ps = lp_reads.pool_state(rpc, params.chain, pool)
            except lp_reads.LpReadError as exc:
                return self._ar(error=_lp_read_error_text(exc))
            sqrt_p, dec0, dec1 = ps.sqrt_price_x96, ps.dec0, ps.dec1
            pool_note = f"pool {pool}"
        else:
            if params.initial_price is None:
                return self._ar(error=(
                    f"no Uniswap v3 pool exists yet for {params.token_a}/"
                    f"{params.token_b} at fee {params.fee} on {params.chain} "
                    f"— initial_price (token_b per token_a) is REQUIRED to "
                    f"quote a deposit into a pool that has not been created."))
            if params.initial_price <= 0:
                return self._ar(error="initial_price must be greater than 0.")
            try:
                dec0 = lp_reads.decimals(rpc, params.chain, token0)
                dec1 = lp_reads.decimals(rpc, params.chain, token1)
            except lp_reads.LpReadError as exc:
                return self._ar(error=_lp_read_error_text(exc))
            price0_in_1 = _lp_invert_if_flipped(params.initial_price, flipped)
            sqrt_p = M.sqrt_price_from_price(price0_in_1, dec0, dec1)
            pool_note = "not-yet-created pool"

        spacing = A.FEE_TIERS[params.fee]
        if params.range == "full":
            tick_lower, tick_upper = M.full_range_ticks(spacing)
        else:
            try:
                lo_s, hi_s = params.range.split(",", 1)
                lo, hi = float(lo_s), float(hi_s)
            except (ValueError, AttributeError):
                return self._ar(error=(
                    f"range {params.range!r} is not 'full' or "
                    f"'price_lo,price_hi'."))
            if lo <= 0 or hi <= 0:
                return self._ar(error="range prices must be greater than 0.")
            if flipped:
                price0_lo = _lp_invert_if_flipped(hi, True)
                price0_hi = _lp_invert_if_flipped(lo, True)
            else:
                price0_lo, price0_hi = lo, hi
            tick_lower = M.align_tick(M.price_to_tick(price0_lo, dec0, dec1), spacing)
            tick_upper = M.align_tick(M.price_to_tick(price0_hi, dec0, dec1), spacing)

        if tick_lower >= tick_upper:
            return self._ar(error=(
                f"range collapses to an empty tick window "
                f"[{tick_lower},{tick_upper}] after aligning to the "
                f"{spacing}-tick spacing for fee {params.fee} — widen it."))

        sqrt_a = M.sqrt_price_at_tick(tick_lower)
        sqrt_b = M.sqrt_price_at_tick(tick_upper)

        amount0_human = params.amount_b if flipped else params.amount_a
        amount1_human = params.amount_a if flipped else params.amount_b
        raw0 = (_LP_MAX_RAW if amount0_human is None
                else int(round(amount0_human * (10 ** dec0))))
        raw1 = (_LP_MAX_RAW if amount1_human is None
                else int(round(amount1_human * (10 ** dec1))))

        liquidity = M.liquidity_for_amounts(sqrt_p, sqrt_a, sqrt_b, raw0, raw1)
        out0, out1 = M.amounts_for_liquidity(sqrt_p, sqrt_a, sqrt_b, liquidity)

        sym_a = self._lp_symbol(params.chain, params.token_a)
        sym_b = self._lp_symbol(params.chain, params.token_b)
        sym0 = self._lp_symbol(params.chain, token0)
        sym1 = self._lp_symbol(params.chain, token1)
        price0_in_1_now = (sqrt_p / M.Q96) ** 2 * (10 ** dec0) / (10 ** dec1)
        implied = _lp_invert_if_flipped(price0_in_1_now, flipped)

        lines = [
            f"lp_quote {params.chain} v3 fee {params.fee / 1e4:g}% — {pool_note}",
            f"  ticks: [{tick_lower},{tick_upper}]",
            f"  liquidity: {liquidity:,}",
            f"  amount0: {_lp_amount_text(out0, dec0)} {sym0}",
            f"  amount1: {_lp_amount_text(out1, dec1)} {sym1}",
            f"  implied price: 1 {sym_a} = {_lp_number_text(implied)} {sym_b}",
        ]
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
