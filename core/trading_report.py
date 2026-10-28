"""The ONE trading renderer (086 W6, §3.7) — ``/trade status``, the status
snapshot's Trading block and the digest's Trading section read this.

What it shows, from the book and the stores beside it (no model, $0):

* the mandate — status, preset, stage, allocation used at cost, slots, breaker
  state (a latched reason, a cooldown and when it ends);
* the mandate's open positions — cost, value and unrealized when a price is
  given (``price_fn``; the status snapshot passes none: no network read), and
  exit coverage PROTECTED / UNPROTECTED (an exit job in ``cron.db`` carrying
  ``payload.mandate`` + ``payload.position`` and still scheduled);
* realized P&L — gross; gas is not recorded on the book, so "net" says so;
* the mandate's LLM cost (episodes ``spend_usd`` with ``meta.mandate``) beside
  the P&L.

Honesty rules (Senpi): a partial read is "at least $X (N of M priced)"; an
unreadable store is UNKNOWN; an absent store is a real empty answer; a READ
never creates a store.

What counts as the mandate's positions is the money kernel's own selection
(``core.money.mandate_gate.read_mandate_book``), never a second rule here.

Tier-0: imports nothing above ``core``.
"""
import json
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

#: A cron job in one of these states still runs (``cron/jobs.py``).
_LIVE_JOB = ("scheduled", "running")

PriceFn = Callable[[str, str], Optional[float]]


def _hhmm(ts: Optional[float]) -> str:
    if ts is None:
        return "?"
    return time.strftime("%m-%d %H:%MZ", time.gmtime(float(ts)))


def _usd(v: float) -> str:
    return f"${v:,.2f}"


def _signed(v: float) -> str:
    return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"


def _key(chain: Any, account: Any, address: Any) -> Tuple[str, str, str]:
    from core.open_positions import _account_key, _norm
    return (str(chain or "").strip().lower(), _account_key(account), _norm(str(address or "")))


# -- readers (each RAISES on an unreadable store; absent = empty) -------------------

def exit_jobs(user_id: str, mandate_id: str, *, cron_db: str) -> Dict[Tuple[str, str, str], str]:
    """``{(chain, account, address): job status}`` of the mandate's exit jobs. A
    paused or disabled job is listed with its state (it does not protect)."""
    if not os.path.exists(cron_db):
        return {}
    from core.sqlite_util import execute_retry
    rows = execute_retry(cron_db, "SELECT status, enabled, payload FROM cron_jobs "
                         "WHERE user_id=?", (str(user_id),), fetch="all") or []
    out: Dict[Tuple[str, str, str], str] = {}
    for r in rows:
        # A row that does not parse might be an exit job: json raises, and the
        # caller renders coverage UNKNOWN rather than guessing UNPROTECTED.
        payload = json.loads(r["payload"] or "{}")
        if not isinstance(payload, dict):
            raise ValueError("a cron job payload is not an object")
        pos = payload.get("position")
        if str(payload.get("mandate") or "") != mandate_id or not isinstance(pos, dict):
            continue
        state = str(r["status"] or "") if r["enabled"] else "disabled"
        k = _key(pos.get("chain"), pos.get("account"), pos.get("address"))
        if out.get(k) not in _LIVE_JOB:
            out[k] = state
    return out


def mandate_llm_cost(user_id: str, mandate_id: str, *, memory_db: str,
                     since: Optional[float] = None) -> Optional[Tuple[float, int]]:
    """``(usd, runs)`` of the mandate's recorded runs (ended at or after *since*);
    None = no episode store."""
    if not os.path.exists(memory_db):
        return None
    from core.sqlite_util import execute_retry
    have = execute_retry(memory_db, "SELECT 1 FROM sqlite_master WHERE type='table' "
                         "AND name='episodes'", (), fetch="one")
    if not have:
        return None
    sql = ("SELECT COUNT(*) AS n, COALESCE(SUM(spend_usd), 0) AS usd FROM episodes "
           "WHERE user_id=? AND json_extract(meta, '$.mandate')=?")
    args: tuple = (str(user_id), str(mandate_id))
    if since is not None:
        sql, args = sql + " AND ts >= ?", args + (float(since),)
    r = execute_retry(memory_db, sql, args, fetch="one")
    return float(r["usd"] or 0.0), int(r["n"] or 0)


def _mandate_rows(m, user_id: str, book_path: str) -> List[dict]:
    """The kernel's own selection, through the book reader ``core.wallet``
    registers at boot. No reader registered RAISES (rendered UNKNOWN)."""
    from core.money.mandate_gate import read_mandate_book
    rows, _ = read_mandate_book(m, user_id=user_id, book_path=book_path)
    return rows


# -- the view ----------------------------------------------------------------------

def trading_view(user_id: str, *, now: Optional[float] = None, data_dir: Optional[str] = None,
                 price_fn: Optional[PriceFn] = None, mandate_path: Optional[str] = None,
                 book_path: Optional[str] = None, cron_db: Optional[str] = None,
                 memory_db: Optional[str] = None) -> Dict[str, Any]:
    """``{lines, present, unknown: [what could not be read], mandate, ...}``.
    ``present`` is False when there is no mandate and no open position (and
    nothing was unreadable). Never raises."""
    from core import runtime_paths as rp
    from core.open_positions import open_positions_db_path, positions_for, realized_for
    from core.trading_mandate import db_path, get_active
    ts = time.time() if now is None else float(now)
    book_path = book_path or open_positions_db_path(data_dir)
    mandate_path = mandate_path or db_path(data_dir)
    cron_db = cron_db or rp.cron_db_path(data_dir)
    memory_db = memory_db or rp.memory_db_path(data_dir)
    lines: List[str] = []
    unknown: List[str] = []

    try:
        m = get_active(user_id, path=mandate_path)
    except Exception as exc:
        m = None
        unknown.append("mandate")
        lines.append(f"mandate: UNKNOWN — the trading store could not be read "
                     f"({type(exc).__name__}); not the same as none")
    try:
        book = positions_for(user_id, db_path=book_path)
        book = [r for r in book if r.qty > 0]
    except Exception as exc:
        book = None
        unknown.append("book")
        lines.append(f"positions: UNKNOWN — the position book could not be read "
                     f"({type(exc).__name__}); not the same as none")
    if m is None and not book and not unknown:
        return {"lines": ["trading: no mandate and no open position"], "present": False,
                "unknown": [], "mandate": None}

    mine: Optional[List[dict]] = None
    if m is not None:
        try:
            mine = _mandate_rows(m, user_id, book_path)
        except Exception as exc:
            if "book" not in unknown:
                unknown.append("book")
            lines.append(f"mandate positions: UNKNOWN ({type(exc).__name__})")
        lines.extend(_mandate_lines(m, mine, ts))

    symbols = {_key(r.chain, r.account, r.address): r.symbol for r in (book or [])}
    if mine is not None:
        coverage, why = _coverage(user_id, m.id, cron_db)
        if why:
            unknown.append("exit jobs")
        lines.extend(_position_lines(mine, symbols, coverage, why, price_fn))
        others = len(book or []) - len(mine)
        if book is not None and others > 0:
            lines.append(f"other open positions (not this mandate's): {others}")
    elif book:
        lines.append(f"open positions: {len(book)} in the book, no mandate — "
                     f"{', '.join((r.symbol or r.address[:10]) for r in book[:5])}"
                     f"{'…' if len(book) > 5 else ''}")

    lines.append(_realized_line(user_id, m, book_path, realized_for, unknown))
    if m is not None:
        lines.append(_cost_line(user_id, m, memory_db, unknown))
    return {"lines": lines, "present": True, "unknown": unknown,
            "mandate": None if m is None else m.id}


def _mandate_lines(m, rows: Optional[List[dict]], ts: float) -> List[str]:
    from core.money.mandate_gate import cost_used_usd
    from core.trading_mandate import HALTED
    used = (f"{_usd(cost_used_usd(m, rows))} of {_usd(m.budget_usd)} at cost"
            if rows is not None else f"UNKNOWN of {_usd(m.budget_usd)}")
    slots = f"{len(rows)}/{m.slots}" if rows is not None else f"?/{m.slots}"
    out = [f"mandate {m.id}: {m.status.upper()} · preset {m.preset} · stage {m.stage} · "
           f"chains {', '.join(m.chains)} · allocation {used} · slots {slots}"]
    if m.status == HALTED:
        out.append(f"breaker: LATCHED — {m.halted_reason or 'a breaker'} since "
                   f"{_hhmm(m.halted_at)}; entries off, exits run; /trade release")
    elif m.cooldown_until is not None and ts < float(m.cooldown_until):
        out.append(f"breaker: cooling down until {_hhmm(m.cooldown_until)} "
                   f"(consecutive losses); entries off, exits run")
    else:
        out.append("breaker: none tripped")
    return out


def _coverage(user_id: str, mandate_id: str, cron_db: str):
    try:
        return exit_jobs(user_id, mandate_id, cron_db=cron_db), ""
    except Exception as exc:
        return {}, f"{type(exc).__name__}"


def _position_lines(rows: List[dict], symbols: dict, coverage: dict, why: str,
                    price_fn: Optional[PriceFn]) -> List[str]:
    if not rows:
        return ["mandate positions: none open"]
    out = [f"mandate positions: {len(rows)} open"]
    value_sum, priced, cost_sum, unprotected = 0.0, 0, 0.0, 0
    for r in rows:
        k = _key(r.get("chain"), r.get("account", ""), r.get("address"))
        sym = symbols.get(k) or str(r.get("address") or "?")[:10]
        cost = r.get("entry_usd")
        bits = [f"{sym} on {k[0]}", f"cost {_usd(float(cost))}" if cost is not None
                else "cost unknown"]
        if cost is not None:
            cost_sum += float(cost)
        price = None
        if price_fn is not None:
            try:
                price = price_fn(k[0], k[2])
            except Exception:
                price = None  # an unread price is unpriced, counted below
        if price is not None and price > 0:
            value = float(r.get("qty") or 0.0) * float(price)
            value_sum += value
            priced += 1
            bits.append(f"value {_usd(value)}")
            if cost is not None:
                bits.append(f"unrealized {_signed(value - float(cost))}")
        else:
            bits.append("not priced here")
        state = coverage.get(k)
        if why:
            bits.append("exit UNKNOWN (cron store unreadable)")
        elif state in _LIVE_JOB:
            bits.append("PROTECTED")
        else:
            unprotected += 1
            bits.append("UNPROTECTED" + (f" (exit job {state})" if state else " (no exit job)"))
        out.append("  - " + " · ".join(bits))
    if price_fn is not None:
        if priced == len(rows):
            out.append(f"  value {_usd(value_sum)} of cost {_usd(cost_sum)}")
        else:
            out.append(f"  value at least {_usd(value_sum)} ({priced} of {len(rows)} priced)")
    if unprotected:
        out.append(f"  ⚠ {unprotected} position(s) UNPROTECTED — no live exit job")
    return out


def _realized_line(user_id: str, m, book_path: str, realized_for, unknown: List[str]) -> str:
    try:
        rows = realized_for(user_id, db_path=book_path)
    except Exception as exc:
        unknown.append("realized")
        return f"realized P&L: UNKNOWN ({type(exc).__name__})"
    scope = "all chains"
    if m is not None:
        rows = [r for r in rows if r.chain.strip().lower() in m.chains
                and r.updated_ts >= float(m.created_at)]
        scope = f"tokens closed since the mandate began on {', '.join(m.chains)}"
    if not rows:
        return f"realized P&L: none booked ({scope})"
    gross = sum(r.realized_usd for r in rows)
    unk = sum(r.unknown_legs for r in rows)
    reasons: Dict[str, int] = {}
    for r in rows:
        reasons[r.close_reason or "unknown"] = reasons.get(r.close_reason or "unknown", 0) + 1
    why = ", ".join(f"{k} {v}" for k, v in sorted(reasons.items()))
    head = f"realized P&L: {_signed(gross)} gross"
    if unk:
        head += f" (known legs only; {unk} leg(s) of unknown value)"
    return (f"{head} · {len(rows)} token(s) · last close by {why} · gas not recorded, "
            f"so net of gas is unknown ({scope})")


def _cost_line(user_id: str, m, memory_db: str, unknown: List[str]) -> str:
    try:
        got = mandate_llm_cost(user_id, m.id, memory_db=memory_db)
    except Exception as exc:
        unknown.append("llm cost")
        return f"LLM cost of this mandate: UNKNOWN ({type(exc).__name__})"
    if got is None:
        return "LLM cost of this mandate: UNKNOWN (no run records store)"
    usd, n = got
    pct = (usd / float(m.budget_usd) * 100.0) if m.budget_usd else 0.0
    return (f"LLM cost of this mandate: {_usd(usd)} over {n} recorded run(s) — "
            f"{pct:.1f}% of the allocation")


def render_trading(user_id: str, *, now: Optional[float] = None, **kw) -> str:
    """The Trading text (``/trade status``, digest). Never raises."""
    try:
        view = trading_view(user_id, now=now, **kw)
    except Exception as exc:  # the renderer itself failed: say so
        return f"Trading: UNKNOWN — the report could not be built ({type(exc).__name__})"
    return "Trading:\n" + "\n".join(f"  {ln}" for ln in view["lines"])


__all__ = ["exit_jobs", "mandate_llm_cost", "render_trading", "trading_view"]
