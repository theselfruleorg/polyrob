"""The trading mandate's lifecycle (086 W4, §3.3) — code, not prose.

Two triggers, one set of rules:

* a **position-event listener** (``core.position_events``), registered at boot —
  ``opened`` on a mandate position -> arm its exit job and book the entry
  (``core.trading_mandate.record_entry``, ACTIVE mandates only); a terminal kind
  (``closed`` · ``written_off`` · ``retired`` · ``dust``) -> cancel that
  position's exit job (``via="mandate"``);
* a **5-minute pull pass** (an autonomy-loop ticker; its first tick at boot is
  the cold-start reconcile) that repairs what an event missed:

  ===========================================  ===================================
  finding                                      action
  ===========================================  ===================================
  held mandate position, no exit job           UNPROTECTED -> arm it, tell the
                                               owner once
  exit job, its position closed / dust / gone  ORPHAN -> cancel (``via="orphan"``)
  an open row priced under ``DUST_USD``        store ``dust`` (``set_status``)
  a breaker trips on realized P&L              latch / cooldown; pause the hunt;
                                               exits run; ONE card "release /
                                               keep halted"
  the promotion ladder moves                   ``set_stage`` + tell the owner
  mandate ``stopped``                          cancel its hunt + review; exits run
  ===========================================  ===================================

What counts as a mandate position is the money kernel's own selection
(``core.money.mandate_gate.read_mandate_book``). Where an exit job's money
authority comes from: ``polyrob/app/trading_jobs.py``.

⚠️ Fail safe: an unreadable mandate store, book or cron store does NOTHING
destructive for that mandate — it is logged and the next pass retries. An agent
that cancels an exit job (``cronjob_cancel`` does not refuse it) is repaired by
the next pass. Idempotent: every write checks the state it changes first, and a
note (``lifecycle_notes`` in ``trading.db``) makes each owner message once.

Composition root.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

INTERVAL_SEC = 300
#: A row is re-priced for the dust check at most this often.
PRICE_EVERY_S = 1800

PriceFn = Callable[[str, str], Optional[float]]

#: Re-entrant: a dust mark inside the pass fires the listener on this thread.
_LOCK = threading.RLock()
_STATE: Dict[str, Any] = {"data_dir": None}

_NOTES_SCHEMA = """
CREATE TABLE IF NOT EXISTS lifecycle_notes (
    mandate_id TEXT NOT NULL,
    note       TEXT NOT NULL,
    ts         REAL NOT NULL,
    PRIMARY KEY (mandate_id, note)
);
"""


@dataclass
class Notice:
    user_id: str
    text: str
    card: Any = None


@dataclass
class PassResult:
    armed: List[str] = field(default_factory=list)
    orphaned: List[str] = field(default_factory=list)
    dust: List[str] = field(default_factory=list)
    breakers: List[str] = field(default_factory=list)
    stopped: List[str] = field(default_factory=list)
    promoted: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    notices: List[Notice] = field(default_factory=list)

    def changed(self) -> bool:
        return bool(self.armed or self.orphaned or self.dust or self.breakers
                    or self.stopped or self.promoted)


# -- once-only notes -----------------------------------------------------------------

def _first_note(mandate_id: str, note: str, *, path: Optional[str]) -> bool:
    """True the FIRST time (mandate, note) is noted. A store fault answers False
    (say nothing rather than repeat)."""
    try:
        from core.sqlite_util import execute_retry, init_schema
        from core.trading_mandate import init_schema as init_mandates
        p = init_mandates(path)
        init_schema(p, _NOTES_SCHEMA, mkdir=True)
        before = execute_retry(p, "SELECT 1 FROM lifecycle_notes WHERE mandate_id=? AND note=?",
                               (mandate_id, note), fetch="one")
        if before:
            return False
        execute_retry(p, "INSERT OR IGNORE INTO lifecycle_notes (mandate_id, note, ts) "
                      "VALUES (?,?,?)", (mandate_id, note, time.time()))
        return True
    except Exception:
        logger.warning("trading lifecycle: note %s/%s not stored", mandate_id, note,
                       exc_info=True)
        return False


def _set_note(mandate_id: str, note: str, *, path: Optional[str], ts: float) -> None:
    try:
        from core.sqlite_util import execute_retry, init_schema
        from core.trading_mandate import init_schema as init_mandates
        p = init_mandates(path)
        init_schema(p, _NOTES_SCHEMA, mkdir=True)
        execute_retry(p, "INSERT OR REPLACE INTO lifecycle_notes (mandate_id, note, ts) "
                      "VALUES (?,?,?)", (mandate_id, note, float(ts)))
    except Exception:
        logger.warning("trading lifecycle: note %s/%s not stored", mandate_id, note,
                       exc_info=True)


def _note_ts(mandate_id: str, note: str, *, path: Optional[str]) -> Optional[float]:
    try:
        from core.sqlite_util import execute_retry
        from core.trading_mandate import db_path
        r = execute_retry(path or db_path(), "SELECT ts FROM lifecycle_notes WHERE "
                          "mandate_id=? AND note=?", (mandate_id, note), fetch="one")
        return float(r["ts"]) if r else None
    except Exception:
        return None


def _paths(mandate_path: Optional[str], book_path: Optional[str]):
    """The stores of the data home this process was started on."""
    data_dir = _STATE.get("data_dir")
    if mandate_path is None:
        from core.trading_mandate import db_path
        mandate_path = db_path(data_dir)
    if book_path is None:
        from core.open_positions import open_positions_db_path
        book_path = open_positions_db_path(data_dir)
    return mandate_path, book_path


# -- the listener -------------------------------------------------------------------

def _mandate_rows(m, book_path: Optional[str]):
    from core.money.mandate_gate import read_mandate_book
    rows, _ = read_mandate_book(m, user_id=m.user_id, book_path=book_path)
    return rows


def _row_key(r) -> Tuple[str, str, str]:
    from polyrob.app.trading_jobs import _key
    return _key(r.get("chain"), r.get("account", ""), r.get("address"))


def on_event(event, *, svc=None, mandate_path: Optional[str] = None,
             book_path: Optional[str] = None) -> Optional[str]:
    """Apply one position event. Returns what was done (``armed`` / ``cancelled``
    / ``""``). Never raises (the book write that fired it must not break)."""
    from core import position_events as pe
    from core import trading_mandate as tm
    from polyrob.app import trading_jobs as tj
    try:
        if event.kind not in (pe.OPENED,) + tuple(pe.TERMINAL_KINDS):
            return ""
        mandate_path, book_path = _paths(mandate_path, book_path)
        if not os.path.exists(mandate_path):
            return ""  # no mandate was ever made here: nothing to arm or disarm
        svc = svc or tj.service(_STATE.get("data_dir"))
        with _LOCK:
            if event.kind in pe.TERMINAL_KINDS:
                done = tj.cancel_exit(svc, user_id=event.user_id, chain=event.chain,
                                      account=event.account, address=event.address,
                                      via="mandate")
                return "cancelled" if done else ""
            if event.reason == "inherited":
                return ""
            m = tm.get_active(event.user_id, path=mandate_path)
            if m is None or str(event.chain or "").strip().lower() not in m.chains:
                return ""
            want = tj._key(event.chain, event.account, event.address)
            if want not in {_row_key(r) for r in _mandate_rows(m, book_path)}:
                return ""  # not the mandate's (another origin, before the mandate)
            if m.status == tm.ACTIVE:
                tm.record_entry(m.id, chain=want[0], token=event.address, usd=event.usd,
                                path=mandate_path)
            job = tj.arm_exit(svc, m, chain=event.chain, account=event.account,
                              address=event.address, symbol=event.symbol)
            return "armed" if job is not None else ""
    except Exception:
        logger.warning("trading lifecycle: %s event on %s/%s not applied (the pull pass "
                       "retries)", getattr(event, "kind", "?"), getattr(event, "chain", "?"),
                       getattr(event, "address", "?"), exc_info=True)
        return ""


def _listener(event) -> None:
    on_event(event)


# -- the pull pass -------------------------------------------------------------------

def _closed_trades(m, book_path: Optional[str]):
    from core.money.mandate_gate import ClosedTrade
    from core.open_positions import realized_for
    out = []
    for r in realized_for(m.user_id, db_path=book_path):
        if r.chain.strip().lower() not in m.chains or r.updated_ts < float(m.created_at):
            continue
        pnl = r.realized_usd if r.known_legs > 0 else None
        out.append(ClosedTrade(pnl_usd=pnl, closed_at=float(r.updated_ts),
                               chain=r.chain.strip().lower(), token=r.address))
    return out


_PRICED: Dict[Tuple[str, str, str], float] = {}


def _is_dust(r: dict, price_fn: Optional[PriceFn], now: float) -> bool:
    """A priced holding under ``DUST_USD`` that also lost half its cost (a $1
    probe ticket a few cents down is not dust). Unpriced = not dust."""
    from core.open_positions import DUST_USD
    if price_fn is None:
        return False
    k = _row_key(r)
    if now - _PRICED.get(k, 0.0) < PRICE_EVERY_S:
        return False
    _PRICED[k] = now
    try:
        price = price_fn(k[0], str(r.get("address") or ""))
    except Exception:
        return False
    if price is None or price <= 0:
        return False
    value = float(r.get("qty") or 0.0) * float(price)
    cost = r.get("entry_usd")
    return value < DUST_USD and (cost is None or value < 0.5 * float(cost))


def _breaker_card(m, name: str):
    from core.surfaces import cards
    body = (f"Trading mandate {m.id} tripped its {name.replace('_', ' ')} breaker. New "
            f"entries are off and the hunt job is paused; every exit job keeps running. "
            f"Confirm releases the breaker (entries resume); Dismiss keeps it halted.")
    return cards.job_card(m.user_id, ["/trade", "release", m.id],
                          title="Trading halted — release or keep halted", body=body,
                          origin=cards.ORIGIN_SYSTEM)


def _llm_cost(m, memory_db: Optional[str]) -> Optional[float]:
    from core.trading_report import mandate_llm_cost
    if memory_db is None:
        from core.runtime_paths import memory_db_path
        memory_db = memory_db_path(_STATE.get("data_dir"))
    got = mandate_llm_cost(m.user_id, m.id, memory_db=memory_db, since=m.released_at)
    return None if got is None else float(got[0])


def _breakers(m, svc, res: PassResult, *, now: float, mandate_path, book_path, memory_db):
    from core import trading_mandate as tm
    from core.money.mandate_gate import evaluate_breakers
    from polyrob.app import trading_jobs as tj
    newly = False
    if m.status == tm.ACTIVE:
        try:
            trades = _closed_trades(m, book_path)
        except Exception:
            logger.warning("trading lifecycle: realized P&L of %s unreadable — no breaker "
                           "check this pass", m.id, exc_info=True)
            return m, False
        try:
            cost = _llm_cost(m, memory_db)
        except Exception:
            logger.warning("trading lifecycle: LLM spend of %s unreadable — the "
                           "cost_over_alloc breaker is not checked this pass", m.id,
                           exc_info=True)
            cost = None
        name = evaluate_breakers(m, trades, now, llm_cost_usd=cost)
        if not name:
            return m, False
        m = tm.apply_breaker(m.id, name, path=mandate_path, now=now)
        res.breakers.append(f"{m.id}:{name}")
        newly = True
        if m.status != tm.HALTED:  # a cooldown that expires by itself
            if _first_note(m.id, f"cooldown:{m.cooldown_at}", path=mandate_path):
                res.notices.append(Notice(m.user_id, (
                    f"Trading mandate {m.id}: {name.replace('_', ' ')} — entries cool down "
                    f"for {m.effective_preset().breakers.cooldown_h or 0:g} h; exits keep "
                    f"running.")))
            return m, True
    if m.status == tm.HALTED:
        tj.set_hunt(svc, m, paused=True, via="mandate")
        if _first_note(m.id, f"halt:{m.halted_at}", path=mandate_path):
            try:
                card = _breaker_card(m, (m.halted_reason or "breaker").split(":")[-1])
            except Exception:
                logger.warning("trading lifecycle: breaker card for %s not made", m.id,
                               exc_info=True)
                card = None
            res.notices.append(Notice(m.user_id, (
                f"Trading mandate {m.id} is HALTED ({m.halted_reason}). Entries are off; "
                f"exits keep running. /trade release resumes entries."), card=card))
    return m, newly


def _promotion(m, svc, res: PassResult, tripped: bool, *, mandate_path, book_path):
    from core import trading_mandate as tm
    if not tripped and m.status != tm.ACTIVE:
        return
    try:
        trades = _closed_trades(m, book_path)
    except Exception:
        return
    since = _note_ts(m.id, "stage:small", path=mandate_path) or float(m.created_at)
    small = [t for t in trades if t.closed_at >= since and t.pnl_usd is not None]
    # A demotion back to probe needs a NEW probe close: closes before it do not count.
    demoted = _note_ts(m.id, "stage:demoted", path=mandate_path) or 0.0
    probed = {t.chain for t in trades if t.closed_at >= demoted and t.pnl_usd is not None}
    nxt = tm.next_stage(m, probe_passed_chains=probed,
                        small_realized_usd=sum(t.pnl_usd for t in small),
                        small_closed_trades=len(small), breaker_tripped=tripped)
    if nxt == m.stage:
        return
    moved = tm.set_stage(m.id, nxt, path=mandate_path)
    try:
        from polyrob.app.trading_jobs import refresh_stage
        refresh_stage(svc, moved)
    except Exception:
        logger.warning("trading lifecycle: the hunt of %s keeps its old stage line", m.id,
                       exc_info=True)
    if tripped:
        _set_note(m.id, "stage:demoted", path=mandate_path, ts=time.time())
    elif nxt == "small":  # the small window (re)starts now
        _set_note(m.id, "stage:small", path=mandate_path, ts=time.time())
    res.promoted.append(f"{m.id}:{m.stage}->{nxt}")
    why = ("a breaker tripped" if tripped else
           "the probe passed on every mandate chain" if nxt == "small" else
           "realized P&L at small tickets covers the added risk")
    res.notices.append(Notice(m.user_id, (
        f"Trading mandate {m.id}: stage {m.stage} -> {nxt} ({why}); the next entries use "
        f"${tm.stage_ticket(tm.get(m.id, path=mandate_path)):.2f} tickets.")))


def pull_pass(*, svc=None, mandate_path: Optional[str] = None, book_path: Optional[str] = None,
              memory_db: Optional[str] = None, price_fn: Optional[PriceFn] = None,
              now: Optional[float] = None) -> PassResult:
    """One reconcile of every mandate. Sync (run it in a thread). Never raises."""
    from core import trading_mandate as tm
    from core.open_positions import STATUS_DUST, set_status
    from polyrob.app import trading_jobs as tj
    res = PassResult()
    ts = time.time() if now is None else float(now)
    mandate_path, book_path = _paths(mandate_path, book_path)
    svc = svc or tj.service(_STATE.get("data_dir"))
    try:
        live = tm.live_mandates(path=mandate_path)
    except Exception:
        logger.warning("trading lifecycle: the mandate store is unreadable — nothing done",
                       exc_info=True)
        res.skipped.append("mandates")
        return res
    try:
        jobs = tj.mandate_jobs(svc)
    except Exception:
        logger.warning("trading lifecycle: the cron store is unreadable — nothing done",
                       exc_info=True)
        res.skipped.append("cron")
        return res
    by_id = {m.id: m for m in live}
    for mid in {str((j.payload or {}).get("mandate")) for j in jobs} - set(by_id):
        try:
            m = tm.get(mid, path=mandate_path)
        except Exception:
            res.skipped.append(mid)
            continue
        if m is not None:
            by_id[mid] = m
    for m in by_id.values():
        mine = [j for j in jobs if str((j.payload or {}).get("mandate")) == m.id]
        try:
            with _LOCK:
                _one_mandate(m, mine, svc, res, ts, mandate_path=mandate_path,
                             book_path=book_path, memory_db=memory_db, price_fn=price_fn,
                             set_status=set_status, dust_status=STATUS_DUST)
        except Exception:
            logger.warning("trading lifecycle: mandate %s not reconciled", m.id, exc_info=True)
            res.skipped.append(m.id)
    return res


def _one_mandate(m, mine, svc, res: PassResult, ts: float, *, mandate_path, book_path,
                 memory_db, price_fn, set_status, dust_status) -> None:
    from core import trading_mandate as tm
    from core.cron_lanes import LaneStopped
    from polyrob.app import trading_jobs as tj
    if m.status == tm.STOPPED:
        done = tj.cancel_programme(svc, m, via="mandate")
        if done:
            res.stopped += done
    exits = [j for j in mine if tj.role(j) == tj.EXIT and tj.holding(j)]
    try:
        rows = _mandate_rows(m, book_path)
    except Exception:
        logger.warning("trading lifecycle: the book of %s is unreadable — no arm, no orphan "
                       "cancel this pass", m.id, exc_info=True)
        res.skipped.append(f"{m.id}:book")
        return
    held = {}
    for r in rows:
        if _is_dust(r, price_fn, ts):
            k = _row_key(r)
            if set_status(m.user_id, k[0], str(r.get("address") or ""), dust_status,
                          reason=f"worth under the dust line (lifecycle {m.id})",
                          account=k[1], db_path=book_path):
                res.dust.append(f"{k[0]}:{k[2]}")
            continue
        held[_row_key(r)] = r
    for j in exits:
        if tj.position_key(j) not in held:
            if svc.cancel(j.id, user_id=m.user_id, via="orphan"):
                res.orphaned.append(j.id)
        else:
            try:
                tj.refresh_plan(svc, j, m.user_id)  # the plan lands after the book row
            except Exception:
                logger.warning("trading lifecycle: exit job %s keeps no plan line", j.id,
                               exc_info=True)
    if m.status != tm.STOPPED:
        armed_keys = {tj.position_key(j) for j in exits}
        for k, r in held.items():
            if k in armed_keys:
                continue
            try:
                job = tj.arm_exit(svc, m, chain=k[0], account=k[1],
                                  address=str(r.get("address") or ""))
            except LaneStopped as exc:
                if _first_note(m.id, f"lane_stopped:{k}", path=mandate_path):
                    res.notices.append(Notice(m.user_id, (
                        f"UNPROTECTED: {r.get('address')} on {k[0]} (mandate {m.id}) has no "
                        f"exit job, and I cannot arm one: {exc}")))
                continue
            if job is None:
                continue
            res.armed.append(job.id)
            if _first_note(m.id, f"unprotected:{k}", path=mandate_path):
                res.notices.append(Notice(m.user_id, (
                    f"UNPROTECTED -> armed: {r.get('address')} on {k[0]} (mandate {m.id}) had "
                    f"no exit job; job {job.id} now watches it.")))
    tripped = False
    if m.status in (tm.ACTIVE, tm.HALTED):
        m, tripped = _breakers(m, svc, res, now=ts, mandate_path=mandate_path,
                               book_path=book_path, memory_db=memory_db)
    _promotion(m, svc, res, tripped, mandate_path=mandate_path, book_path=book_path)


# -- delivery + wiring ------------------------------------------------------------------

async def deliver(container: Any, notices: List[Notice]) -> None:
    from core.surfaces import cards
    from core.surfaces.user_delivery import deliver_user_message
    for n in notices:
        try:
            if n.card is not None:
                await deliver_user_message(container, n.user_id, cards.render_text(n.card),
                                           source="action_card", card_id=n.card.card_id)
            await deliver_user_message(container, n.user_id, n.text, source="trading")
        except Exception:
            logger.warning("trading lifecycle: an owner notice was not delivered",
                           exc_info=True)


def build_ticker(task_agent: Any, data_dir: Optional[str]):
    from core.tickers import IntervalTicker
    from polyrob.app.trading_jobs import spend_grade_price
    container = getattr(task_agent, "container", None)

    async def _tick():
        res = await asyncio.to_thread(pull_pass, price_fn=spend_grade_price)
        if res.changed():
            logger.info("trading lifecycle: armed=%s orphaned=%s dust=%s breakers=%s "
                        "stopped=%s promoted=%s", res.armed, res.orphaned, res.dust,
                        res.breakers, res.stopped, res.promoted)
        if res.notices:
            await deliver(container, res.notices)

    return IntervalTicker(_tick, interval_seconds=INTERVAL_SEC)


def start(handles: Any, task_agent: Any, data_dir: Optional[str]) -> None:
    """Boot wiring (``polyrob.app.autonomy_runtime.start_autonomy``): the listener
    and the pull-pass loop."""
    from core.position_events import register_listener
    _STATE["data_dir"] = data_dir
    register_listener(_listener)
    handles._add("trading", build_ticker(task_agent, data_dir))


def stop() -> None:
    from core.position_events import unregister_listener
    unregister_listener(_listener)


__all__ = ["INTERVAL_SEC", "Notice", "PassResult", "build_ticker", "deliver", "on_event",
           "pull_pass", "start", "stop"]
