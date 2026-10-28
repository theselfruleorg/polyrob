"""Text renderers and number formatters for ``defi_data`` results.

Pure functions: each takes already-read data and returns lines or a string. No
network, no wallet, no state. Split out of ``data_tool.py`` (W9, behaviour-
preserving); ``data_tool`` re-exports every name here, so
``from tools.defi.data_tool import _fmt_usd`` keeps working.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from tools.defi.providers.base import PriceInfo
from tools.defi.token_screen import quoted

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
    from tools.defi.data_tool import _pricing_budget_sec  # call-time
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


def _render_scan(rows, *, chain: str, kind: str, excluded: int, floor: float,
                 read_count: Optional[int] = None) -> List[str]:
    """Pure. Every classified pool, ordered by verdict, nothing dropped.

    ``read_count`` is how many pools the indexer sent. 086 W9: a floor that
    removed every pool used to render "no pools were returned" — the indexer
    HAD returned them, and the hunt reported an empty chain."""
    label = "newest" if kind == "new" else "trending"
    head = [
        f"{label} pools on {chain} — {len(rows)} classified",
        "",
        "A verdict reads the MARKET (depth, flow, age, participation), NOT the "
        "contract: run token_info and token_holders before acting. A SURVIVOR "
        "is not a safety claim.",
        "",
    ]
    if not rows and excluded:
        sent = read_count if read_count is not None else excluded
        return head + [
            f"  the indexer returned {sent} pool(s); all of them were below "
            f"your liquidity floor ({_fmt_usd(floor)}).",
            "  Lower the floor to see them — a filter that removed everything is "
            "not an empty chain.",
        ]
    if not rows:
        return head + [
            "  no pools: the indexer answered with ZERO pools for this chain and window.",
            "  That is an empty answer from the indexer, not a failed read and "
            "not a verdict on the chain.",
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
        lines.append(f"  {verdict.verdict}{tag}  {quoted(pool.name, name=True)}   token "
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
from tools.defi.reconcile import _ticker_collision_lines  # noqa: E402,F401 — re-exported


def _price_line(price) -> str:
    """The price line, NAMING the pool the price came from.

    ⚠️ It used to read ``price: X  confidence: Y  pools: N  liquidity: Z`` —
    three numbers from three scopes, since the price is ONE pool's and the
    liquidity is the SUM. On 2026-09-23 the SAFETY rail reported one token's liquidity
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
        # 2026-09-23: reserve and volume pointed at DIFFERENT pools for one token —
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
