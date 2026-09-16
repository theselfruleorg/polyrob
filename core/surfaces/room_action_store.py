"""Durable offer rows for 046 paid room actions.

One row per offer, in its own sidecar DB (WAL + jittered retry via
`core/sqlite_util`).

⚠️ Every status transition is DURABLE. A marker held in a process attribute
re-arms on restart, which is exactly how the empty-pipeline escalation kept
paging the owner across 14 restarts (031). An offer that was PAID and not yet
applied is an obligation; it has to survive a crash.

⚠️ ``amount_raw`` is TEXT: an 18-decimal amount exceeds SQLite's signed 64-bit
INTEGER range.
"""
from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass
from typing import List, Optional

import logging

from core.sqlite_util import execute_retry

logger = logging.getLogger(__name__)

#: ``pending`` -> ``paid`` -> ``applied``, or to a terminal failure.
#: ``refused`` is a mint that never happened; ``redeemed`` is a CREDIT that has
#: been spent on a later free offer (046 phase 2 — before it existed the credit
#: a failed action wrote could never be honoured by anything).
#: ⚠️ No ``failed``. It shipped in the vocabulary and nothing ever wrote it —
#: every apply failure pays a CREDIT — and a status a seat may write but nothing
#: produces is the same dead-key class as the credit that had no redeemer.
STATUSES = ("pending", "paid", "applied", "credited", "expired",
            "refused", "redeemed")

#: Statuses that consume a payer's / a target's daily budget.
#:
#: ⚠️ ``refused`` is absent on purpose: a refusal did not happen to anyone, and
#: must not spend the very cap it was refused by — otherwise three refused
#: attempts would lock a member out for the day.
_COUNTED = ("pending", "paid", "applied", "credited", "expired", "redeemed")

_DDL = """CREATE TABLE IF NOT EXISTS room_action_offers (
    offer_id TEXT PRIMARY KEY,
    surface TEXT NOT NULL, chat_id TEXT NOT NULL,
    verb TEXT NOT NULL, target_user_id TEXT NOT NULL,
    target_name TEXT NOT NULL DEFAULT '',
    duration_sec INTEGER NOT NULL DEFAULT 0,
    price_usd REAL NOT NULL DEFAULT 0,
    asset_id TEXT NOT NULL DEFAULT '',
    amount_raw TEXT NOT NULL DEFAULT '0',
    requester_id TEXT NOT NULL DEFAULT '',
    invoice_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    settled_at REAL NOT NULL DEFAULT 0,
    applied_at REAL NOT NULL DEFAULT 0,
    credit_id TEXT NOT NULL DEFAULT '',
    redeemed_by_offer TEXT NOT NULL DEFAULT '')"""

#: Columns added after the table first shipped. Self-healing on open (the shape
#: `modules/database/x402_tables.py` already uses) — a live 046 install created
#: `room_actions.db` without them.
_ADD_COLUMNS = (
    ("redeemed_by_offer", "TEXT NOT NULL DEFAULT ''"),
)

_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_room_action_invoice "
    "ON room_action_offers(invoice_id)",
    "CREATE INDEX IF NOT EXISTS idx_room_action_room "
    "ON room_action_offers(surface, chat_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_room_action_status "
    "ON room_action_offers(status)",
)


@dataclass(frozen=True)
class Offer:
    offer_id: str
    surface: str
    chat_id: str
    verb: str
    target_user_id: str
    target_name: str
    duration_sec: int
    price_usd: float
    asset_id: str
    amount_raw: str
    requester_id: str
    invoice_id: str
    status: str
    reason: str
    created_at: float
    settled_at: float = 0.0
    applied_at: float = 0.0
    credit_id: str = ""
    redeemed_by_offer: str = ""


def store_path(data_home: Optional[str] = None) -> str:
    """``<data_home>/room_actions.db``.

    Resolved at CALL time — an import-time bind breaks ``-P``/profile selection
    (pinned by tests/test_home_binding_ratchet.py).
    """
    from core.runtime_paths import data_dir_or_home
    return os.path.join(data_dir_or_home(data_home), "room_actions.db")


class OfferStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        execute_retry(db_path, _DDL)
        self._add_missing_columns()
        for sql in _IDX:
            execute_retry(db_path, sql)

    def _add_missing_columns(self) -> None:
        """Bring an older `room_actions.db` up to the current shape. Fail-open:
        a column that cannot be added degrades the feature that needs it, never
        the store."""
        try:
            rows = execute_retry(self.db_path,
                                 "PRAGMA table_info(room_action_offers)",
                                 fetch="all") or []
            have = {str(r["name"]) for r in rows}
        except Exception:
            return
        for col, decl in _ADD_COLUMNS:
            if col in have:
                continue
            try:
                execute_retry(
                    self.db_path,
                    f"ALTER TABLE room_action_offers ADD COLUMN {col} {decl}")
            except Exception as e:      # pragma: no cover - concurrent add
                logger.debug("room_actions.db: could not add %s (%s)", col, e)

    def create(self, offer: Offer) -> None:
        d = asdict(offer)
        cols = ",".join(d)
        marks = ",".join("?" for _ in d)
        execute_retry(self.db_path,
                      f"INSERT INTO room_action_offers({cols}) VALUES({marks})",
                      tuple(d.values()))

    def _row(self, r) -> Offer:
        return Offer(**{k: r[k] for k in Offer.__dataclass_fields__})

    def get(self, offer_id: Optional[str]) -> Optional[Offer]:
        if not offer_id:
            return None
        r = execute_retry(self.db_path,
                          "SELECT * FROM room_action_offers WHERE offer_id = ?",
                          (str(offer_id),), fetch="one")
        return self._row(r) if r else None

    def by_invoice(self, invoice_id: Optional[str]) -> Optional[Offer]:
        """The offer a settled invoice buys. ⚠️ This is the ONLY link from a
        settlement back to the effect it paid for."""
        if not invoice_id:
            return None
        r = execute_retry(self.db_path,
                          "SELECT * FROM room_action_offers WHERE invoice_id = ?",
                          (str(invoice_id),), fetch="one")
        return self._row(r) if r else None

    def set_status(self, offer_id: str, status: str, *, reason: str = "",
                   invoice_id: Optional[str] = None,
                   settled_at: Optional[float] = None,
                   applied_at: Optional[float] = None,
                   credit_id: Optional[str] = None) -> bool:
        """Move one offer. An unknown status RAISES — a seat that mistypes must
        hear the vocabulary back, never write a status nothing reads."""
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}; one of {STATUSES}")
        sets = ["status = ?"]
        params: list = [status]
        for col, val in (("reason", reason or None),
                         ("invoice_id", invoice_id),
                         ("settled_at", settled_at),
                         ("applied_at", applied_at),
                         ("credit_id", credit_id)):
            if val is not None:
                sets.append(f"{col} = ?")
                params.append(val)
        params.append(str(offer_id))
        rc = execute_retry(
            self.db_path,
            f"UPDATE room_action_offers SET {', '.join(sets)} WHERE offer_id = ?",
            tuple(params))
        return bool(rc)

    def count_since(self, surface: str, chat_id: str, *,
                    requester: Optional[str] = None,
                    target: Optional[str] = None, since_ts: float = 0.0) -> int:
        """Offers in this room since *since_ts*, by payer or by target."""
        where = ["surface = ?", "chat_id = ?", "created_at >= ?",
                 f"status IN ({','.join('?' for _ in _COUNTED)})"]
        params: list = [surface, str(chat_id), float(since_ts), *_COUNTED]
        if requester:
            where.append("requester_id = ?")
            params.append(str(requester))
        if target:
            where.append("target_user_id = ?")
            params.append(str(target))
        r = execute_retry(
            self.db_path,
            f"SELECT COUNT(*) AS n FROM room_action_offers "
            f"WHERE {' AND '.join(where)}",
            tuple(params), fetch="one")
        return int(r["n"]) if r else 0

    def open_offers(self, surface: str, chat_id: str) -> List[Offer]:
        rows = execute_retry(
            self.db_path,
            "SELECT * FROM room_action_offers WHERE surface = ? AND chat_id = ? "
            "AND status = 'pending' ORDER BY created_at",
            (surface, str(chat_id)), fetch="all") or []
        return [self._row(r) for r in rows]

    def recent(self, surface: str, chat_id: str, limit: int = 20) -> List[Offer]:
        rows = execute_retry(
            self.db_path,
            "SELECT * FROM room_action_offers WHERE surface = ? AND chat_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (surface, str(chat_id), max(1, int(limit))), fetch="all") or []
        return [self._row(r) for r in rows]

    def settle_pending(self, offer_id: str, settled_at: float) -> bool:
        """``pending -> paid``, and ONLY that.

        ⚠️ `set_status` moves a row from ANY status, so a payment that arrived
        after the offer expired silently resurrected it into ``paid`` and the
        effect was applied — an action we had already told the payer would not
        happen. The status predicate is in the UPDATE, so it is also the
        concurrency guard: two watcher ticks racing one settlement, one winner.
        """
        rc = execute_retry(
            self.db_path,
            "UPDATE room_action_offers SET status = 'paid', settled_at = ? "
            "WHERE offer_id = ? AND status = 'pending'",
            (float(settled_at), str(offer_id)))
        return bool(rc)

    def consume_credit(self, credit_offer_id: str, spent_on: str) -> bool:
        """``credited -> redeemed``, atomically. False when someone else won.

        ⚠️ Nothing called `redeemable_credit` before phase 2: the failure path
        told a payer they held a credit "good for one {verb} in this room at no
        further charge" and no code path in the tree could honour it. The
        predicate is in the UPDATE because two turns can race for one credit and
        exactly one may spend it; the loser falls through to the ordinary paid
        path rather than being refused — they still want the action.
        """
        rc = execute_retry(
            self.db_path,
            "UPDATE room_action_offers SET status = 'redeemed', "
            "redeemed_by_offer = ? WHERE offer_id = ? AND status = 'credited'",
            (str(spent_on), str(credit_offer_id)))
        return bool(rc)

    def pending_rooms(self) -> List[tuple]:
        """``[(surface, chat_id), ...]`` with at least one PENDING offer.

        The expiry sweep needs this because the TTL is a PER-ROOM setting
        (``chat.paid_offer_ttl``): a single global cutoff would override every
        room's own choice.
        """
        rows = execute_retry(
            self.db_path,
            "SELECT DISTINCT surface, chat_id FROM room_action_offers "
            "WHERE status = 'pending'", fetch="all") or []
        return [(r["surface"], r["chat_id"]) for r in rows]

    def expire_stale(self, ttl_sec: float, *, surface: Optional[str] = None,
                     chat_id: Optional[str] = None,
                     now: Optional[float] = None) -> int:
        """Move PENDING offers older than *ttl_sec* to ``expired``.

        An unpaid offer must stop inviting payment: a payer who sends late would
        pay for something nothing will apply.

        Scoped to one room when ``surface``/``chat_id`` are given, because the
        TTL belongs to the room.
        """
        cutoff = (now or time.time()) - float(ttl_sec)
        where = ["status = 'pending'", "created_at < ?"]
        params: list = [cutoff]
        if surface is not None and chat_id is not None:
            where.extend(["surface = ?", "chat_id = ?"])
            params.extend([surface, str(chat_id)])
        return int(execute_retry(
            self.db_path,
            "UPDATE room_action_offers SET status = 'expired', "
            "reason = 'unpaid past the room offer_ttl' "
            f"WHERE {' AND '.join(where)}", tuple(params)) or 0)

    def credits_owed(self, surface: Optional[str] = None) -> List[Offer]:
        """Paid offers whose effect could not be applied.

        ⚠️ Money we hold against an undelivered service. The status snapshot
        leads with these.
        """
        sql = "SELECT * FROM room_action_offers WHERE status = 'credited'"
        params: tuple = ()
        if surface:
            sql += " AND surface = ?"
            params = (surface,)
        rows = execute_retry(self.db_path, sql + " ORDER BY created_at",
                             params, fetch="all") or []
        return [self._row(r) for r in rows]

    def redeemable_credit(self, surface: str, chat_id: str, requester: str,
                          verb: str) -> Optional[Offer]:
        """An unredeemed credit this payer holds for this verb in this room."""
        r = execute_retry(
            self.db_path,
            "SELECT * FROM room_action_offers WHERE status = 'credited' "
            "AND surface = ? AND chat_id = ? AND requester_id = ? AND verb = ? "
            "ORDER BY created_at LIMIT 1",
            (surface, str(chat_id), str(requester), verb), fetch="one")
        return self._row(r) if r else None


__all__ = ["Offer", "OfferStore", "STATUSES", "store_path"]
