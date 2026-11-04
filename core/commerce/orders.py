"""The order state machine (099 W4, §3.5) — one row per order in ``commerce.db``.

::

    fixed:  awaiting_payment -> paid -> fulfilling -> delivered -> accepted
    quote:  quoted -> awaiting_payment -> …
            awaiting_payment -> expired | cancelled;   fulfilling -> paid (retry)
            paid | fulfilling -> failed;   delivered -> disputed -> accepted | failed

* **One fact, one store.** The money state lives on the invoice row
  (``x402_payment_requests`` in ``bot.db``). An order becomes ``paid`` only when
  its invoice COMPLETED (the settlement watcher calls :func:`mark_paid`); it has
  no refund state of its own — a ``failed`` order is refunded by the owner on the
  invoice (U40: refunds are owner-only in v1).
* **Idempotent fulfillment, keyed on the order.** :func:`claim_fulfillment` is a
  CAS ``paid -> fulfilling`` that also stamps a claim token in ``goal_id``; only
  the claimant enqueues the goal. A claim whose goal never got written (a crash
  between the two) is re-opened by :func:`stale_claims`.
* **The order id is the run's mandate.** The fulfillment goal carries
  ``payload["mandate"] = <order id>`` (the ``ord_`` prefix routes the money
  kernel to the offer step, ``core.money.offer_gate``), and
  :func:`reserve_spend` is the per-order envelope: a CAS that refuses once the
  confirmed ``spend_usd`` budget is used.
* **Never trust the payer address.** ``Transfer.from`` is often an exchange; an
  order delivers to its ``recipient`` input and refunds to its ``refund`` input.

Tier-0: imports nothing above ``core``.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from core.state.machine import StateMachine

QUOTED, AWAITING, PAID, FULFILLING = "quoted", "awaiting_payment", "paid", "fulfilling"
DELIVERED, ACCEPTED, DISPUTED = "delivered", "accepted", "disputed"
FAILED, EXPIRED, CANCELLED = "failed", "expired", "cancelled"

ORDER_MACHINE = StateMachine(
    "commerce_order",
    states=frozenset({QUOTED, AWAITING, PAID, FULFILLING, DELIVERED, ACCEPTED, DISPUTED,
                      FAILED, EXPIRED, CANCELLED}),
    initial=frozenset({QUOTED, AWAITING}),
    terminal=frozenset({ACCEPTED, FAILED, EXPIRED, CANCELLED}),
    transitions={QUOTED: {AWAITING, CANCELLED, EXPIRED},
                 AWAITING: {PAID, EXPIRED, CANCELLED},
                 PAID: {FULFILLING, FAILED},
                 FULFILLING: {DELIVERED, PAID, FAILED},
                 DELIVERED: {ACCEPTED, DISPUTED},
                 DISPUTED: {ACCEPTED, FAILED}})

OPEN = (QUOTED, AWAITING, PAID, FULFILLING)
#: Orders that took money (paid or later, not failed).
SOLD = (PAID, FULFILLING, DELIVERED, ACCEPTED, DISPUTED)
#: The run-id prefix the money kernel dispatches on (``core.money.offer_gate``).
ORDER_PREFIX = "ord_"
#: A claim whose goal was never written is re-opened after this long.
CLAIM_STALE_S = 180

ORDER_SCHEMA = """
CREATE TABLE IF NOT EXISTS offer_orders (
    id             TEXT PRIMARY KEY,
    offer_id       TEXT NOT NULL,
    offer_version  INTEGER NOT NULL,
    user_id        TEXT NOT NULL,
    status         TEXT NOT NULL,
    channel        TEXT NOT NULL,
    buyer          TEXT NOT NULL,
    inputs         TEXT NOT NULL DEFAULT '{}',
    amount_usd     TEXT,
    request_id     TEXT,
    payment_tx     TEXT,
    attempts       INTEGER NOT NULL DEFAULT 0,
    goal_id        TEXT,
    claimed_at     REAL,
    spend_budget   REAL NOT NULL DEFAULT 0,
    spent_usd      REAL NOT NULL DEFAULT 0,
    proof          TEXT,
    receipt        TEXT,
    error          TEXT,
    source         TEXT,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    paid_at        REAL,
    delivered_at   REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_offer_orders_request
    ON offer_orders(request_id) WHERE request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_offer_orders_offer ON offer_orders(offer_id, status);
CREATE INDEX IF NOT EXISTS idx_offer_orders_buyer ON offer_orders(channel, buyer);
CREATE TABLE IF NOT EXISTS offer_order_spend (
    order_id  TEXT NOT NULL,
    spend_key TEXT NOT NULL,
    usd       REAL NOT NULL,
    PRIMARY KEY (order_id, spend_key)
);
"""


class OrderError(ValueError):
    """A write the rules refuse."""


@dataclass(frozen=True)
class Order:
    id: str
    offer_id: str
    offer_version: int
    user_id: str
    status: str
    channel: str
    buyer: str
    inputs: Mapping[str, Any] = field(default_factory=dict)
    amount_usd: Optional[str] = None
    request_id: Optional[str] = None
    payment_tx: Optional[str] = None
    attempts: int = 0
    goal_id: Optional[str] = None
    claimed_at: Optional[float] = None
    spend_budget: float = 0.0
    spent_usd: float = 0.0
    proof: Optional[Mapping[str, Any]] = None
    receipt: Optional[Mapping[str, Any]] = None
    error: Optional[str] = None
    source: Optional[str] = None
    created_at: float = 0.0
    updated_at: float = 0.0
    paid_at: Optional[float] = None
    delivered_at: Optional[float] = None

    @property
    def is_open(self) -> bool:
        return self.status in OPEN


def _p(path: Optional[str]) -> str:
    from core.commerce.store import init_schema
    return init_schema(path)


def _read(path, sql, args, fetch):
    from core.commerce.store import read
    return read(path, sql, args, fetch)


def _j(v):
    return json.loads(v) if v else None


def _row(r) -> Order:
    return Order(id=r["id"], offer_id=r["offer_id"], offer_version=int(r["offer_version"]),
                 user_id=r["user_id"], status=r["status"], channel=r["channel"],
                 buyer=r["buyer"], inputs=_j(r["inputs"]) or {}, amount_usd=r["amount_usd"],
                 request_id=r["request_id"], payment_tx=r["payment_tx"], attempts=int(r["attempts"] or 0),
                 goal_id=r["goal_id"], claimed_at=r["claimed_at"],
                 spend_budget=float(r["spend_budget"] or 0), spent_usd=float(r["spent_usd"] or 0),
                 proof=_j(r["proof"]), receipt=_j(r["receipt"]), error=r["error"],
                 source=r["source"], created_at=float(r["created_at"]),
                 updated_at=float(r["updated_at"]), paid_at=r["paid_at"],
                 delivered_at=r["delivered_at"])


def _now(now) -> float:
    return time.time() if now is None else float(now)


# -- reads -----------------------------------------------------------------------

def is_order_id(value: Any) -> bool:
    return str(value or "").startswith(ORDER_PREFIX)


def get(order_id: str, *, path: Optional[str] = None) -> Optional[Order]:
    r = _read(path, "SELECT * FROM offer_orders WHERE id=?", (str(order_id),), "one")
    return _row(r) if r else None


def by_request(request_id: str, *, path: Optional[str] = None) -> Optional[Order]:
    r = _read(path, "SELECT * FROM offer_orders WHERE request_id=?", (str(request_id),), "one")
    return _row(r) if r else None


def list_orders(*, offer_id: Optional[str] = None, user_id: Optional[str] = None,
                channel: Optional[str] = None, buyer: Optional[str] = None,
                statuses=None, limit: int = 50, path: Optional[str] = None) -> List[Order]:
    sql, args = "SELECT * FROM offer_orders WHERE 1=1", []
    for col, val in (("offer_id", offer_id), ("user_id", user_id), ("channel", channel),
                     ("buyer", buyer)):
        if val:
            sql += f" AND {col}=?"
            args.append(str(val))
    if statuses:
        statuses = list(statuses)
        sql += f" AND status IN ({','.join('?' * len(statuses))})"
        args += statuses
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(int(limit))
    return [_row(r) for r in (_read(path, sql, tuple(args), "all") or [])]


def by_source(source: str, *, path: Optional[str] = None) -> Optional[Order]:
    """The order an external listing created for its own reference (dedupe)."""
    r = _read(path, "SELECT * FROM offer_orders WHERE source=? LIMIT 1", (str(source),), "one")
    return _row(r) if r else None


def _in(states) -> str:
    return f"({','.join('?' * len(states))})"


def paid_since(offer_id: str, since: float, *, path: Optional[str] = None) -> int:
    """Orders that took money since *since* (the ``per_day`` cap). An unpaid order
    never counts: a stranger cannot fill the day with orders nobody pays."""
    r = _read(path, f"SELECT COUNT(*) AS n FROM offer_orders WHERE offer_id=? AND paid_at>=? "
                    f"AND status IN {_in(SOLD)}", (str(offer_id), float(since), *SOLD), "one")
    return int(r["n"]) if r else 0


def sold(offer_id: str, *, path: Optional[str] = None) -> int:
    """Orders that took money (paid or later, not failed)."""
    r = _read(path, f"SELECT COUNT(*) AS n FROM offer_orders WHERE offer_id=? AND status IN "
                    f"{_in(SOLD)}", (str(offer_id), *SOLD), "one")
    return int(r["n"]) if r else 0


def paid_before(order: Order, *, since: Optional[float] = None,
                path: Optional[str] = None) -> int:
    """Sold orders of the same offer paid BEFORE *order* (ties by id): the rank that
    decides which payments a cap keeps when more paid than it allows."""
    sql = (f"SELECT COUNT(*) AS n FROM offer_orders WHERE offer_id=? AND id<>? AND status IN "
           f"{_in(SOLD)} AND (paid_at<? OR (paid_at=? AND id<?))")
    paid_at = float(order.paid_at or 0)
    args = [str(order.offer_id), order.id, *SOLD, paid_at, paid_at, order.id]
    if since is not None:
        sql += " AND paid_at>=?"
        args.append(float(since))
    r = _read(path, sql, tuple(args), "one")
    return int(r["n"]) if r else 0


def open_for_buyer(offer_id: str, channel: str, buyer: str, *,
                   path: Optional[str] = None) -> int:
    r = _read(path, f"SELECT COUNT(*) AS n FROM offer_orders WHERE offer_id=? AND channel=? AND "
                    f"buyer=? AND status IN ({','.join('?' * len(OPEN))})",
              (str(offer_id), str(channel), str(buyer), *OPEN), "one")
    return int(r["n"]) if r else 0


def stale_claims(now: Optional[float] = None, *, path: Optional[str] = None) -> List[Order]:
    """``fulfilling`` orders whose goal was never written (claim token only)."""
    rows = _read(path, "SELECT * FROM offer_orders WHERE status=? AND goal_id LIKE 'claim:%' "
                       "AND claimed_at < ?", (FULFILLING, _now(now) - CLAIM_STALE_S), "all")
    return [_row(r) for r in rows or []]


def overdue(deadline_s_fn, now: Optional[float] = None, *,
            path: Optional[str] = None) -> List[Order]:
    """``fulfilling`` orders past their offer's deadline (``deadline_s_fn(order)``)."""
    ts = _now(now)
    rows = _read(path, "SELECT * FROM offer_orders WHERE status=? AND goal_id NOT LIKE "
                       "'claim:%'", (FULFILLING,), "all") or []
    out = []
    for r in rows:
        o = _row(r)
        if o.claimed_at is not None and ts - float(o.claimed_at) > float(deadline_s_fn(o)):
            out.append(o)
    return out


def due_acceptance(window_s_fn, now: Optional[float] = None, *,
                   path: Optional[str] = None) -> List[Order]:
    ts = _now(now)
    rows = _read(path, "SELECT * FROM offer_orders WHERE status=?", (DELIVERED,), "all") or []
    return [o for o in map(_row, rows)
            if o.delivered_at is not None and ts - float(o.delivered_at) >= window_s_fn(o)]


# -- writes ------------------------------------------------------------------------

def _cas(order_id: str, to: str, froms, *, path: str, set_cols=None, where_extra: str = "",
         where_params: tuple = ()) -> bool:
    from core.state.sql import cas_transition
    return cas_transition(path, "offer_orders", key="id=?", key_params=(str(order_id),),
                          machine=ORDER_MACHINE, to=to, from_states=froms,
                          set_cols=set_cols or {}, where_extra=where_extra,
                          where_params=where_params)


def create(*, offer_id: str, offer_version: int, user_id: str, channel: str, buyer: str,
           inputs: Mapping[str, Any], amount_usd: Optional[str], spend_budget: float,
           source: Optional[str] = None, path: Optional[str] = None,
           now: Optional[float] = None) -> Order:
    """A new order: ``awaiting_payment`` with a price, ``quoted`` without one."""
    if not str(channel or "").strip() or not str(buyer or "").strip():
        raise OrderError("an order needs a channel and a buyer")
    p = _p(path)
    oid = f"{ORDER_PREFIX}{uuid.uuid4().hex[:12]}"
    ts = _now(now)
    status = AWAITING if amount_usd else QUOTED
    from core.sqlite_util import execute_retry
    execute_retry(p, "INSERT INTO offer_orders (id, offer_id, offer_version, user_id, status, "
                     "channel, buyer, inputs, amount_usd, spend_budget, source, created_at, "
                     "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (oid, str(offer_id), int(offer_version), str(user_id), status,
                   str(channel)[:40], str(buyer)[:200], json.dumps(dict(inputs or {})),
                   amount_usd, float(spend_budget), source, ts, ts))
    return get(oid, path=p)


def attach_invoice(order_id: str, request_id: str, amount_usd: str, *,
                   path: Optional[str] = None, now: Optional[float] = None) -> Order:
    """Bind the order to its invoice (and its price): quoted -> awaiting_payment, or
    an awaiting order that has none yet."""
    p = _p(path)
    o = get(order_id, path=p)
    if o is None:
        raise OrderError(f"no order {order_id}")
    cols = {"request_id": str(request_id), "amount_usd": str(amount_usd),
            "updated_at": _now(now)}
    if o.status == QUOTED:
        ok = _cas(o.id, AWAITING, [QUOTED], path=p, set_cols=cols)
    elif o.status == AWAITING and not o.request_id:
        from core.sqlite_util import execute_retry
        ok = execute_retry(p, "UPDATE offer_orders SET request_id=?, amount_usd=?, updated_at=? "
                              "WHERE id=? AND status=? AND request_id IS NULL",
                           (cols["request_id"], cols["amount_usd"], cols["updated_at"], o.id,
                            AWAITING)) == 1
    else:
        ok = False
    if not ok:
        raise OrderError(f"order {o.id} is {o.status}; it cannot take an invoice")
    return get(o.id, path=p)


def mark_paid(request_id: str, *, payment_tx: Optional[str] = None,
              path: Optional[str] = None, now: Optional[float] = None) -> Optional[Order]:
    """The invoice COMPLETED: awaiting_payment -> paid. Idempotent; returns the order
    when this call moved it, else ``None``."""
    p = _p(path)
    o = by_request(request_id, path=p)
    if o is None:
        return None
    ts = _now(now)
    if _cas(o.id, PAID, [AWAITING], path=p,
            set_cols={"paid_at": ts, "updated_at": ts, "payment_tx": payment_tx}):
        return get(o.id, path=p)
    return None


def mark_expired(request_id: str, *, path: Optional[str] = None,
                 now: Optional[float] = None) -> bool:
    p = _p(path)
    o = by_request(request_id, path=p)
    return bool(o) and _cas(o.id, EXPIRED, [QUOTED, AWAITING], path=p,
                            set_cols={"updated_at": _now(now)})


def cancel(order_id: str, *, path: Optional[str] = None, now: Optional[float] = None) -> bool:
    return _cas(order_id, CANCELLED, [QUOTED, AWAITING], path=_p(path),
                set_cols={"updated_at": _now(now)})


def claim_fulfillment(order_id: str, *, path: Optional[str] = None,
                      now: Optional[float] = None) -> Optional[str]:
    """paid -> fulfilling (attempt + 1). Returns the claim token, or ``None`` when
    another worker holds it."""
    p = _p(path)
    ts = _now(now)
    token = f"claim:{uuid.uuid4().hex[:10]}"
    from core.sqlite_util import execute_retry
    ok = _cas(order_id, FULFILLING, [PAID], path=p,
              set_cols={"goal_id": token, "claimed_at": ts, "updated_at": ts})
    if not ok:
        return None
    execute_retry(p, "UPDATE offer_orders SET attempts=attempts+1 WHERE id=?", (str(order_id),))
    return token


def reclaim(order: Order, *, path: Optional[str] = None,
            now: Optional[float] = None) -> Optional[str]:
    """Take over a stale claim (its goal was never written). It is a new attempt, so
    a fulfiller that never writes the goal runs out of attempts instead of looping."""
    p = _p(path)
    ts = _now(now)
    token = f"claim:{uuid.uuid4().hex[:10]}"
    from core.sqlite_util import execute_retry
    n = execute_retry(p, "UPDATE offer_orders SET goal_id=?, claimed_at=?, updated_at=?, "
                         "attempts=attempts+1 WHERE id=? AND status=? AND goal_id=?",
                      (token, ts, ts, order.id, FULFILLING, order.goal_id))
    return token if n == 1 else None


def set_goal(order_id: str, token: str, goal_id: str, *, path: Optional[str] = None) -> bool:
    from core.sqlite_util import execute_retry
    return execute_retry(_p(path), "UPDATE offer_orders SET goal_id=? WHERE id=? AND status=? "
                                   "AND goal_id=?",
                         (str(goal_id), str(order_id), FULFILLING, str(token))) == 1


def release_for_retry(order_id: str, *, path: Optional[str] = None,
                      now: Optional[float] = None) -> bool:
    """fulfilling -> paid, so the next tick enqueues a fresh attempt."""
    return _cas(order_id, PAID, [FULFILLING], path=_p(path),
                set_cols={"goal_id": None, "updated_at": _now(now)})


def fail(order_id: str, error: str, *, path: Optional[str] = None,
         now: Optional[float] = None) -> bool:
    return _cas(order_id, FAILED, [PAID, FULFILLING, DISPUTED], path=_p(path),
                set_cols={"error": str(error)[:500], "updated_at": _now(now)})


def reserve_spend(order_id: str, usd: float, *, key: Optional[str] = None,
                  path: Optional[str] = None) -> bool:
    """Book *usd* against the order's envelope; False when it would exceed it or the
    order is not being fulfilled. A reservation is never returned (the safe side).

    ``key`` (the transaction's idempotency key) makes it idempotent: a dry run and
    the real send of the same built transaction book the budget ONCE; the same
    key asking for more than it booked refuses."""
    from core.sqlite_util import execute_retry
    try:
        amt = float(usd)
    except (TypeError, ValueError):
        return False
    if amt < 0:
        return False
    p = _p(path)
    if key:
        prior = execute_retry(p, "SELECT usd FROM offer_order_spend WHERE order_id=? AND "
                                 "spend_key=?", (str(order_id), str(key)), fetch="one")
        if prior is not None:
            o = get(order_id, path=p)
            return bool(o and o.status == FULFILLING and amt <= float(prior["usd"]) + 1e-9)
    ok = execute_retry(p, "UPDATE offer_orders SET spent_usd=spent_usd+? WHERE id=? "
                          "AND status=? AND spent_usd+? <= spend_budget + 1e-9",
                       (amt, str(order_id), FULFILLING, amt)) == 1
    if ok and key:
        execute_retry(p, "INSERT OR IGNORE INTO offer_order_spend (order_id, spend_key, usd) "
                         "VALUES (?,?,?)", (str(order_id), str(key), amt))
    return ok


def receipt_for(order: Order, proof: Mapping[str, Any], *, payment_tx: Optional[str],
                now: float) -> Dict[str, Any]:
    """The buyer's receipt: what was bought, paid and delivered, with a content hash
    anyone can recompute (v1: an integrity hash, not a signature)."""
    body = {"order": order.id, "offer": order.offer_id, "offer_version": order.offer_version,
            "amount_usd": order.amount_usd, "invoice": order.request_id,
            "payment_tx": payment_tx, "proof": dict(proof or {}), "delivered_at": int(now)}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"))
    body["sha256"] = hashlib.sha256(canon.encode()).hexdigest()
    return body


def deliver(order_id: str, proof: Mapping[str, Any], *, accept_now: bool = False, path: Optional[str] = None,
            now: Optional[float] = None) -> Order:
    """fulfilling -> delivered (-> accepted when the offer has no acceptance window)."""
    p = _p(path)
    o = get(order_id, path=p)
    if o is None:
        raise OrderError(f"no order {order_id}")
    ts = _now(now)
    receipt = receipt_for(o, proof, payment_tx=o.payment_tx, now=ts)
    if not _cas(o.id, DELIVERED, [FULFILLING], path=p,
                set_cols={"proof": json.dumps(dict(proof or {}))[:4000],
                          "receipt": json.dumps(receipt), "delivered_at": ts,
                          "updated_at": ts}):
        raise OrderError(f"order {o.id} is {o.status}; only a fulfilling order is delivered")
    if accept_now:
        _cas(o.id, ACCEPTED, [DELIVERED], path=p, set_cols={"updated_at": ts})
    return get(o.id, path=p)


def accept(order_id: str, *, path: Optional[str] = None, now: Optional[float] = None) -> bool:
    return _cas(order_id, ACCEPTED, [DELIVERED, DISPUTED], path=_p(path),
                set_cols={"updated_at": _now(now)})


def dispute(order_id: str, reason: str, *, path: Optional[str] = None,
            now: Optional[float] = None) -> bool:
    return _cas(order_id, DISPUTED, [DELIVERED], path=_p(path),
                set_cols={"error": f"dispute: {str(reason)[:400]}", "updated_at": _now(now)})


__all__ = ["ACCEPTED", "AWAITING", "CANCELLED", "DELIVERED", "DISPUTED", "EXPIRED", "FAILED",
           "FULFILLING", "OPEN", "ORDER_MACHINE", "ORDER_PREFIX", "Order", "OrderError", "PAID",
           "QUOTED", "SOLD", "accept", "attach_invoice", "by_request", "cancel",
           "claim_fulfillment", "create", "deliver", "dispute", "fail", "get", "is_order_id",
           "list_orders", "mark_expired", "mark_paid", "open_for_buyer", "overdue", "paid_before",
           "paid_since", "receipt_for", "reclaim", "release_for_retry", "reserve_spend",
           "set_goal", "sold", "stale_claims"]
