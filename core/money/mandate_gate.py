"""The trading mandate's money-kernel step + its breakers (086 §3.5).

Two halves:

* :func:`entry_refusal` — what ``core.money.authorize.authorize_spend`` runs for an
  ENTRY by a run that belongs to a mandate (``metadata["mandate"]`` on the
  execution context, ``core.trading_mandate.mandate_id_from_context``). It refuses
  when the mandate is paused / halted / stopped / cooling down, the chain is not
  the mandate's, the allocation at cost is used, the slots are full, the daily
  entry count is reached, or the token is in its re-entry cooldown. An exit never
  reaches it (the caller declares ``entry=False`` for an exit-shaped intent), and an
  owner-direct trade carries no mandate.

  ⚠️ Fail CLOSED on an unreadable mandate or book — an unreadable store is not an
  empty one. An absent book (no file yet) is an empty book: fail OPEN.

* :func:`evaluate_breakers` — PURE: given the mandate, its closed trades and the
  clock, the breaker that trips (``drawdown`` · ``daily_loss`` · ``cost_over_alloc``
  · ``consecutive_losses``) or None. The lifecycle pass (W4) calls it and applies
  the answer with ``core.trading_mandate.apply_breaker`` (a latch, or a cooldown for
  consecutive losses).

What counts as the mandate's positions: OPEN rows of the treasury book on the
mandate's chains, ``origin == "trade"``, first opened at or after the mandate was
created. A row of unknown cost basis counts as one full ticket. A position held
before the mandate (a recurring swap job's holding, an inherited row) is not the mandate's.

The kernel carries no rail data: the book arrives through the rail's registered
reader (``core.money.hooks.position_reader``, registered by ``core.wallet``).
No reader registered = the book is unreadable = refuse.

Tier-0: imports nothing above ``core``.
"""
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

_DAY = 86400.0
_EPS = 1e-9


@dataclass(frozen=True)
class ClosedTrade:
    """One closed (or partly closed) leg of a mandate position. ``pnl_usd=None``
    = the realized figure is unknown; it is skipped, never read as $0."""

    pnl_usd: Optional[float]
    closed_at: float
    chain: str = ""
    token: str = ""


def _status_refusal(m, now: float) -> Optional[str]:
    from core.trading_mandate import ACTIVE, HALTED, PAUSED, STOPPED
    if m.status == PAUSED:
        return (f"refused: trading mandate {m.id} is PAUSED — no new entries; exits still "
                f"run. The owner resumes it with /trade resume.")
    if m.status == HALTED:
        return (f"refused: trading mandate {m.id} is HALTED ({m.halted_reason or 'a breaker'})"
                f" — no new entries; exits still run. Only the owner releases it "
                f"(/trade release).")
    if m.status == STOPPED:
        return f"refused: trading mandate {m.id} is STOPPED — no new entries."
    if m.status != ACTIVE:
        return f"refused: trading mandate {m.id} has an unknown status {m.status!r}"
    if m.cooldown_until is not None and now < float(m.cooldown_until):
        mins = int((float(m.cooldown_until) - now) // 60) + 1
        return (f"refused: trading mandate {m.id} is cooling down after consecutive "
                f"losses ({mins} min left) — no new entries; exits still run.")
    return None


def read_mandate_book(m, *, user_id: str, chain: str = "", token: Optional[str] = None,
                      book_path: Optional[str] = None) -> Tuple[List[dict], Optional[float]]:
    """``(the mandate's open rows on every mandate chain, when *token* on *chain*
    was last sold)``. RAISES when the book cannot be read or no reader exists."""
    from core.money.hooks import position_reader
    read = position_reader()
    if read is None:
        raise RuntimeError("no position book reader is registered")
    rows: List[dict] = []
    sold_at: Optional[float] = None
    for c in m.chains:
        view = read(user_id, c, token if c == chain else None, db_path=book_path)
        for r in view.get("rows") or ():
            if (r.get("status") == "open" and r.get("origin") == "trade"
                    and float(r.get("entry_ts") or 0.0) >= float(m.created_at) - _EPS
                    and float(r.get("qty") or 0.0) > 0):
                rows.append(dict(r, chain=c, is_token=bool(r.get("is_token")) and c == chain))
        if c == chain and view.get("token_sold_at") is not None:
            sold_at = float(view["token_sold_at"])
    return rows, sold_at


def cost_used_usd(m, rows: Iterable[dict]) -> float:
    """The allocation in use at cost; an unknown basis counts as one full ticket."""
    return sum(float(r["entry_usd"]) if r.get("entry_usd") is not None
               else float(m.ticket_usd) for r in rows)


def _capacity_refusal(m, rows: List[dict], *, held: bool, add: float, ts: float,
                      path: Optional[str]) -> Optional[str]:
    """Slots, allocation at cost and the daily entry count — the room for one
    more entry of ``add`` USD. Shared by :func:`entry_refusal` and :func:`hunt_refusal`."""
    from core.trading_mandate import MandateUnreadable, entries_since
    if not held and len(rows) >= int(m.slots):
        return (f"refused: trading mandate {m.id} has all {m.slots} slots in use — "
                f"close a position before a new entry")
    used = cost_used_usd(m, rows)
    if used + add > float(m.budget_usd) + _EPS:
        return (f"refused: trading mandate {m.id} allocation is used — ${used:.2f} at cost "
                f"of ${m.budget_usd:.2f}; this entry adds ${add:.2f}")
    try:
        breakers = m.effective_preset().breakers
    except Exception as exc:
        return f"refused: trading mandate {m.id} has invalid numbers ({exc}); failing closed"
    if breakers.max_entries_per_day:
        try:
            booked = entries_since(m.id, ts - _DAY, path=path)
        except MandateUnreadable as exc:
            return f"refused: the trading mandate could not be read ({exc}); failing closed"
        seen = sum(1 for r in rows if float(r["entry_ts"]) >= ts - _DAY)
        if max(booked, seen) >= int(breakers.max_entries_per_day):
            return (f"refused: trading mandate {m.id} reached its "
                    f"{breakers.max_entries_per_day} entries in 24 h")
    return None


@dataclass(frozen=True)
class HuntReadiness:
    """May the mandate's hunt take ONE more entry now? ``ready=None`` = the
    mandate or the book could not be read (unknown, not "no")."""

    ready: Optional[bool]
    reason: str = ""
    #: ``(chain, address)`` of the mandate's open rows (the hunt skips them).
    held: Tuple[Tuple[str, str], ...] = ()
    chains: Tuple[str, ...] = ()


def hunt_refusal(mandate_id: str, *, user_id: str, now: Optional[float] = None,
                 path: Optional[str] = None,
                 book_path: Optional[str] = None) -> HuntReadiness:
    """The hunt's $0 skip signal (W3 ``defi_data_discovery_count``): the same
    status, slot, allocation and daily-entry rules as :func:`entry_refusal`, for
    one more entry at the stage ticket on any mandate chain. Never raises."""
    from core.trading_mandate import MandateUnreadable, get, stage_ticket
    ts = time.time() if now is None else float(now)
    try:
        m = get(mandate_id, path=path)
    except MandateUnreadable as exc:
        return HuntReadiness(None, f"the trading mandate could not be read ({exc})")
    if m is None:
        return HuntReadiness(False, f"trading mandate {mandate_id} does not exist")
    if str(m.user_id) != str(user_id or ""):
        return HuntReadiness(False, f"trading mandate {m.id} belongs to another user")
    why = _status_refusal(m, ts)
    if why:
        return HuntReadiness(False, why, chains=m.chains)
    try:
        rows, _sold = read_mandate_book(m, user_id=user_id, book_path=book_path)
    except Exception as exc:
        return HuntReadiness(None, f"the position book could not be read ({exc})",
                             chains=m.chains)
    held = tuple((r["chain"], str(r.get("address") or "").lower()) for r in rows)
    try:
        ticket = stage_ticket(m)
    except Exception:
        ticket = float(m.ticket_usd)
    why = _capacity_refusal(m, rows, held=False, add=ticket, ts=ts, path=path)
    if why and "could not be read" in why:
        return HuntReadiness(None, why, held, m.chains)
    return HuntReadiness(why is None, why or "ready", held, m.chains)


#: An entry may run this much over the stage ticket (the quote moves between the
#: size decision and the fill): 5 % plus five cents.
STAGE_TOLERANCE_PCT = 5.0
STAGE_TOLERANCE_USD = 0.05


def _stage_refusal(m, usd: Optional[float]) -> Optional[str]:
    """One entry costs at most the promotion stage's ticket (086 §3.8). An
    unknown entry size is not judged here (the capacity rules count it)."""
    from core.trading_mandate import stage_ticket
    if usd is None:
        return None
    try:
        cap = stage_ticket(m)
    except Exception as exc:
        return f"refused: trading mandate {m.id} has invalid numbers ({exc}); failing closed"
    if float(usd) > cap * (1.0 + STAGE_TOLERANCE_PCT / 100.0) + STAGE_TOLERANCE_USD:
        return (f"refused: trading mandate {m.id} is at the {m.stage} stage — one entry costs "
                f"at most ${cap:.2f} now (this one ${float(usd):.2f})")
    return None


def entry_refusal(mandate_id: str, *, user_id: str, chain: str, token: Optional[str],
                  usd: Optional[float], now: Optional[float] = None,
                  path: Optional[str] = None, book_path: Optional[str] = None) -> Optional[str]:
    """None when the mandate admits this entry; else the refusal sentence."""
    from core.trading_mandate import MandateUnreadable, entries_since, get
    ts = time.time() if now is None else float(now)
    try:
        m = get(mandate_id, path=path)
    except MandateUnreadable as exc:
        return f"refused: the trading mandate could not be read ({exc}); failing closed"
    if m is None:
        return (f"refused: this run belongs to trading mandate {mandate_id}, which does "
                f"not exist; failing closed")
    if str(m.user_id) != str(user_id or ""):
        return f"refused: trading mandate {m.id} belongs to another user"
    why = _status_refusal(m, ts)
    if why:
        return why
    c = str(chain or "").strip().lower()
    if c not in m.chains:
        return (f"refused: trading mandate {m.id} trades on {', '.join(m.chains)} only, "
                f"not {c or 'an unnamed chain'}")
    try:
        rows, sold_at = read_mandate_book(m, user_id=user_id, chain=c, token=token,
                                          book_path=book_path)
    except Exception as exc:
        return f"refused: the position book could not be read ({exc}); failing closed"
    why = _stage_refusal(m, usd)
    if why:
        return why
    held = any(r["is_token"] for r in rows)
    why = _capacity_refusal(m, rows, held=held, add=float(usd or 0.0), ts=ts, path=path)
    if why:
        return why
    breakers = m.effective_preset().breakers
    if token and breakers.token_cooldown_h and not held and sold_at is not None \
            and sold_at >= ts - float(breakers.token_cooldown_h) * 3600.0:
        return (f"refused: {token} on {c} was sold less than "
                f"{breakers.token_cooldown_h:g} h ago — no re-entry yet (token cooldown)")
    return None


def evaluate_breakers(mandate, realized_rows: Sequence[ClosedTrade], now: float, *,
                      llm_cost_usd: Optional[float] = None, preset=None) -> Optional[str]:
    """The breaker that trips now, or None. PURE.

    * ``drawdown`` — realized equity fell ``drawdown_pct`` of the allocation from its
      peak since the mandate began (or since the owner last released a breaker —
      every P&L test starts at that baseline, ``llm_cost_usd`` is counted from it);
    * ``daily_loss`` — realized P&L of the last 24 h is a loss of ``daily_loss_pct``
      of the allocation;
    * ``cost_over_alloc`` — the mandate's LLM spend reached ``cost_over_alloc_pct``
      of the allocation;
    * ``consecutive_losses`` — the last ``max_consecutive_losses`` closes since the
      previous cooldown began were all losses (and no cooldown is running).
    """
    from core.trading_mandate import ACTIVE
    if mandate.status != ACTIVE:
        return None
    b = (preset or mandate.effective_preset()).breakers
    budget = float(mandate.budget_usd)
    since = max(float(mandate.created_at), float(getattr(mandate, "released_at", None) or 0.0))
    rows: List[ClosedTrade] = sorted(
        (r for r in realized_rows
         if r.pnl_usd is not None and float(r.closed_at) >= since - _EPS
         and float(r.closed_at) <= float(now) + _EPS),
        key=lambda r: float(r.closed_at))
    if b.drawdown_pct:
        equity = peak = 0.0
        worst = 0.0
        for r in rows:
            equity += float(r.pnl_usd)
            peak = max(peak, equity)
            worst = max(worst, peak - equity)
        if worst >= budget * float(b.drawdown_pct) / 100.0 - _EPS and worst > 0:
            return "drawdown"
    if b.daily_loss_pct:
        day = sum(float(r.pnl_usd) for r in rows if float(r.closed_at) >= float(now) - _DAY)
        if day < 0 and -day >= budget * float(b.daily_loss_pct) / 100.0 - _EPS:
            return "daily_loss"
    if b.cost_over_alloc_pct and llm_cost_usd is not None:
        if float(llm_cost_usd) >= budget * float(b.cost_over_alloc_pct) / 100.0 - _EPS:
            return "cost_over_alloc"
    if b.max_consecutive_losses:
        cooling = (mandate.cooldown_until is not None
                   and float(now) < float(mandate.cooldown_until))
        if not cooling:
            since = float(mandate.cooldown_at) if mandate.cooldown_at is not None else None
            tail = [r for r in rows if since is None or float(r.closed_at) > since]
            n = int(b.max_consecutive_losses)
            if len(tail) >= n and all(float(r.pnl_usd) < 0 for r in tail[-n:]):
                return "consecutive_losses"
    return None
