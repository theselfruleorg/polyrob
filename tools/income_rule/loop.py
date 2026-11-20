"""The income rule's loop — debt first, buy second (proposal 122 W4).

The ``income_rule`` loop (``polyrob/app/autonomy_runtime.py``, every :data:`INTERVAL_SEC`)
calls :func:`tick`. No rule file, no work (the loop never creates it). Each tick:

1. reads the income of the rule's kinds from the existing records (``sources.read_all``);
2. under the store lock: adds P % of each new income to the debt, follows a buy queued on the
   owner's decision, and starts ONE buy when it is due (the balance passed the minimum, or once
   a day) — the buy row is written ``running`` BEFORE the act, so a restart never runs it twice;
3. runs the buy through the normal money path: ``defi_trade_swap`` (native → the pinned token,
   held in the treasury) in the ONE autonomous-act runner (``tools/autonomous_act.py``: the
   guard, the caps, the pause and the lanes apply);
4. books the end (``tools.autonomous_act.outcome``): done pays the held income; a failure (a
   revert too) gives it back to the debt and says so; an end the result does not name (a
   broadcast not confirmed, a send that may have landed) is ``unknown``: the income stays held
   and nothing is bought again until the owner settles it;
   an act the lane sends to the owner becomes the owner's decision (a ``/swap`` proposal) and
   the buy waits ``queued`` — code never forces it.

The owner gets one notice per buy (done, failed, queued, unknown) and one when an income
could not be valued for a day. NOT pause-gated as a loop: the buy meets the pause in the guard.
"""
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional

from core.state.files import file_lock
from core.wallet import income_rule as R

logger = logging.getLogger(__name__)

INTERVAL_SEC = 300
SOURCE = "income_rule"
ACTION, TOOL = "defi_trade_swap", "defi_trade"
#: The declared ceiling over the debt it pays: the price may move and the guard counts gas.
BUY_MARGIN = 1.1
#: A ``running`` buy older than this was cut by a restart: its end is unknown.
RUNNING_STALE_SEC = 3600


def _owner() -> str:
    from core.instance import resolve_owner_user_id
    return resolve_owner_user_id() or ""


def buy_params(rule: R.Rule, usd: float, native_usd: float) -> Dict[str, Any]:
    """The exact swap of *usd* of native into the pinned token."""
    amount = int(usd / native_usd * 1e8) / 1e8
    return {"chain": rule.chain, "token_in": "native", "token_out": rule.token,
            "amount_in": amount, "max_spend_usd": round(usd * BUY_MARGIN + 0.01, 2)}


def proposal_words(params: Dict[str, Any]) -> List[str]:
    """The owner's ``/swap`` line for the same buy (a proposal never carries ``go``)."""
    return [f"{params['amount_in']:.8f}".rstrip("0").rstrip("."), "native", "to",
            params["token_out"], "on", params["chain"], "max", f"{params['max_spend_usd']:.2f}"]


# ---------------------------------------------------------------------------- the owner's decision

def _descendants(st, user_id: str, root: str, since: float) -> List[Any]:
    cards = st.changed_since(user_id, since - 1, limit=60)
    ids, out, grew = {root}, [], True
    while grew:
        grew = False
        for c in cards:
            if c.card_id not in ids and c.parent_id in ids:
                ids.add(c.card_id)
                out.append(c)
                grew = True
    return out


def _reverted(text: Optional[str]) -> bool:
    """A sent act whose result names a revert or an expiry: it did not happen."""
    from types import SimpleNamespace
    from tools.autonomous_act import FAILED, outcome
    return outcome(SimpleNamespace(extracted_content=str(text or ""), error=None,
                                   metadata=None), live=True) == FAILED


def follow_queued(state: Dict[str, Any], user_id: str, st, now: float) -> Optional[Dict]:
    """Move a buy queued on the owner's decision on, from the cards' own outcomes. Returns the
    buy when it ended (or became unknown), else None."""
    from core.surfaces.card_states import (OUTCOME_SENT, OUTCOME_UNKNOWN, S_CONFIRMED,
                                           S_OPEN)
    from tools.autonomous_act import TX_REF
    buy = R.open_buy(state)
    if buy is None or buy.get("state") != R.B_QUEUED:
        return None
    root = st.get(buy["card"])
    if root is None:
        return R.finish_buy(state, buy["id"], ok=False, now=now,
                            text="the owner's decision is gone (pruned); the debt is owed again")
    kids = _descendants(st, user_id, root.card_id, float(buy["at"]))
    for c in kids:
        if c.outcome == OUTCOME_SENT:
            ref = TX_REF.search(c.result or "")
            if _reverted(c.result):   # sent and reverted: the debt stays (audit M-4)
                return R.finish_buy(state, buy["id"], ok=False, now=now,
                                    text=f"the owner's buy reverted: {str(c.result)[:600]}",
                                    tx=ref.group(0) if ref else "")
            return R.finish_buy(state, buy["id"], ok=True, now=now,
                                text=f"the owner confirmed it: {str(c.result)[:600]}",
                                tx=ref.group(0) if ref else "")
    if any(c.outcome == OUTCOME_UNKNOWN for c in kids):
        return R.mark_unknown(state, buy["id"],
                              "the owner's buy may have landed and its end is unknown")
    alive = [c for c in [root] + kids if c.state in (S_OPEN, S_CONFIRMED)
             and not c.expired(now)]
    if alive:
        return None
    return R.finish_buy(state, buy["id"], ok=False, now=now,
                        text=f"the owner's decision {root.card_id} ended with no buy "
                             f"({root.state}); the debt is owed again")


# ---------------------------------------------------------------------------- the tick

def render(buy: Dict[str, Any], state: Dict[str, Any], rule: Optional[R.Rule]) -> str:
    head = {R.B_DONE: "✅ Income rule buy done", R.B_FAILED: "⚠️ Income rule buy failed",
            R.B_QUEUED: "🔔 Income rule buy needs your decision",
            R.B_UNKNOWN: "⚠️ Income rule buy: end unknown"}.get(buy["state"], "Income rule buy")
    lines = [f"{head} — {R.buy_line(buy)}"]
    if buy.get("text"):
        lines.append(str(buy["text"])[:600])
    if buy["state"] == R.B_FAILED:
        lines.append(f"The debt stays: ${R.owed_usd(state):.2f} owed to the rule.")
    lines.extend(R.status_lines(state, rule=rule, limit=1)[1:])
    return "\n".join(lines)


async def _deliver(container, user_id: str, text: str, deliver=None) -> None:
    try:
        if deliver is None:
            from core.surfaces.user_delivery import deliver_user_message as deliver
        await deliver(container, user_id, text, source=SOURCE)
    except Exception:  # noqa: BLE001
        logger.warning("income rule: the owner notice failed", exc_info=True)


async def _run_buy(buy: Dict[str, Any], rule: R.Rule, *, user_id: str, price: Callable,
                   run=None) -> Dict[str, Any]:
    """``{"ok", "text", "tx_ref", "needs_owner", "params"}`` — never raises."""
    import asyncio
    unit = await asyncio.to_thread(price, rule.chain)
    if not unit:
        return {"ok": False, "needs_owner": False, "tx_ref": "", "params": None,
                "text": f"the native price on {rule.chain} could not be read; nothing was sent"}
    params = buy_params(rule, float(buy["usd"]), float(unit))
    why = (f"income rule {rule.id}: {rule.percent:g} % of {len(buy['from'])} income to "
           f"{rule.symbol or rule.token}")
    if run is None:
        from tools.autonomous_act import run_act as run
    res = await run(action=ACTION, tool_id=TOOL, params=params, why=why, user_id=user_id,
                    session_id=f"income-{buy['id']}", source=f"income rule {rule.id}",
                    live=True)
    return dict(res, params=params, why=why)


def _native_price(chain: str) -> Optional[float]:
    from tools.defi.price_sources import native_spend_price
    return native_spend_price(chain)


async def tick(container: Any = None, *, user_id: Optional[str] = None,
               path: Optional[str] = None, now: Optional[float] = None, read=None,
               run=None, price: Optional[Callable] = None, cards=None,
               deliver=None) -> Dict[str, Any]:
    """One pass. Never raises. Returns what it did (for the log and the tests)."""
    import asyncio
    out: Dict[str, Any] = {"added": 0, "buy": None, "errors": []}
    try:
        p = path or R.store_path()
        if not os.path.exists(p):
            return out              # no rule was ever set: never create the file here
        user_id = user_id or _owner()
        now = time.time() if now is None else now
        try:
            first = await asyncio.to_thread(R.load, p)
        except Exception as exc:  # noqa: BLE001 — unreadable is not "no rule"
            logger.warning("income rule: %s is unreadable (%s); nothing moved", p, exc)
            out["errors"].append(f"store unreadable: {exc}")
            return out
        rule = R.rule_of(first)
        events, totals = [], None
        if rule is not None:
            reader = read or _read
            events, totals, errors = await reader(rule, user_id=user_id)
            out["errors"] += errors
            for e in errors:
                logger.warning("income rule: %s", e)
        st = cards
        if st is None:
            from core.surfaces import cards as _cards
            st = _cards.store()
        notices: List[str] = []
        with file_lock(p):
            state = R.load(p)
            ended = follow_queued(state, user_id, st, now)
            if ended is not None:
                notices.append(render(ended, state, rule))
            for b in state.get("buys") or ():
                if b.get("state") == R.B_RUNNING and now - float(b.get("at") or 0) > RUNNING_STALE_SEC:
                    R.mark_unknown(state, b["id"], "the buy started and its end is unknown "
                                                   "(a restart?): check the transaction")
                    notices.append(render(b, state, rule))
            buy = None
            if rule is not None:
                if totals is not None:
                    events = list(events) + R.trading_events(state, totals, now)
                added = R.accrue(state, rule, events, now)
                out["added"] = len(added)
                gave_up = [e for e in added if e.get("state") == R.UNVALUED]
                if gave_up:
                    notices.append("⚠️ Income the rule could not value for a day (shown, not "
                                   "counted):\n" + "\n".join(f"  · {e['detail']}" for e in gave_up))
                reason = R.due(state, rule, now)
                if reason:
                    buy = dict(R.start_buy(state, rule, now), reason=reason)
            R.save(p, state)
        if buy is not None:
            out["buy"] = await _finish(buy, rule, p=p, user_id=user_id, now=now, run=run,
                                       price=price or _native_price, st=st,
                                       container=container, notices=notices)
        for text in notices:
            await _deliver(container, user_id, text, deliver)
    except Exception:  # noqa: BLE001
        logger.warning("income rule: the tick failed", exc_info=True)
        out["errors"].append("the tick failed")
    return out


async def _read(rule: R.Rule, *, user_id: str):
    from tools.income_rule.sources import read_all
    return await read_all(rule.kinds, user_id=user_id, since=rule.created_at)


async def _finish(buy, rule, *, p, user_id, now, run, price, st, container, notices) -> str:
    """Run the started buy and book its end; returns the buy's state."""
    import asyncio
    res = await _run_buy(buy, rule, user_id=user_id, price=price, run=run)
    card_id, text = "", res["text"]
    if res.get("needs_owner") and res.get("params"):
        try:
            from core.surfaces import cards as _cards
            card = await asyncio.to_thread(
                _cards.proposal_card, user_id, "/swap", proposal_words(res["params"]),
                why=f"{res['why']}. {buy.get('reason') or ''}".strip(), st=st)
            card_id = card.card_id
            await _cards.push(container, user_id, card)
        except Exception as exc:  # noqa: BLE001
            text = f"{text} The owner's decision could not be made ({exc})."
    with file_lock(p):
        state = R.load(p)
        if card_id:
            ended = R.queue_buy(state, buy["id"], card_id,
                                f"{text} It waits for your decision {card_id}.")
        elif res.get("outcome") == "unknown":
            # It may have landed: the income stays held, never bought again (audit M-4).
            ended = R.mark_unknown(state, buy["id"], text, tx=res.get("tx_ref") or "")
        else:
            # Done only on a named success; a revert is failed and the debt stays.
            ok = res.get("outcome") == "done" if res.get("outcome") else bool(res["ok"])
            ended = R.finish_buy(state, buy["id"], ok=ok, text=text,
                                 tx=res.get("tx_ref") or "", now=now)
        R.save(p, state)
    notices.append(render(ended, state, rule))
    return ended["state"]


__all__ = ["INTERVAL_SEC", "buy_params", "follow_queued", "proposal_words", "render", "tick"]
