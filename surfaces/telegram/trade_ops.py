"""``/trade`` — the owner's trading mandate (086 W5, §3.1 + §3.7).

One verb, six subcommands; the REPL twin (``cli/ui/commands/h_money_verbs.py``)
calls the same :func:`trade_reply`:

* ``start [fields]`` — the intake card (``core.trading_intake``): the echo line of
  §3.1 with the preset's numbers and ONE Confirm. ``start <draft ref>`` is the
  line that Confirm runs: it creates the mandate (``actor="owner_seat"``) AND its
  hunt + review jobs (``polyrob.app.trading_jobs``) in one operation — a job that
  cannot be written leaves no mandate behind. Exit jobs are armed by the
  lifecycle per position (``polyrob/app/trading_lifecycle.py``).
* ``status`` — the ONE trading renderer (``core.trading_report``).
* ``pause`` — entries off, exits on (the hunt job is paused); ``resume``.
* ``stop`` — final: the hunt and the review end, each exit job runs until its
  position closes; a card offers "close all". ``stop <id> close-all`` sells
  every open mandate position in one run.
* ``release`` — clear a latched breaker (owner only), the hunt resumes.

This replaced the one-goal ``/trade <task>`` (a money-granted goal per typed
line). Owner seats only: Telegram runs it after its owner gate and refuses it in
a room; nothing here widens a cap — the signer envelope and ``tx_guard`` bind
every trade the jobs make.
"""
from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Callable, List, Optional

logger = logging.getLogger(__name__)

_REF = re.compile(r"^d[0-9a-f]{10}$")

USAGE = (
    "Usage: /trade start [budget=<usd> chains=<a,b> preset=memecoin|majors ticket=<usd> "
    "slots=<n>]\n"
    "       /trade status | pause | resume | stop | release\n"
    "`start` shows ONE card with every number before anything is created; Confirm "
    "creates the mandate and its jobs (a hunt, an exit watch per open position, a daily "
    "review). Or say \"I want to trade\" in chat and I ask what is missing.")


def _start_usage() -> str:
    from core.trading_presets import PRESETS
    lines = ["Tell me the allocation and the chains, e.g.:",
             "  /trade start 150 solana,base memecoin",
             "Presets (the numbers live in the preset; the card shows them):"]
    for p in PRESETS.values():
        tick = (f"${p.ticket_usd:g} tickets" if p.ticket_usd is not None
                else f"{p.ticket_alloc_pct[1]:g}% of the allocation per ticket"
                if p.ticket_alloc_pct else "a fixed amount")
        lines.append(f"  {p.name}: {tick}, up to {p.slots} open, a hunt every "
                     f"{p.hunt_cadence_min} min")
    return "\n".join(lines)


class _Ctx:
    """The stores one reply works on (injected by tests)."""

    def __init__(self, data_dir: str, svc=None, mandate_path: Optional[str] = None,
                 price_fn: Optional[Callable] = None):
        from core.trading_mandate import db_path
        self.data_dir = data_dir
        self.mandate_path = mandate_path or db_path(data_dir)
        self._svc = svc
        self.price_fn = price_fn

    @property
    def svc(self):
        if self._svc is None:
            from polyrob.app.trading_jobs import service
            self._svc = service(self.data_dir)
        return self._svc


def trade_reply(user_id: Optional[str], data_dir: str, args: List[str], *,
                via: str = "telegram", svc=None, mandate_path: Optional[str] = None,
                price_fn: Optional[Callable] = None) -> Any:
    """The ``/trade`` answer: text, or a ``CommandReply`` carrying a card."""
    if not user_id:
        return "Only the owner can run /trade."
    words = [str(a) for a in (args or []) if str(a).strip()]
    sub = words[0].lower() if words else ""
    ctx = _Ctx(data_dir, svc, mandate_path, price_fn)
    from core.trading_mandate import MandateError, MandateUnreadable
    try:
        if sub == "start":
            return _start(user_id, words[1:], ctx, via)
        if sub == "status":
            return _status(user_id, ctx)
        if sub in ("pause", "resume", "release"):
            return _state(user_id, sub, ctx, via)
        if sub == "stop":
            return _stop(user_id, words[1:], ctx, via)
    except MandateUnreadable as exc:
        return (f"❌ The trading store could not be read ({exc}). Nothing was changed — "
                f"this is not the same as \"no mandate\".")
    except MandateError as exc:
        return f"❌ {exc}"
    return USAGE


# -- start ---------------------------------------------------------------------------

def _start(user_id: str, args: List[str], ctx: _Ctx, via: str) -> Any:
    from core.trading_mandate import get_active
    live = get_active(user_id, path=ctx.mandate_path)
    if live is not None:
        return (f"Trading mandate {live.id} is {live.status.upper()} — one live mandate at a "
                f"time. /trade status shows it; /trade stop ends it first.")
    if len(args) == 1 and _REF.match(args[0].lower()):
        return _create(user_id, args[0].lower(), ctx, via)
    from core import trading_intake as ti
    fields = ti.parse_start_args(args)
    if fields.get("budget_usd") is None or not fields.get("chains"):
        return _start_usage()
    card, _m = ti.intake_card(user_id, fields, origin="owner", path=ctx.mandate_path)
    from core.surfaces import cards
    from core.surfaces.command_reply import CommandReply
    return CommandReply(cards.render_text(card), card_id=card.card_id)


def _create(user_id: str, ref: str, ctx: _Ctx, via: str) -> str:
    """The card Confirm: the mandate AND its jobs, or neither."""
    from dataclasses import replace

    from core import trading_intake as ti
    from core import trading_mandate as tm
    from polyrob.app import trading_jobs as tj
    fields = ti.load_draft(ref, user_id=user_id, path=ctx.mandate_path)
    mid = f"m_{uuid.uuid4().hex[:12]}"
    pending = replace(tm.draft(user_id, mandate_id=mid, **fields), actor=tm.OWNER_SEAT)
    try:
        jobs = tj.create_mandate_jobs(ctx.svc, pending, via=via)
    except Exception as exc:
        for j in tj.mandate_jobs(ctx.svc, user_id=user_id, mandate_id=mid):
            ctx.svc.cancel(j.id, user_id=user_id, via=via)
        return f"❌ Nothing was started: the mandate's jobs could not be written ({exc})."
    try:
        m = tm.create(user_id, actor=tm.OWNER_SEAT, path=ctx.mandate_path, mandate_id=mid,
                      **fields)
    except Exception:
        for j in jobs:
            ctx.svc.cancel(j.id, user_id=user_id, via=via)
        raise
    ti.mark_used(ref, m.id, path=ctx.mandate_path)
    notes = [n for j in jobs for n in (getattr(j, "schedule_notes", None) or [])]
    lines = [f"✅ Trading mandate {m.id} started.", ti.echo_line(m),
             f"Jobs: hunt {jobs[0].id} ({jobs[0].schedule_spec}), review {jobs[1].id} "
             f"(daily). An exit watch is armed by itself for each position it opens.",
             "/trade status · /trade pause · /trade stop"]
    return "\n".join(lines + notes)


# -- status / pause / resume / release ------------------------------------------------

def _status(user_id: str, ctx: _Ctx) -> str:
    from core.trading_report import render_trading
    price_fn = ctx.price_fn
    if price_fn is None:
        from polyrob.app.trading_jobs import spend_grade_price
        price_fn = spend_grade_price
    return render_trading(user_id, data_dir=ctx.data_dir, mandate_path=ctx.mandate_path,
                          price_fn=price_fn)


def _state(user_id: str, sub: str, ctx: _Ctx, via: str) -> str:
    from core import trading_mandate as tm
    from polyrob.app import trading_jobs as tj
    m = tm.get_active(user_id, path=ctx.mandate_path)
    if m is None:
        return "No live trading mandate. /trade start begins one."
    if sub == "pause":
        m = tm.set_status(m.id, tm.PAUSED, actor=tm.OWNER_SEAT, path=ctx.mandate_path)
        moved = tj.set_hunt(ctx.svc, m, paused=True, via=via)
        return (f"⏸ Trading mandate {m.id} is PAUSED: no new entries"
                f"{' (hunt job paused)' if moved else ''}; every exit job keeps running. "
                f"/trade resume starts entries again.")
    if sub == "release":
        if m.status != tm.HALTED:
            return f"Trading mandate {m.id} is {m.status.upper()}, not halted — nothing to release."
        m = tm.release(m.id, actor=tm.OWNER_SEAT, path=ctx.mandate_path)
    else:
        if m.status == tm.HALTED:
            return (f"Trading mandate {m.id} is HALTED by a breaker ({m.halted_reason}). "
                    f"/trade release clears it.")
        m = tm.set_status(m.id, tm.ACTIVE, actor=tm.OWNER_SEAT, path=ctx.mandate_path)
    tj.set_hunt(ctx.svc, m, paused=False, via=via)
    return (f"▶️ Trading mandate {m.id} is ACTIVE: entries may resume on the next hunt tick; "
            f"exits run as before.")


# -- stop ------------------------------------------------------------------------------

def _stop(user_id: str, args: List[str], ctx: _Ctx, via: str) -> Any:
    from core import trading_mandate as tm
    from polyrob.app import trading_jobs as tj
    close_all = any(a.lower() in ("close-all", "close_all", "closeall") for a in args)
    ids = [a for a in args if a.lower().startswith("m_")]
    m = tm.get(ids[0], path=ctx.mandate_path) if ids else tm.get_active(
        user_id, path=ctx.mandate_path)
    if m is None or str(m.user_id) != str(user_id):
        return "No such trading mandate. /trade status shows the live one."
    lines: List[str] = []
    if m.status != tm.STOPPED:
        m = tm.set_status(m.id, tm.STOPPED, actor=tm.OWNER_SEAT, path=ctx.mandate_path)
        done = tj.cancel_programme(ctx.svc, m, via=via)
        lines.append(f"⏹ Trading mandate {m.id} is STOPPED: no new entries"
                     f"{f'; {len(done)} job(s) ended (hunt, review)' if done else ''}.")
    if close_all:
        job = tj.schedule_close_all(ctx.svc, m, via=via)
        lines.append(f"Close all: job {job.id} sells every open mandate position into the "
                     f"quote asset in about a minute. Each exit job ends when its position "
                     f"closes.")
        return "\n".join(lines)
    open_n = _open_count(m, ctx)
    if open_n == 0:
        lines.append("No open mandate position — nothing is left running.")
        return "\n".join(lines)
    lines.append(f"{open_n} open position(s) keep their exit jobs until each closes."
                 if open_n is not None else
                 "I could not read the position book, so I cannot say what is open; "
                 "every exit job keeps running until its position closes.")
    from core.surfaces import cards
    from core.surfaces.command_reply import CommandReply
    card = cards.job_card(user_id, ["/trade", "stop", m.id, "close-all"],
                          title="Close all, or keep the exits?",
                          body=("\n".join(lines) + "\nConfirm sells every open mandate "
                                "position now; Dismiss keeps each exit job running until "
                                "its position closes."),
                          origin=cards.ORIGIN_SYSTEM)
    return CommandReply(cards.render_text(card), card_id=card.card_id)


def _open_count(m, ctx: _Ctx) -> Optional[int]:
    """The mandate's open positions; None = the book could not be read."""
    try:
        from core.money.mandate_gate import read_mandate_book
        from core.open_positions import open_positions_db_path
        rows, _ = read_mandate_book(m, user_id=m.user_id,
                                    book_path=open_positions_db_path(ctx.data_dir))
        return len(rows)
    except Exception:
        logger.warning("/trade stop: the book of %s is unreadable", m.id, exc_info=True)
        return None


__all__ = ["USAGE", "trade_reply"]
