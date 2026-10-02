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

import asyncio
import logging
import time
import types
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field

from tools.base_tool import BaseTool
from tools.defi.providers import alchemy_index, dexscreener, goplus
from tools.defi import wallet_intel as _wallet_intel
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
    token_in: str = Field(..., description="CONTRACT ADDRESS of the token to sell (0x…), or the literal 'native' (quoted via the chain's wrapped-native, as the swap verb prefers)")
    token_out: str = Field(..., description="CONTRACT ADDRESS of the token to buy (0x…), or the literal 'native'")
    amount_in: float = Field(..., gt=0, description="Human amount of token_in to sell")


class TokenRefParams(BaseModel):
    chain: str = Field("base", description=_chain_field("look the token up on"))
    address: str = Field(..., description=(
        "The TOKEN's address: on EVM chains its contract (0x…, 40 hex); on "
        "solana its MINT (base58). A ticker is not accepted — use token_resolve "
        "first. A WALLET is not a token: to see what a wallet holds use "
        "wallet_holdings."))


class PortfolioParams(BaseModel):
    chain: str = Field("base", description=_chain_field("report holdings on"))
    account: Optional[str] = Field(None, description=(
        "Optional (069 v4) — report the holdings of the token-bound ERC-6551 account of an NFT "
        "this treasury OWNS instead of the treasury's (a pinned collection only; needs "
        "AGENT_NFT_ENABLED)."))
    nft: Optional[str] = Field(None, description=(
        "Optional — the same as `account`, naming the NFT: '<collection>#<id>' or '<id>'."))


class WalletHoldingsParams(BaseModel):
    address: str = Field(..., description=(
        "The WALLET address to read — any owner, not only this agent's. A Solana "
        "wallet is base58 (e.g. 7xKX…); an EVM wallet is 0x… (40 hex). Give a "
        "wallet, not a token: for a token contract/mint use token_info."))
    chain: Optional[str] = Field(None, description=(
        "Chain to read. Omit for a base58 address (it is read on solana). "
        "REQUIRED for a 0x address — the same 0x wallet exists on every EVM "
        "chain with different holdings. " + _chain_field("read the wallet on")))


class ReconcileParams(BaseModel):
    chain: str = Field("base", description=_chain_field("reconcile against"))
    ledger_path: str = Field(..., description=(
        "Path to the LIVE position-ledger markdown file — never a copy or an "
        "excerpt. Its '## Open positions' table is compared row by row "
        "against actual on-chain balances."))
    account: Optional[str] = Field(None, description=(
        "Reconcile a token-bound ERC-6551 account (its 0x address) instead of the "
        "treasury wallet. Omit for the treasury."))


class PositionsParams(BaseModel):
    """071 W3: the rail book, with the arithmetic done."""
    chain: Optional[str] = Field(None, description=(
        "Optional — one chain. Omit for every chain the rail book tracks."))


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
    address: str = Field(..., description="Contract address to read from (0x…). EVM chains only — there is no eth_call on solana.")
    signature: str = Field(..., description="Function signature, e.g. 'totalSupply()'")
    args: List[Any] = Field(default_factory=list, description="Arguments (currently unencoded; prefer no-arg views)")


class NftHoldingsParams(BaseModel):
    chain: str = Field("base", description=_chain_field("list NFT holdings on"))
    address: Optional[str] = Field(
        None, description=("Wallet address to inspect (0x…). EVM chains only. "
                           "Defaults to this agent's own wallet."))


class NftInfoParams(BaseModel):
    chain: str = Field("base", description=_chain_field("read the token on"))
    contract: str = Field(..., description="The NFT collection contract (0x…). EVM chains only.")
    token_id: int = Field(..., ge=0, description="The token id to read.")


class LpPositionsParams(BaseModel):
    chain: str = Field("robinhood", description=_chain_field("read Uniswap v3 positions on"))
    protocol: str = Field("v3", description="Uniswap protocol: v3 (v4 in a later phase).")
    address: Optional[str] = Field(None, description="Owner address; defaults to the agent wallet.")


class LpPoolInfoParams(BaseModel):
    chain: str = Field("robinhood", description=_chain_field("read a Uniswap v3 pool on"))
    protocol: str = Field("v3", description="v3, or v4 for a Pons graduated pool (give token_a + token_b; the other side is 'native').")
    pool: Optional[str] = Field(None, description="Pool address, OR give token_a+token_b+fee.")
    token_a: Optional[str] = None
    token_b: Optional[str] = None
    fee: Optional[int] = Field(None, description="Fee tier in hundredths of a bip: 100, 500, 3000, 10000.")


class PoolMetricsParams(BaseModel):
    chain: str = Field("robinhood", description=_chain_field("read a Pons v4 pool on"))
    token: str = Field(..., description="The Pons token whose graduated v4 pool to read (0x…). EVM only — Pons is a Robinhood-chain launchpad.")
    expect_pool_id: Optional[str] = Field(None, description=(
        "The pool id you expect (bytes32). A different PoolKey hash is reported as an ALERT."))
    depth_pct: List[float] = Field(default_factory=lambda: [1.0, 2.0, 5.0], description=(
        "Price moves (in %) to size the depth for, both directions."))
    mint_native: float = Field(0.021, gt=0, description="Native size of one buy, for the impact figure.")
    hook_fee_bps: int = Field(100, ge=0, le=10000, description=(
        "Fee the hook takes from the input, in bps (090: 1%). An assumption, not read."))


class LpQuoteParams(BaseModel):
    chain: str = Field("robinhood", description=_chain_field("quote a Uniswap v3 deposit on"))
    protocol: str = Field("v3", description="v3, or v4 for a Pons graduated pool (full range only; the other side is 'native').")
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


#: token_resolve rows listed before the rest is stated as a count (all of them
#: stay in the result metadata).
_RESOLVE_ROWS_SHOWN = 10

#: Rows the budget block lists by address before it states the remainder as a
#: count (071: an exchange wallet measured 3,093 token accounts).
_BUDGET_ROWS_SHOWN = 40


def _budget_exhausted_lines(rows, *, priced: int, total: int,
                            list_rows: bool = True) -> List[str]:
    """Name every holding the budget stopped us pricing — or, past the row cap,
    COUNT them (the caller states the count; the metadata lists each one).

    ⚠️ The failure mode this guards is in this file's own history: on 2026-08-25 a
    DexScreener outage put the wallet's own USDC under the dust warning and the
    agent "duly reported its entire spendable balance as not a position". A budget
    that silently shortened the list would be that bug again and worse — a rail
    reading a shorter portfolio can conclude a position is GONE. So an unpriced
    holding is listed with its amount, or counted out loud, and the reason is
    stated ONCE: it is missing a VALUE, not a balance.
    """
    if not rows:
        return []
    out = ["", f"holdings NOT priced — time budget ran out ({priced} of {total} "
               f"priced in {_pricing_budget_sec():.0f}s): they are held and missing a "
               f"VALUE, not a balance. Do not conclude you hold nothing here; re-read "
               f"for a value (or raise DEFI_PORTFOLIO_PRICE_BUDGET_SEC)."]
    if not list_rows:
        return out
    for row in rows[:_BUDGET_ROWS_SHOWN]:
        # 3-tuple: identity resolved before the budget ran out, so the decimal
        # amount and symbol are known. 4-tuple: it did not, and RAW UNITS are
        # what is actually known — say that rather than invent a decimal figure
        # from decimals nobody read.
        addr, amount, symbol = row[0], row[1], row[2]
        units = row[3] if len(row) > 3 else None
        if isinstance(amount, (int, float)):
            shown = f"{amount:,.6f}" + (f" {symbol}" if symbol else "")
        elif units is not None:
            shown = f"{units} raw units (decimals not read — do not convert by hand)"
        else:
            shown = f"{amount} {symbol or '?'}"
        out.append(f"  {addr}  {shown}  value NOT READ (budget)")
    if len(rows) > _BUDGET_ROWS_SHOWN:
        # Counted, never dropped: a wallet can carry thousands of mints.
        out.append(f"  … and {len(rows) - _BUDGET_ROWS_SHOWN} more held token(s) "
                   f"not priced and not listed ({len(rows)} in total).")
    return out


#: Valued rows a holdings render lists (largest first); the rest are counted.
_VALUED_ROWS_SHOWN = 10
#: Past this many unvalued + unpriced rows, a holdings render gives COUNTS (the
#: full list stays in the result metadata). An exchange wallet measured 3,093.
_REST_ROWS_LISTED = 10


def _valued_lines(valued: List[tuple]) -> List[str]:
    """``[(value, text)]`` → the top rows by value, and the rest counted."""
    rows = sorted(valued, key=lambda r: -r[0])
    out = [t for _v, t in rows[:_VALUED_ROWS_SHOWN]] or ["  (no confidently-valued holdings)"]
    if len(rows) > _VALUED_ROWS_SHOWN:
        out.append(f"  +{len(rows) - _VALUED_ROWS_SHOWN} more valued holding(s) — "
                   f"counted in the total, listed in the result metadata")
    return out


def _total_line(total: float, *, reads: int, failed: int) -> str:
    """The total. When EVERY balance read failed it is UNKNOWN, never $0.00."""
    if reads and failed >= reads:
        return f"total: UNKNOWN — {failed} of {reads} balance reads failed (unknown is not $0)"
    line = f"total (confidently valued only): {_fmt_usd(total)}"
    if failed:
        line += f" — {failed} of {reads} balance reads failed, so it may be higher"
    return line


def _dust_header(own: bool) -> str:
    return ("unvalued — NOT necessarily holdings you bought: a token can sit here "
            "because someone SENT it to you, and airdropped dust is common bait. "
            "Never trade or approve one you did not deliberately buy."
            if own else
            "unvalued — NOT necessarily tokens this wallet bought: anyone can SEND "
            "a token here, and airdropped dust is common bait. Never trade or "
            "approve one you did not deliberately buy.")


def _holdings_rest_lines(*, own: bool, known: List[str], ours: List[str],
                         others: List[str], budget_rows: List[tuple], priced: int,
                         read_failed: int = 0) -> List[str]:
    """Everything below the total: canonical tokens without a price (always
    listed — they are never dust), the budget's unpriced rows, and the
    unvalued rows. Own/pinned rows are always listed; the rest is listed when
    short and COUNTED when long (the metadata keeps every row)."""
    out: List[str] = []
    if known:
        out += ["", ("verified holdings whose PRICE is unavailable right now — canonical "
                     "tokens, your own balance (not dust); excluded from the total, not "
                     "from your holdings:") if own else
                    ("verified holdings whose PRICE is unavailable right now — canonical "
                     "tokens; excluded from the total, not from the holdings:")] + known
    n_rest = len(others) + len(budget_rows)
    listed = n_rest <= _REST_ROWS_LISTED
    out += _budget_exhausted_lines(budget_rows, priced=priced,
                                   total=priced + len(budget_rows), list_rows=listed)
    if ours or others:
        out += ["", _dust_header(own)] + ours + (others if listed else [])
    if not listed:
        bits = []
        if budget_rows:
            bits.append(f"{len(budget_rows):,} not priced (time limit)")
        if others:
            bits.append(f"{len(others):,} unvalued (possible airdrops)")
        out.append(f"  {n_rest:,} more held: " + " · ".join(bits)
                   + " — not listed here; every row is in the result metadata")
    if read_failed:
        out.append(f"  {read_failed} other scanned contract(s): balance read failed — "
                   f"unknown, not zero (not listed; see metadata)")
    return out


def _excluded_value_text(amount: Optional[float], info: PriceInfo, *,
                         ours: bool = True) -> str:
    """The tail of a holding line whose price is not confident enough to count.

    ⚠️ It used to say only ``value EXCLUDED (low confidence price)`` and leave
    the multiplication to the model. On 2026-09-29 the model did that sum in its
    head for a 156,387,468.62-token holding, divided by 10**6 once too often, and
    told the owner the position was worth "$7" instead of ~$6,780 — then defended
    the wrong figure for half an hour. The tool now does the arithmetic and shows
    it, so the figure is never the model's.

    It stays EXCLUDED from the total, and the tail deliberately never ends in
    ``= $X`` — that is the confident-value shape `book._worth_from_portfolio`
    parses.

    ⚠️ ``ours`` = canonical or owner-pinned. Only those get the figure: an
    airdropped token priced by its sender's own seeded pool would otherwise
    print "≈ $10,000,000" on the screen the owner reads as net worth.
    """
    conf = info.confidence
    price = info.price_usd
    if not ours or price is None or not _is_finite(price) or amount is None:
        return f"value EXCLUDED ({conf} confidence price)"
    return (f"value EXCLUDED from the total ({conf} confidence price "
            f"{_fmt_usd(price)} × {amount:,.6f} → indicative ≈ "
            f"{_fmt_usd(amount * price)})")


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


def _fmt_amount(amount: float) -> str:
    """A token amount at six decimals — unless a real nonzero amount would
    round to ``0.000000``, which reads as an empty balance; then it is written
    out at its own significant digits."""
    text = f"{amount:,.6f}"
    if amount and float(text.replace(",", "")) == 0:
        from tools.defi.token_screen import plain_number
        return plain_number(amount)
    return text


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

#: The one honest refusal for an LP read this tier cannot do (v4 positions, an
#: unknown protocol) — never a stub answer.
_LP_V4_NOT_YET = ("this Uniswap v4 read is not built — phase 3 of proposal 048 ships only "
                  "lp_pool_info and lp_quote on a Pons PoolKey; v3 only for this read")


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


import re as _re

#: A verdict reason that only repeats a figure from the numbers line.
_SCAN_RESTATEMENT = _re.compile(
    r"^(liquidity [\d,.]+|V/L [\d,.]+|[\d,.]+ txns per buyer|[\d,.]+d old)$")


def _usd0(value: Optional[float]) -> str:
    """Whole dollars for a large figure (a scan row is read at a glance)."""
    if value is None or not _is_finite(value):
        return "unknown"
    return f"${value:,.0f}" if abs(value) >= 1000 else _fmt_usd(value)


def _fmt_scan_numbers(pool) -> str:
    """ONE line of every figure a verdict reads; each appears once."""
    trades = getattr(pool, "trades_h24", None)
    buyers = None if trades is None else trades.buyers
    buys = None if trades is None else trades.buys
    sells = None if trades is None else trades.sells
    mcap = getattr(pool, "market_cap_usd", None)
    return (f"{_fmt_hours(getattr(pool, 'age_hours', None))} old · liq {_usd0(pool.liquidity_usd)}"
            f" · vol 24h {_usd0(pool.volume_h24_usd)}"
            f" · V/L {_fmt_ratio(getattr(pool, 'vol_liq_ratio', None))}"
            f" (1h {_fmt_ratio(getattr(pool, 'hourly_vol_liq_ratio', None), 2)})"
            f" · {_fmt_ratio(getattr(pool, 'txns_per_buyer', None))} txns/buyer"
            f" · buyers {'unknown' if buyers is None else f'{buyers:,}'}"
            f" · buys/sells {'unknown' if buys is None else f'{buys:,}'}/"
            f"{'unknown' if sells is None else f'{sells:,}'}"
            f" · mcap {_usd0(mcap)}"
            + (f" ({_fmt_ratio(getattr(pool, 'mcap_liq_ratio', None))}x liq)"
               if mcap is not None else ""))


#: Worst news first. A scan is read top-down and the rows that cost money are
#: the ones that must not be below the fold.
_SCAN_ORDER = ("SURVIVOR", "CANDIDATE", "TOO_NEW", "PASS", "UNSCREENABLE", "WASH")


def _render_scan(rows, *, chain: str, kind: str, excluded: int, floor: float) -> List[str]:
    """Pure. Every classified pool, ordered by verdict, nothing dropped."""
    label = "newest" if kind == "new" else "trending"
    head = [
        f"{label} pools on {chain} — {len(rows)} classified",
        "",
        "A verdict reads the MARKET (depth, flow, age, participation), NOT the "
        "contract: run token_info and token_holders before acting. A SURVIVOR "
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
        lines.append(f"  {verdict.verdict}{tag}  {pool.name}   token "
                     f"{pool.base_token or 'UNKNOWN (the indexer named a base token this chain does not accept)'}")
        lines.append(f"      {_fmt_scan_numbers(pool)}")
        # Only the reasons that EXPLAIN the verdict; a bare restatement of a
        # number already on the line above ("V/L 3.3", "12d old") is dropped.
        for reason in verdict.reasons:
            if not _SCAN_RESTATEMENT.match(reason):
                lines.append(f"      · {reason}")
        if verdict.unknowns:
            lines.append(f"      ? NOT CHECKED: {'; '.join(verdict.unknowns)}")
        lines.append("")
    if excluded:
        lines.append(f"  {excluded} pool(s) excluded by your liquidity floor "
                     f"({_fmt_usd(floor)}). Said out loud because a silent filter "
                     f"reads as 'this is all there was'.")
    return lines



# 068: moved to tools/defi/reconcile.py so the reconcile render shows it too.
from tools.defi.reconcile import _ticker_collision_lines  # noqa: E402,F401


def _price_line(price) -> str:
    """The price line, NAMING the pool the price came from.

    ⚠️ It used to read ``price: X  confidence: Y  pools: N  liquidity: Z`` —
    three numbers from three scopes, since the price is ONE pool's and the
    liquidity is the SUM. On 2026-09-23 the SAFETY rail reported PNL liquidity
    as $4,133 and then $17,887 with no flag, because it had silently switched
    venue; neither pool had moved. Nothing in this line could have revealed
    that. Naming the address is what lets two reports be told apart.

    An address the provider did not supply is SAID to be missing, never guessed.
    """
    venue = (f"priced pool {price.priced_pool_address}"
             if getattr(price, "priced_pool_address", None)
             else "priced pool not named by the provider")
    priced = getattr(price, "priced_liquidity_usd", None)
    depth = (f"{_fmt_usd(priced)} in that pool" if priced is not None
             else "depth of that pool unknown")
    out = (f"  price:    {_fmt_usd(price.price_usd)}   confidence: {_display_confidence(price)}\n"
           f"  {venue}   {depth}\n"
           f"  pools: {price.pool_count}   liquidity: {_fmt_usd(price.liquidity_usd)}"
           f" total across those pools (pools that only quote this token are not counted)")
    extra = _quote_source_lines(price, indent="  ")
    return out + ("\n" + "\n".join(extra) if extra else "")


def _identity_lines(ident, jupiter: Optional[str] = None) -> List[str]:
    """token_info's identity block. Symbol and name are ATTACKER-WRITTEN, so
    they are shown in double quotes (cleaned), never as bare text; a name the
    source did not send is left out rather than printed as ``None``."""
    from tools.defi.token_screen import quoted
    sym = quoted(ident.symbol) or "unknown"
    name = quoted(getattr(ident, "name", None), name=True)
    dec = "unknown" if ident.decimals is None else ident.decimals
    line = f"  symbol:   {sym}" + (f"   name: {name}" if name else "") + f"   decimals: {dec}"
    if "self-reported" in str(getattr(ident, "source", "") or ""):
        line += "   (symbol from Jupiter, self-reported)"
    if getattr(ident, "source", None) == "owner_pin":
        ver = "yes (pinned by the owner)"
    elif ident.verified:
        ver = "yes (pinned canonical token)"
    else:
        ver = "no (not pinned)"
        if jupiter:
            ver = ("no (not pinned; Jupiter lists it as verified)" if jupiter.startswith("yes")
                   else "no (not pinned; Jupiter does not verify it either)")
    out = [line, f"  verified: {ver}"]
    if ident.metadata_changed:
        out.append("  ⚠ metadata_changed: this contract now reports different "
                   "symbol/decimals than when first seen; the first-seen values "
                   "are shown and it is treated as unverified")
    return out


def _curve_price_lines(price, curve) -> List[str]:
    """A pump.fun token still ON its bonding curve: the curve IS the market, so
    a pool liquidity of $0 and a "single pool" grade say nothing useful. Show
    the curve depth instead, and that a curve price moves with every buy."""
    sold = getattr(curve, "sold_fraction", None)
    sol = getattr(curve, "sol_in_curve", None)
    out = [f"  price:    {_fmt_usd(price.price_usd)}   (curve price — it moves with every buy)",
           f"  on pump.fun curve: {'unknown' if sol is None else f'{sol:,.3f}'} SOL, "
           f"{'unknown' if sold is None else f'{sold * 100:.1f}%'} sold"]
    return out + _quote_source_lines(price, indent="  ")


def _holders_line(holders, screen) -> str:
    """ONE holders line for token_info. A failed per-address read is said
    once: when the screen already lists it under "not run", it is not
    repeated here. Unknown distribution is never presented as a wide one."""
    if holders is not None and getattr(holders, "available", False):
        return (f"  holders:  {_fmt_count(holders.holder_count)} addresses; "
                f"top {len(holders.top_holders)} hold {_fmt_pct(holders.top_percent)} "
                f"({_fmt_pct(holders.top_percent_wallets)} in non-contract wallets)"
                + ("   ⚠ same creator has deployed a honeypot before"
                   if holders.honeypot_with_same_creator is True else "")
                + " — token_holders lists them")
    from tools.defi.token_screen import plain_errors
    said = any(n.name == "holder_concentration" for n in getattr(screen, "not_checked", []))
    if said:
        why = "per-address list not read (see not run)"
    else:
        reason = str(getattr(holders, "reason", "") or "no holder data was reported")
        why = "unknown — " + plain_errors(reason.split(". A keyed RPC")[0].split("; Jupiter reports")[0])
    agg = {c.name: c.result for c in getattr(screen, "checks", []) if c.source == "jupiter"}
    jup = ""
    if "top_holders_pct" in agg:
        jup = (f"; Jupiter: top holders hold {agg['top_holders_pct']}"
               + (f" across {agg['holder_count']} holders" if "holder_count" in agg else ""))
    return f"  holders:  {why}{jup}. Unknown distribution is not a wide one."


def _display_confidence(price) -> str:
    """``single_pool`` for a one-pool low grade — DISPLAY only; every spend
    decision compares the raw ``confidence`` field."""
    shown = getattr(price, "display_confidence", None)
    return shown if isinstance(shown, str) else str(getattr(price, "confidence", "unknown"))


def _quote_source_lines(price, *, indent: str) -> List[str]:
    """071 §3.3: every source with its value, the spread, the sources that did
    not answer, and the DISPUTED warning. Empty for a seam's bare PriceInfo."""
    sources = getattr(price, "sources", None)
    failed = getattr(price, "failed", None)
    if not isinstance(sources, tuple) or not isinstance(failed, tuple):
        return []
    out: List[str] = []
    if sources:
        spread = getattr(price, "spread_pct", None)
        out.append(f"{indent}sources: "
                   + " · ".join(f"{n} {_fmt_usd(v)}" for n, v in sources)
                   + (f"   spread {spread:.2f}%" if spread is not None
                      else "   (one source — nothing to cross-check against)"))
    if failed:
        out.append(f"{indent}did not answer: "
                   + ", ".join(f"{n} ({e})" for n, e in failed)
                   + " — unknown, not zero")
    if getattr(price, "confidence", None) == "disputed":
        out.append(f"{indent}⚠ DISPUTED — do not size or value a spend on this. "
                   f"{getattr(price, 'reason', '')}".rstrip())
    elif getattr(price, "note", None):
        out.append(f"{indent}note: {price.note}")
    return out


def _quote_metadata(price) -> Dict[str, Any]:
    """Typed price facts for ``ActionResult.metadata`` (071 §2.4: prose is
    not the interface). Works for a full quote and for a seam's PriceInfo."""
    to_md = getattr(price, "to_metadata", None)
    if callable(to_md):
        return to_md()
    return {
        "usd": getattr(price, "price_usd", None),
        "confidence": getattr(price, "confidence", None),
        "display_confidence": _display_confidence(price),
        "sources": [], "failed_sources": [], "spread_pct": None,
        "liquidity_usd": getattr(price, "liquidity_usd", None),
        "priced_liquidity_usd": getattr(price, "priced_liquidity_usd", None),
        "pool": getattr(price, "priced_pool_address", None),
        "pool_count": getattr(price, "pool_count", None),
    }


def _holding_md(address: str, symbol, decimals, raw, amount, info,
                value_usd: Optional[float]) -> Dict[str, Any]:
    """One holdings row as typed facts. ``usd`` is the tool's own product of
    amount × price and is set ONLY when the row counts toward the total."""
    return {
        "address": address, "symbol": symbol, "decimals": decimals,
        "raw": None if raw is None else str(raw),
        "amount_human": amount,
        "price_usd": getattr(info, "price_usd", None),
        "confidence": getattr(info, "confidence", None),
        "usd": value_usd,
        "status": "valued" if value_usd is not None else (
            "unpriced" if getattr(info, "price_usd", None) is None else "excluded"),
    }


def _holdings_metadata(*, holder: str, chain: str, own: bool, coverage: str,
                       complete: bool, total_usd: float,
                       rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """``portfolio`` / ``wallet_holdings`` typed facts (071 §2.4). The total is
    the CONFIDENTLY valued total only — the same number the prose prints — and
    every row it leaves out is listed with the reason."""
    return {
        "holder": holder, "chain": chain, "own": own,
        "coverage": coverage, "coverage_complete": bool(complete),
        "total_usd": float(total_usd),
        "holdings": rows,
        "excluded": [r["address"] for r in rows if r.get("status") == "excluded"],
        "unpriced": [r["address"] for r in rows
                     if r.get("status") not in ("valued", "excluded")],
    }


def _render_candles(candles, summary, *, chain: str, token: str, pool: str,
                    timeframe: str, aggregate: int, depth=None,
                    reference=None) -> List[str]:
    """Pure. The series plus the shape read; no candles is SAID, never drawn flat.

    ``depth`` is the resolver's :class:`PoolPick` when the pool was chosen here,
    and None when the caller supplied one. Naming the pool was never enough on
    its own: on 2026-09-22 a rail read candles from a $4,251 pool, called a
    "~96% collapse", and its own executable quote said −7% — the two pools
    disagreed by ~20× and nothing in the answer let the reader see it. The depth
    line and the cross-check pointer are that missing signal.
    """
    if depth is not None:
        liq = getattr(depth, "liquidity_usd", None)
        liq_txt = (f"liquidity {_fmt_usd(liq)}" if liq is not None
                   else "liquidity unknown (the indexer gave no reserve)")
        vol = getattr(depth, "volume_h24_usd", None)
        vol_txt = (f"24h volume {_fmt_usd(vol)}" if vol is not None
                   else "24h volume unknown (the indexer gave none)")
        count = getattr(depth, "pool_count", 0) or 0
        basis = getattr(depth, "basis", "deepest")
        pool_line = (f"  pool read: {pool}   window: {aggregate}x{timeframe}\n"
                     f"  {basis} of {count} indexed pool{'s' if count != 1 else ''}"
                     f" · {liq_txt} · {vol_txt}")
        # 2026-09-23: reserve and volume pointed at DIFFERENT pools for PNL —
        # $18,190/$1,390 against $4,148/$61,792 — and a reviewer who read the
        # deeper one filed a red flag against a correct rail. Naming the pool we
        # did NOT read is the only way the reader can see that choice was made.
        if getattr(depth, "pools_disagree", False):
            pool_line += (
                f"\n  ⚠ a DEEPER pool exists and was not read: "
                f"{depth.deepest_address} · "
                f"{_fmt_usd(depth.deepest_liquidity_usd)} liquidity · "
                + (f"{_fmt_usd(depth.deepest_volume_h24_usd)} 24h volume"
                   if depth.deepest_volume_h24_usd is not None
                   else "no 24h volume reported")
                + ". This series is the pool that TRADES; that one holds more.")
    else:
        pool_line = (f"  pool read: {pool}   window: {aggregate}x{timeframe}\n"
                     f"  as supplied by the caller — no depth was read for it")
        # 071 R10: a caller-named pool can be the THIN one (a "~96% collapse"
        # read off a $4k pool while the trade routed through an $18k one).
        # Naming the resolver's pick next to it makes a wrong-pool read visible.
        ref = getattr(reference, "address", None)
        if ref and str(ref).lower() != str(pool).lower():
            liq = getattr(reference, "liquidity_usd", None)
            vol = getattr(reference, "volume_h24_usd", None)
            pool_line += (
                f"\n  ⚠ NOT the indexer's pick: {ref} · "
                f"{_fmt_usd(liq) if liq is not None else 'unknown'} liquidity · "
                f"{_fmt_usd(vol) if vol is not None else 'unknown'} 24h volume"
                f" ({getattr(reference, 'basis', 'deepest')} of "
                f"{getattr(reference, 'pool_count', 0) or 0} indexed). Check which pool "
                f"the trade routes through before narrating a move.")
        elif ref:
            liq = getattr(reference, "liquidity_usd", None)
            pool_line += (f"\n  this is also the indexer's pick · "
                          f"{_fmt_usd(liq) if liq is not None else 'unknown'} liquidity")
    head = [
        f"price history for {token} (chain {chain})",
        pool_line,
        "  ⚠ Candles are POOL-scoped. Another pool for the same token can show a "
        "different history; this is the one named above.",
        "  ⚠ The DEEPEST INDEXED pool is not necessarily the one a trade routes "
        "through. Before narrating a move, check it against an executable price "
        "(`swap_quote`): a thin pool can show a collapse the venue you trade on "
        "never had.",
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
        from tools.defi.token_screen import plain_errors
        return head + [
            f"  UNAVAILABLE — {plain_errors(report.reason or 'the screener said nothing')}.",
            "  Unknown is NOT a clean result and NOT a wide holder base.",
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
    lines += ["", "  ⚠ These are ADDRESSES, not people: one party can hold through many."]
    return lines


def _render_creator(report) -> List[str]:
    lines = []
    if report.creator_address:
        lines.append(f"  creator: {report.creator_address}  holds "
                     f"{_fmt_pct(report.creator_percent)}")
    if report.owner_address:
        # GoPlus's owner_address: for an upgradeable proxy this is often the
        # PROXY ADMIN, not what the token's own owner() returns (token_info
        # shows that one as "token owner()"). Labelled so the two never clash.
        lines.append(f"  owner per GoPlus (for a proxy, often the proxy admin): "
                     f"{report.owner_address}  holds {_fmt_pct(report.owner_percent)}")
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
        """The agreed quote (071: ``core.intel.price`` over every source), or
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

    def _swap_quote_sync(self, params: SwapQuoteParams, execution_context=None):
        from core.wallet.tokens import get_token_identity

        # 'native' is a first-class side here, exactly as it is on the swap verb
        # (which PREFERS it when the wallet holds the gas asset). The EVM route
        # providers price the WRAPPED asset, so the literal resolves to the
        # chain registry's wrapped_native row — before validation, which would
        # otherwise refuse "'native' is not a 20-byte hex address" and cost the
        # money rails one wasted step per quote (13 refusals in 3 days, 09-19..21).
        via_native = []
        token_in, token_out = params.token_in, params.token_out
        if isinstance(token_in, str) and token_in.strip().lower() == "native":
            token_in, why = self._native_as_wrapped(params.chain)
            if token_in is None:
                return self._ar(error=why)
            via_native.append(f"token_in 'native' quoted via wrapped-native {token_in}")
        if isinstance(token_out, str) and token_out.strip().lower() == "native":
            token_out, why = self._native_as_wrapped(params.chain)
            if token_out is None:
                return self._ar(error=why)
            via_native.append(f"token_out 'native' quoted via wrapped-native {token_out}")

        addr_in, err = self._validate(params.chain, token_in)
        if err:
            return self._ar(error=err)
        addr_out, err = self._validate(params.chain, token_out)
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
        # Cross-check against the indexer-implied output (fail-open). The OUT
        # side's indexed price is read once and also values the output in USD
        # below — the EXIT rail quotes every row into WETH and was converting
        # by a remembered ETH price ("WETH≈$2,700 est", 2026-09-22 02:01Z).
        p_out_usd = None
        try:
            price_out = self._price_for(params.chain, addr_out)
            p_out_usd = _usable_price(price_out)
            xc = quote_cross_check(params.amount_in, out_human,
                                   self._price_for(params.chain, addr_in), price_out)
        except Exception as e:  # noqa: BLE001 — a read-side check never blocks the quote
            xc = QuoteCrossCheck("unavailable", None, None, f"price lookup failed: {e}")
        if p_out_usd is not None:
            value_line = (f"  value:  ≈ {_fmt_usd(out_human * p_out_usd)} "
                          f"(output at the indexed {_fmt_usd(p_out_usd)} per "
                          f"{id_out.symbol or 'out'}; an indexer read, not a fill)\n")
        else:
            value_line = (f"  value:  unknown — no indexed price with real liquidity "
                          f"for {id_out.symbol or addr_out}; do not substitute a "
                          f"remembered rate\n")
        if xc.verdict == "suspect":
            head = (f"  ⚠ SUSPECT QUOTE — {xc.why} (implied "
                    f"{xc.implied_out:.8f} {id_out.symbol or 'out'}, quoted "
                    f"{out_human:.8f}, {xc.ratio:.1f}x). Re-quote before acting and "
                    f"do NOT use this number as a min-out floor.\n")
        elif xc.verdict == "consistent":
            head = f"  cross-check: consistent — {xc.why}\n"
        else:
            head = f"  cross-check: unavailable — {xc.why}\n"
        for note in via_native:
            head += f"  {note}\n"
        return self._ar(content=(
            f"{params.amount_in} {id_in.symbol or addr_in} -> "
            f"{out_human:.8f} {id_out.symbol or addr_out}\n"
            + head + value_line +
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

    def _evm_holdings_report(self, holder: str, chain: str, *, own: bool = True):
        """The EVM holdings render for ANY holder — the agent's own wallet
        (`portfolio`, ``own=True``) or an explicit public address
        (`wallet_holdings`, ``own=False``). Only the wording about whose
        balance it is differs; coverage, budget and valuation are one code path."""
        if chain not in SUPPORTED_CHAINS:
            return self._ar(error=(
                f"chain {chain!r} is not supported (this tier covers "
                f"{', '.join(SUPPORTED_CHAINS)})"))
        # The budget clock starts BEFORE enumeration: the index/balance read is
        # part of the wall time the controller's 60 s guard measures.
        _budget_started = time.monotonic()
        indexed = None
        index_complete = True
        if self._index_fn is not None:
            indexed = self._index_fn(holder, chain=chain)
        elif alchemy_index.available():
            got = alchemy_index.fetch_all_balances(holder, chain=chain)
            if got is not None:
                indexed, index_complete = got

        if indexed is not None:
            raw = {a: v for a, v in indexed.items()}
            coverage = ("indexed (alchemy) — complete" if index_complete else
                        f"indexed (alchemy) — PARTIAL: {len(raw)} token(s) read, more "
                        f"pages exist or a page failed; a token not listed may still be held")
            scanned_note = ""
        else:
            scan = self._scan_set(chain)
            raw = self._balances(holder, chain, scan)
            coverage = f"partial — {len(scan)} candidate address(es) scanned"
            scanned_note = ("\n  (no ALCHEMY_API_KEY, so holdings could not be enumerated; "
                            "a token outside the scanned set is INVISIBLE here)")

        from core.wallet.tokens import canonical_token
        valued, unpriced_known, total = [], [], 0.0
        ours_unvalued, other_unvalued = [], []
        read_failed_other = zero_rows = 0
        # (symbol, address) for every row, so one symbol held under two
        # contracts can be flagged below — the 2026-09-23 defect, where a rail
        # resolved the token it was monitoring by ticker off this very screen.
        symbols_seen = []
        # 2026-09-23: the pricing half is one network call PER HOLDING, and this
        # wallet's dust cohort made that loop outlive the agent's 600 s stall
        # limit twice, so the SAFETY monitor wrote nothing. Bound it, and NAME
        # (or count) whatever the bound skipped — never shorten the list silently.
        # The clock started before enumeration (top of this method).
        budget_skipped: List[tuple] = []
        priced_count = 0
        pinned = _pinned_addresses(chain)
        # 071 §3.4: one batch call per source per 30 tokens BEFORE the loop
        # (inside the same budget clock), so a dust token with no pool costs no
        # per-row call and the second source costs no per-row call at all.
        prefetch = self._prefetch_prices(chain, [
            a for a, u in sorted(raw.items(), key=lambda kv: (
                _valuation_priority(chain, kv[0], pinned), kv[0])) if u is not None])
        md_rows: List[Dict[str, Any]] = []
        for addr, units in sorted(
                raw.items(),
                key=lambda kv: (_valuation_priority(chain, kv[0], pinned), kv[0])):
            # A token on the registry's CANONICAL pin list is one an operator
            # verified on-chain — it is definitionally not something a stranger
            # sent you. Its provenance and its PRICE are separate questions, and
            # a third-party indexer is allowed to fail at the second. Keeping
            # the two apart matters: on 2026-08-25 a DexScreener outage put the
            # wallet's own USDC under the dust warning, and the agent duly
            # reported its entire spendable balance as "not a position".
            known = canonical_token(chain, addr) is not None
            ours = _valuation_priority(chain, addr, pinned) == 0
            sink = unpriced_known if known else ours_unvalued if ours else other_unvalued
            if units is None:
                md_rows.append({"address": addr, "status": "balance_unknown"})
                if known or ours:
                    sink.append(f"  {addr}  balance UNKNOWN (read failed — not zero)")
                else:
                    # A scan-set candidate whose read failed is COUNTED, not
                    # listed: the scan set can carry placeholder addresses from
                    # a polluted cache, and a real holding is never hidden by
                    # this (only failed reads of unpinned candidates are).
                    read_failed_other += 1
                continue
            if units == 0:
                zero_rows += 1
                md_rows.append({"address": addr, "raw": "0", "status": "zero"})
                continue
            # ⚠️ BEFORE the identity lookup, not after it (2026-09-24). Identity
            # resolution is itself one network call PER HOLDING — measured at
            # 0.26 s mean, ~22 s across this wallet's 84 rows — so a check placed
            # after it leaves the larger half of the loop unbounded, and the
            # budget could not do its job however it was tuned.
            if _budget_spent(_budget_started, time.monotonic()):
                budget_skipped.append((addr, None, None, units))
                md_rows.append({"address": addr, "raw": str(units),
                                "status": "not_priced_budget"})
                continue
            ident = self._identity(chain, addr)
            # getattr: `_identity` is an injection seam (`self._identity_fn`),
            # and a caller's identity object is only promised symbol/decimals.
            # A missing name renders "name unknown"; demanding one would make
            # the collision check the thing that breaks the portfolio read.
            symbols_seen.append((ident.symbol, addr, getattr(ident, "name", None)))
            if ident.decimals is None:
                sink.append(f"  {addr}  {units} raw units (decimals unknown — cannot value; "
                            f"do not convert by hand)")
                md_rows.append({"address": addr, "symbol": ident.symbol, "raw": str(units),
                                "status": "decimals_unknown"})
                continue
            amount = units / (10 ** ident.decimals)
            priced_count += 1
            info = self._row_price(chain, addr, prefetch, ours=ours)
            if info.price_usd is None or info.confidence != "high":
                sink.append(
                    f"  {addr}  {_fmt_amount(amount)} {ident.symbol or '?'}  "
                    f"{_excluded_value_text(amount, info, ours=ours)}")
                md_rows.append(_holding_md(addr, ident.symbol, ident.decimals, units,
                                           amount, info, None))
                continue
            value = amount * info.price_usd
            total += value
            valued.append((value, f"  {addr}  {_fmt_amount(amount)} {ident.symbol or '?'}  "
                                  f"= {_fmt_usd(value)}"))
            md_rows.append(_holding_md(addr, ident.symbol, ident.decimals, units,
                                       amount, info, value))

        lines = [f"holdings for {holder} (chain {chain})",
                 f"coverage: {coverage}{scanned_note}", ""]
        lines += self._gas_lines(holder, chain) + [""]
        # Before the holdings, not after: this is the screen a caller reads to
        # decide which address a ticker means, and a warning below the list
        # arrives after the decision.
        lines += _ticker_collision_lines(symbols_seen)
        lines += _valued_lines(valued)
        reads = len(raw)
        failed_reads = sum(1 for u in raw.values() if u is None)
        lines += ["", _total_line(total, reads=reads, failed=failed_reads)]
        # Immediately after the total, because the total is the number a reader
        # acts on and it is INCOMPLETE whenever this section is non-empty.
        lines += _holdings_rest_lines(
            own=own, known=unpriced_known, ours=ours_unvalued, others=other_unvalued,
            budget_rows=budget_skipped, priced=priced_count, read_failed=read_failed_other)
        return self._ar(content="\n".join(lines), metadata=_holdings_metadata(
            holder=holder, chain=chain, own=own, coverage=coverage,
            complete=(indexed is not None and index_complete) and not budget_skipped,
            total_usd=total, rows=md_rows))

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

    def _record_precondition_failed(self, params, execution_context, reason: str) -> None:
        try:
            from core.event_kinds import RAIL_PRECONDITION_FAILED
            from core.event_log import emit
            emit(RAIL_PRECONDITION_FAILED, source="defi_data",
                 user_id=str(getattr(execution_context, "user_id", "") or ""),
                 session_id=str(getattr(execution_context, "session_id", "") or ""),
                 attrs={"tool": "reconcile", "chain": getattr(params, "chain", ""),
                        "ledger_path": getattr(params, "ledger_path", ""),
                        "reason": reason[:400]})
        except Exception:
            pass

    async def _reconcile_impl(self, params: ReconcileParams, execution_context=None):
        """Thin delegator kept for any caller that awaits the impl directly."""
        return await asyncio.to_thread(self._reconcile_impl_sync, params, execution_context)

    def _reconcile_impl_sync(self, params: ReconcileParams, execution_context=None):
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
        # CR-M07: the book is reconciled against the OPERATOR wallet, and the
        # ledger is read from the caller's own tree — never another tenant's.
        caller_refusal = _operator_read_refusal(execution_context)
        if self._holder == "__from_wallet__" and caller_refusal:
            return self._ar(error=caller_refusal)
        holder = self._solana_holder() if svm else self._resolve_holder()
        nft_account = (getattr(params, "account", None) or "").strip()
        if nft_account:
            # 050 §7.4: reconcile scans the account it is asked about.
            import re as _re
            if svm or not _re.fullmatch(r"0x[0-9a-fA-F]{40}", nft_account):
                return self._ar(error=(
                    f"account {nft_account!r} is not an EVM account address — a token-bound "
                    f"account is a 0x-prefixed 20-byte address on an EVM chain"))
            holder = nft_account
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
        if caller_refusal:
            # Not the owner: only the caller's OWN workspace, never the data
            # home (which holds every tenant's tree) nor the service cwd.
            ws = getattr(execution_context, "workspace_dir", None)
            allowed = [Path(ws).resolve()] if ws else []
        else:
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
        # The size cap bounds the HEAD that must hold the state table, not the
        # file: only the `## Open positions` section is ever parsed, and the
        # rails append their run logs BELOW it. On 2026-09-21 a whole-file cap
        # refused the money rails' step-1 gate for 9 h once the narrative tail
        # passed 1 MB — a legitimate ledger with a long tail must reconcile,
        # while a file whose first 1 MB holds no table is still not a ledger.
        _HEAD = 1_000_000
        if real.stat().st_size > _HEAD:
            with real.open("rb") as fh:
                head = fh.read(_HEAD)
            text = head.decode("utf-8", errors="replace")
            if not any(ln.lstrip().startswith("##") and "open positions" in ln.lower()
                       for ln in text.splitlines()):
                return self._ar(error=(
                    "that file exceeds 1MB and its first 1MB holds no "
                    "'## Open positions' section — not a position ledger; point "
                    "this at the ledger markdown itself (or move its run logs to "
                    "the runlog archive so the table sits at the top)"))
        else:
            text = real.read_text(encoding="utf-8", errors="replace")

        rows, parse_err = rec.parse_open_positions(text)
        if parse_err:
            return self._ar(error=parse_err)
        # 071 W3: the RAIL store is diffed against the chain too, so its
        # addresses are read directly like every ledger row.
        rail, rail_state = self._rail_book(execution_context, chain,
                                           account=nft_account)
        addr_rows = list(rows) + list(rail or [])

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
                raw = {r.address: None for r in addr_rows}
                coverage = ("UNAVAILABLE — the token-account read failed. Every "
                            "row below is unverified; this is NOT a zero book.")
            else:
                raw = dict(enumerated)
                for r in addr_rows:
                    raw.setdefault(r.address, 0)
                coverage = ("complete (enumerated from the chain; no indexer "
                            "key needed here)")
            return self._render_reconcile(rows, raw, chain=chain, row=row,
                                          holder=holder, ledger_path=str(real),
                                          coverage=coverage, rail=rail,
                                          rail_state=rail_state)
        indexed = None
        index_complete = True
        if self._index_fn is not None:
            indexed = self._index_fn(holder, chain=chain)
        elif alchemy_index.available():
            got = alchemy_index.fetch_all_balances(holder, chain=chain)
            if got is not None:
                indexed, index_complete = got
        if indexed is not None:
            raw = {a: v for a, v in indexed.items()}
            coverage = "indexed (alchemy) — complete"
            if not index_complete:
                # 071 R4: a partial index must not make a held ledger row read
                # as "missing on chain" — read every ledger row directly.
                ledger_addrs = [r.address for r in addr_rows
                                if r.address.startswith("0x") and r.address not in raw]
                if ledger_addrs:
                    for a, v in self._balances(holder, chain, ledger_addrs).items():
                        raw.setdefault(a, v)
                coverage = ("indexed (alchemy) — PARTIAL (page cap or a failed page); "
                            "every ledger row was read directly, but an unlisted "
                            "token may still be held")
        else:
            scan = list(dict.fromkeys(
                self._scan_set(chain)
                + [r.address for r in addr_rows if r.address.startswith("0x")]))
            raw = self._balances(holder, chain, scan)
            coverage = (f"partial — {len(scan)} address(es) scanned (no "
                        f"ALCHEMY_API_KEY; a token outside this set is "
                        f"invisible here, but every ledger row was read "
                        f"directly)")

        return self._render_reconcile(rows, raw, chain=chain, row=row,
                                      holder=holder, ledger_path=str(real),
                                      coverage=coverage, rail=rail,
                                      rail_state=rail_state)

    def _rail_book(self, execution_context, chain: str, *, account: str = ""):
        """071 W3: the rail store's rows on ``chain`` for this tenant and holder,
        as ``(rows, state)``. ``rows is None`` = not consulted — the state says
        why (no tenant, or an UNREADABLE store: unknown is not an empty book)."""
        from tools.defi import reconcile as rec
        uid = str(getattr(execution_context, "user_id", "") or "").strip()
        if not uid:
            try:
                from core.exec_identity import current_exec_identity
                uid = str(current_exec_identity()[0] or "").strip()
            except Exception:
                uid = ""
        if not uid:
            return None, "not consulted (no tenant on this call)"
        try:
            from core import open_positions
            entries = open_positions.positions_for(
                uid, chain=chain, account=(account or "").lower())
        except Exception as exc:
            return None, (f"UNREADABLE ({type(exc).__name__}) — not compared; "
                          f"this is NOT an empty rail book")
        rows = [rec.RailPosition(address=e.address, symbol=e.symbol, qty=e.qty,
                                 status=e.status) for e in entries]
        return rows, f"{len(rows)} tracked row(s) on {chain} compared"

    def _render_reconcile(self, rows, raw, *, chain, row, holder, ledger_path,
                          coverage, rail=None, rail_state=None):
        """Value the chain side and diff it against the ledger. Family-agnostic
        on purpose: only the BALANCE SOURCE differs between EVM and Solana, and
        duplicating the valuation would let the two drift."""
        from tools.defi import reconcile as rec
        holdings = []
        for addr, units in sorted((raw or {}).items()):
            ident = self._identity(chain, addr)
            qty = value = est = None
            state = None
            if units is not None and getattr(ident, "decimals", None) is not None:
                qty = units / (10 ** ident.decimals)
            if units:
                info = self._price_for(chain, addr)
                usd = getattr(info, "price_usd", None)
                if usd is not None:
                    state = "high" if getattr(info, "confidence", None) == "high" else "priced"
                    if qty is not None:
                        est = qty * usd
                        if state == "high":
                            value = est
                else:
                    state = "failed" if getattr(info, "failed", None) else "no_pool"
            holdings.append(rec.ChainHolding(
                address=addr, symbol=getattr(ident, "symbol", None), qty=qty,
                raw_units=units, value_usd=value,
                balance_known=units is not None,
                name=getattr(ident, "name", None),
                price_state=state, est_value_usd=est))

        quote_addresses = [a for a in (
            row.usdc if row else None,
            row.wrapped_native if row else None) if a]
        # `evm_chain` decides which address FAMILY counts as this chain's rows;
        # leaving it True on Solana filed every base58 row as "other chain
        # family (not checked here)" and rendered a CLEAN verdict over an
        # unreconciled book — a false all-clear, the worst possible output here.
        report = rec.diff(rows, holdings, quote_addresses=quote_addresses,
                          evm_chain=(row is None or row.family == "evm"),
                          rail=rail)
        return self._ar(content=rec.render(
            report, chain=chain, holder=holder, ledger_path=ledger_path,
            coverage=coverage, rail_state=rail_state),
            metadata={"report": report.to_dict(), "coverage": coverage,
                      "rail_state": rail_state})

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
        "EVM (0x, needs chain). Public chain data — read-only.",
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

    def _own_addresses(self) -> List[str]:
        out: List[str] = []
        if self._holder != "__from_wallet__":
            return [str(self._holder)] if self._holder else []
        try:
            from core.wallet.factory import get_agent_wallet
            wallet = get_agent_wallet()
        except Exception:
            wallet = None
        if wallet is None:
            return out
        for attr in ("address", "solana_address"):
            try:
                val = getattr(wallet, attr, None)
            except Exception:
                val = None
            if val:
                out.append(str(val))
        # 071 review: EVERY address the agent holds is an owner read — the venue
        # keys (x402, polymarket, hyperliquid) and the token-bound accounts of
        # the NFTs it owns, not only the operational and Solana addresses.
        try:
            from core.wallet.agent_wallet import VENUES
            for venue in sorted(VENUES):
                try:
                    val = wallet.address_for_venue(venue)
                except Exception:
                    val = None
                if val:
                    out.append(str(val))
        except Exception:
            logger.debug("venue addresses unreadable", exc_info=True)
        try:
            from core.wallet import nft_holdings
            for row in nft_holdings.tracked() or []:
                if row.get("account"):
                    out.append(str(row["account"]))
        except Exception:
            logger.debug("NFT account list unreadable", exc_info=True)
        return list(dict.fromkeys(out))

    def _address_on_chain(self, raw_address: str, raw_chain: Optional[str]):
        """``(address, chain, error)``: a base58 address defaults to solana, a
        0x address must name its chain, and the address is validated for it."""
        from core.wallet import chains
        raw = (raw_address or "").strip()
        chain = (raw_chain or "").strip().lower() or None
        if chain is None:
            if raw.lower().startswith("0x"):
                evm = [r.name for r in chains.all_rows() if r.family == "evm"]
                return None, None, (
                    f"{raw} is an EVM address, and the same address holds different "
                    f"things on each EVM chain. Call again with chain= one of: "
                    f"{', '.join(evm)}.")
            chain = "solana"
        address, err = self._validate(chain, raw)
        return address, chain, err

    def _wallet_target(self, raw_address: str, raw_chain: Optional[str], execution_context):
        """``(address, chain, own, error)`` for every any-wallet read
        (wallet_holdings, wallet_activity): chain inference, the own-address
        owner gate (CR-M07) and the address-kind check, in that order."""
        from core.wallet.address_kind import wrong_kind_for_wallet
        from core.wallet.addresses import same_address
        address, chain, err = self._address_on_chain(raw_address, raw_chain)
        if err:
            return None, None, False, err
        own = any(same_address(address, mine) for mine in self._own_addresses())
        if own:
            # CR-M07 unchanged: the agent's OWN wallet is an owner read, by
            # whichever verb and however the address was spelled.
            refusal = _operator_read_refusal(execution_context)
            if refusal:
                return None, None, True, refusal.split(" Another wallet")[0]
        wrong = wrong_kind_for_wallet(chain, address, self._address_kind(chain, address))
        if wrong:
            return None, None, own, wrong
        return address, chain, own, None

    def _wallet_holdings_sync(self, params: WalletHoldingsParams, execution_context=None):
        from core.wallet import chains
        address, chain, own, err = self._wallet_target(params.address, params.chain,
                                                       execution_context)
        if err:
            return self._ar(error=err)
        row = chains.get(chain)
        if row is not None and row.family == "svm":
            return self._solana_holdings_report(address, chain, own=own)
        return self._evm_holdings_report(address, chain, own=own)

    def _solana_portfolio_sync(self, params: PortfolioParams):
        """The agent's OWN holdings on a Solana-family chain (Phase 2)."""
        holder = self._solana_holder()
        if not holder:
            return self._ar(error=(
                "the agent wallet has no Solana address — set "
                "AGENT_WALLET_ENABLED=true with a BIP-39 master seed. Reads "
                "that need no address (token_info, price, new_pools, trending, "
                "wallet_holdings) work on solana regardless."))
        return self._solana_holdings_report(holder, params.chain, own=True)

    #: How many unvalued rows a holdings render lists by address. A wallet can
    #: carry thousands of airdropped mints (an exchange hot wallet measured
    #: 3,093 on 2026-10-01); past this the COUNT is stated, never dropped.
    _UNVALUED_ROWS_SHOWN = _REST_ROWS_LISTED

    def _solana_holdings(self, holder: str):
        """`SplHoldings` (classic + Token-2022, decimals kept) or ``None``."""
        from core.wallet import solana_onchain
        try:
            return (self._solana_holdings_fn or solana_onchain.token_holdings)(holder)
        except Exception:
            return None

    def _solana_holdings_report(self, holder: str, chain: str, *, own: bool = True):
        """Holdings of ANY Solana owner. Deliberately its own method rather
        than a branch inside the EVM one: the enumeration is a different RPC,
        the gas asset is SOL, and it needs NO indexer key —
        `getTokenAccountsByOwner` returns every balance directly.

        071 R2/R3/R9: decimals come from the chain (an unpinned mint used to
        render as "raw units"), Token-2022 accounts are listed (the agent's own
        SPL launches are Token-2022), identity is read before price, and the
        pricing loop runs under the same wall-clock budget as the EVM one.
        """
        # The budget clock starts BEFORE enumeration: the token-account read is
        # part of the wall time the controller's 60 s guard measures.
        started = time.monotonic()
        native = self._solana_native(holder)
        held = self._solana_holdings(holder)
        lines = [f"holdings for {holder} (chain {chain})"]
        if held is None:
            lines += ["coverage: UNAVAILABLE — the token-account read failed. "
                      "This is NOT 'holds no tokens'; nothing below is a "
                      "complete picture.", ""]
        elif not held.token_2022_read:
            lines += ["coverage: PARTIAL — classic SPL accounts read; the "
                      "Token-2022 read FAILED, so Token-2022 holdings are "
                      "missing, not absent.", ""]
        else:
            lines += ["coverage: complete (SPL + Token-2022, enumerated from the "
                      "chain; no indexer key needed here)", ""]
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

        valued, total = [], 0.0
        ours_unvalued, other_unvalued = [], []
        budget_skipped: List[tuple] = []
        priced = 0
        sol_pinned = _pinned_addresses(chain)
        rows = sorted((held.rows if held else []),
                      key=lambda h: (_valuation_priority(chain, h.mint, sol_pinned), h.mint))
        # 071 §3.4: batch the price sources before the loop (see the EVM path).
        sol_prefetch = self._prefetch_prices(chain, [h.mint for h in rows])
        sol_md: List[Dict[str, Any]] = []
        for h in rows:
            if not h.raw:
                # An empty token account is not a holding: hidden, kept in metadata.
                sol_md.append({"address": h.mint, "raw": "0", "status": "zero"})
                continue
            # ⚠️ The budget is checked BEFORE the identity read (one network call
            # per row), and for EVERY row — also one whose amount is unknown.
            if _budget_spent(started, time.monotonic()):
                amt = h.raw / (10 ** h.decimals) if h.decimals is not None else None
                budget_skipped.append((h.mint, amt, None) if amt is not None
                                      else (h.mint, None, None, h.raw))
                sol_md.append({"address": h.mint, "decimals": h.decimals,
                               "raw": str(h.raw), "amount_human": amt,
                               "status": "not_priced_budget"})
                continue
            try:
                ident = self._identity(chain, h.mint)
            except Exception:
                ident = None
            decimals = getattr(ident, "decimals", None)
            if decimals is None:
                decimals = h.decimals
            symbol = getattr(ident, "symbol", None)
            amount = h.raw / (10 ** decimals) if decimals is not None else None
            label = f" {symbol}" if symbol else ""
            t22 = (f"  [Token-2022{': ' + ', '.join(h.extensions) if h.extensions else ''}]"
                   if h.token_2022 else "")
            shown = (f"{_fmt_amount(amount)}{label}" if amount is not None
                     else f"{h.raw} raw units (decimals not read — do not convert by hand)")
            sol_ours = _valuation_priority(chain, h.mint, sol_pinned) == 0
            info = self._row_price(chain, h.mint, sol_prefetch, ours=sol_ours)
            priced += 1
            if info.price_usd is None or info.confidence != "high" or amount is None:
                (ours_unvalued if sol_ours else other_unvalued).append(
                    f"  {h.mint}  {shown}{t22}  "
                    f"{_excluded_value_text(amount, info, ours=own and sol_ours)}")
                sol_md.append(_holding_md(h.mint, symbol, decimals, h.raw, amount, info, None))
                continue
            value = amount * info.price_usd
            total += value
            valued.append((value, f"  {h.mint}  {shown}  = {_fmt_usd(value)}{t22}"))
            sol_md.append(_holding_md(h.mint, symbol, decimals, h.raw, amount, info, value))
        lines += _valued_lines(valued)
        lines += ["", _total_line(total, reads=0, failed=0)]
        lines += _holdings_rest_lines(
            own=own, known=[], ours=ours_unvalued, others=other_unvalued,
            budget_rows=budget_skipped, priced=priced)
        if any(h.token_2022 for h in rows if h.raw):
            lines += ["", "Token-2022 extensions (transfer fee, hook, permanent delegate, "
                          "non-transferable) can change what a balance is worth or whether it moves."]
        if own:
            lines += ["", "NOTE: this verb only reads. Selling goes through "
                          "defi_trade.solana_swap (guarded, dry_run by default), "
                          "and only when SOLANA_TRADE_ENABLED is armed."]
        sol_complete = bool(held is not None and held.token_2022_read and not budget_skipped)
        md = _holdings_metadata(
            holder=holder, chain=chain, own=own,
            coverage=("complete" if sol_complete else
                      "unavailable" if held is None else "partial"),
            complete=sol_complete, total_usd=total, rows=sol_md)
        md["native_sol"] = native
        return self._ar(content="\n".join(lines), metadata=md)

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
        # Blocking body on a worker thread — see the note on `token_info`.
        # Declared `async def` while doing synchronous I/O, this verb held the
        # event loop for its whole read and made the controller's 60 s guard
        # unenforceable (2026-09-23, four dead SAFETY runs).
        return await asyncio.to_thread(self._nft_holdings_sync, params, execution_context)

    def _nft_holdings_sync(self, params: NftHoldingsParams, execution_context=None):
        from tools.defi import nft_verbs

        evm_err = self._evm_only(params.chain, "nft_holdings")
        if evm_err:
            return self._ar(error=evm_err)
        address = params.address
        if not address:
            refusal = _operator_read_refusal(execution_context)
            if refusal:
                return self._ar(error=refusal)
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
        # Blocking body on a worker thread — see the note on `token_info`.
        # Declared `async def` while doing synchronous I/O, this verb held the
        # event loop for its whole read and made the controller's 60 s guard
        # unenforceable (2026-09-23, four dead SAFETY runs).
        return await asyncio.to_thread(self._nft_info_sync, params, execution_context)

    def _nft_info_sync(self, params: NftInfoParams, execution_context=None):
        from core.wallet.onchain import _rpc, rpc_url_for_chain
        from tools.defi import nft_verbs

        evm_err = self._evm_only(params.chain, "nft_info")
        if evm_err:
            return self._ar(error=evm_err)
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

    @BaseTool.action(
        "Monitor a Pons graduated Uniswap v4 pool (090 §3): price, tick, in-range "
        "liquidity and reserves, depth to move the price ±X%, the impact of one buy, "
        "the 24h change and a median reference from earlier readings, and a code-hash "
        "check of the pinned v4 contracts and the hook. ALERT lines come first. "
        "Read-only; each reading is journaled. Safe for a cron read job.",
        param_model=PoolMetricsParams)
    async def pool_metrics(self, params: PoolMetricsParams, execution_context=None):
        return await asyncio.to_thread(self._pool_metrics_sync, params, execution_context)

    def _pool_metrics_sync(self, params: PoolMetricsParams, execution_context=None):
        from tools.defi import lp_reads
        from tools.defi.pool_metrics import pool_metrics
        try:
            out = pool_metrics(self._lp_rpc_for(params.chain), params.chain, params.token,
                               expect_pool_id=params.expect_pool_id,
                               depth_pct=tuple(params.depth_pct),
                               mint_native=params.mint_native,
                               hook_fee_bps=params.hook_fee_bps)
        except lp_reads.LpReadError as exc:
            return self._ar(error=_lp_read_error_text(exc))
        return self._ar(content="\n".join(out["lines"]))

    def _lp_v4_read(self, render, chain: str):
        """048 phase 3 (core handoff W6): a v4 read on a Pons PoolKey. The
        pool is derived from the Pons factory record, never from the caller."""
        from tools.defi import lp_reads, lp_v4
        try:
            return self._ar(content="\n".join(render(lp_v4, self._lp_rpc_for(chain))))
        except lp_reads.LpReadError as exc:
            return self._ar(error=_lp_read_error_text(exc))

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
        # Blocking body on a worker thread — see the note on `token_info`.
        # Declared `async def` while doing synchronous I/O, this verb held the
        # event loop for its whole read and made the controller's 60 s guard
        # unenforceable (2026-09-23, four dead SAFETY runs).
        return await asyncio.to_thread(self._lp_positions_sync, params, execution_context)

    def _lp_positions_sync(self, params: LpPositionsParams, execution_context=None):
        if params.protocol != "v3":
            return self._ar(error=_LP_V4_NOT_YET)
        address = params.address
        if not address:
            refusal = _operator_read_refusal(execution_context)
            if refusal:
                return self._ar(error=refusal)
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
        "invented. Read-only. protocol='v4' reads a Pons graduated pool "
        "(token_a/token_b = 'native' + the Pons token).",
        param_model=LpPoolInfoParams)
    async def lp_pool_info(self, params: LpPoolInfoParams, execution_context=None):
        # Blocking body on a worker thread — see the note on `token_info`.
        # Declared `async def` while doing synchronous I/O, this verb held the
        # event loop for its whole read and made the controller's 60 s guard
        # unenforceable (2026-09-23, four dead SAFETY runs).
        return await asyncio.to_thread(self._lp_pool_info_sync, params, execution_context)

    def _lp_pool_info_sync(self, params: LpPoolInfoParams, execution_context=None):
        if params.protocol == "v4":
            return self._lp_v4_read(lambda lp_v4, rpc: lp_v4.pool_info_lines(
                rpc, params.chain, params.token_a, params.token_b), params.chain)
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
        "Read-only — never deposits. protocol='v4' quotes a full-range "
        "deposit into a Pons graduated pool.",
        param_model=LpQuoteParams)
    async def lp_quote(self, params: LpQuoteParams, execution_context=None):
        # Blocking body on a worker thread — see the note on `token_info`.
        # Declared `async def` while doing synchronous I/O, this verb held the
        # event loop for its whole read and made the controller's 60 s guard
        # unenforceable (2026-09-23, four dead SAFETY runs).
        return await asyncio.to_thread(self._lp_quote_sync, params, execution_context)

    def _lp_quote_sync(self, params: LpQuoteParams, execution_context=None):
        if params.protocol == "v4":
            from tools.defi.lp_reads import decimals
            return self._lp_v4_read(lambda lp_v4, rpc: lp_v4.quote_lines(
                rpc, params.chain, params.token_a, params.token_b, params.amount_a,
                params.amount_b, params.range,
                lambda t: decimals(rpc, params.chain, t)), params.chain)
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
        # Blocking body on a worker thread — see the note on `token_info`.
        # Declared `async def` while doing synchronous I/O, this verb held the
        # event loop for its whole read and made the controller's 60 s guard
        # unenforceable (2026-09-23, four dead SAFETY runs).
        return await asyncio.to_thread(self._contract_read_sync, params, execution_context)

    def _contract_read_sync(self, params: ContractReadParams, execution_context=None):
        evm_err = self._evm_only(params.chain, "contract_read")
        if evm_err:
            return self._ar(error=evm_err)
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
