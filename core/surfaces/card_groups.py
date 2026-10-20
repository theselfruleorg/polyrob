"""Card groups — the agent comes back when the owner has acted on its cards (084).

Prod 2026-10-19: the agent raised seven payout cards, ended its turn, the owner
confirmed them one by one, and the agent never continued — nothing told it.

Every card one session raises joins that session's OPEN group (a quote, an
extra-check or a recovery card inherits the group through ``cards.lineage``).
Each card state change passes ``cards._notify``, which calls
:func:`on_transition` (a hook this module installs on import — every seat that
raises or presses cards imports it). When every card of the group is decided, ONE CAS moves
the group ``open → settled`` and writes ONE durable ``wakes.db`` row
(``kind="cards_settled"``). The owning process's wake drain runs it: an owner
chat session is woken with each card's outcome, a blocked goal is unblocked,
a cron job is only recorded (re-running a job repeats every step in it).

Group states: ``open`` → ``settled`` (row written) → ``woken`` (the chat wake
carries the outcomes) or ``consumed`` (a turn showed them first; the row is
dropped). A goal or cron group stays ``settled`` until that goal's or job's
next run shows it.

The table lives in ``cards.db`` beside the cards; the wake row in ``wakes.db``
beside it (the same data home the drain reads).
"""
from __future__ import annotations

import logging
import os
import secrets
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

G_OPEN = "open"
G_SETTLED = "settled"
G_WOKEN = "woken"
G_CONSUMED = "consumed"

ORIGIN_CHAT = "chat"
ORIGIN_GOAL = "goal"
ORIGIN_CRON = "cron"

WAKE_KIND = "cards_settled"

#: The longest outcome text a wake or a turn block carries.
OUTCOMES_MAX_CHARS = 3000
_LINE_RESULT_CHARS = 200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS card_groups (
    group_id   TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    session_id TEXT NOT NULL,
    origin     TEXT NOT NULL,
    origin_ref TEXT NOT NULL DEFAULT '',
    then_text  TEXT NOT NULL DEFAULT '',
    state      TEXT NOT NULL,
    created_at REAL NOT NULL,
    settled_at REAL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS card_groups_session ON card_groups(session_id, state);
CREATE INDEX IF NOT EXISTS card_groups_origin ON card_groups(origin, origin_ref, state);
"""


class GroupStore:
    """The ``card_groups`` table in ``cards.db``. Every state change is a CAS."""

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        from core.sqlite_util import init_schema
        init_schema(self.db_path, _SCHEMA, mkdir=True)

    def _conn(self):
        from core.sqlite_util import wal_connect
        return wal_connect(self.db_path)

    def open_group_for(self, user_id: str, session_id: str, *, origin: str = ORIGIN_CHAT,
                       origin_ref: str = "") -> str:
        """The session's OPEN group, else a new one."""
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            r = conn.execute("SELECT group_id FROM card_groups WHERE session_id=? AND "
                             "user_id=? AND state=? ORDER BY created_at DESC LIMIT 1",
                             (str(session_id), str(user_id), G_OPEN)).fetchone()
            if r is not None:
                conn.commit()
                return r["group_id"]
            gid = secrets.token_hex(5)
            now = time.time()
            conn.execute("INSERT INTO card_groups (group_id, user_id, session_id, origin, "
                         "origin_ref, state, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                         (gid, str(user_id), str(session_id), origin, str(origin_ref or ""),
                          G_OPEN, now, now))
            conn.commit()
            return gid
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get(self, group_id: str) -> Optional[Dict[str, Any]]:
        conn = self._conn()
        try:
            r = conn.execute("SELECT * FROM card_groups WHERE group_id=?",
                             (str(group_id),)).fetchone()
        finally:
            conn.close()
        return dict(r) if r else None

    def set_then(self, group_id: str, then: str) -> None:
        conn = self._conn()
        try:
            conn.execute("UPDATE card_groups SET then_text=?, updated_at=? WHERE group_id=?",
                         (str(then or "")[:400], time.time(), str(group_id)))
            conn.commit()
        finally:
            conn.close()

    def transition(self, group_id: str, expect: frozenset, new_state: str) -> bool:
        """Move a group from one of ``expect`` to ``new_state`` — once."""
        now = time.time()
        marks = ", settled_at=?" if new_state == G_SETTLED else ""
        qs = ",".join("?" for _ in expect)
        params: list = [new_state, now] + ([now] if marks else []) + [str(group_id)] + list(expect)
        conn = self._conn()
        try:
            cur = conn.execute(f"UPDATE card_groups SET state=?, updated_at=?{marks} "
                               f"WHERE group_id=? AND state IN ({qs})", params)
            conn.commit()
            return (cur.rowcount or 0) == 1
        finally:
            conn.close()

    def decided_for(self, *, session_id: str = "", origin: str = "", origin_ref: str = "",
                    since: float = 0.0) -> List[Dict[str, Any]]:
        """SETTLED groups (decided, not yet shown to the agent) of one session
        (or one goal / cron job), settled after ``since``, oldest first. A
        woken group is excluded: its wake turn carries the outcomes."""
        where, args = ["state=?", "COALESCE(settled_at, 0) > ?"], [G_SETTLED, float(since)]
        if session_id:
            where.append("session_id=?")
            args.append(str(session_id))
        if origin:
            where += ["origin=?", "origin_ref=?"]
            args += [origin, str(origin_ref or "")]
        conn = self._conn()
        try:
            rows = conn.execute("SELECT * FROM card_groups WHERE " + " AND ".join(where)
                                + " ORDER BY settled_at", args).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def stale_woken(self, session_id: str, before: float) -> List[Dict[str, Any]]:
        """Groups ``woken`` before ``before`` whose run never closed them."""
        conn = self._conn()
        try:
            rows = conn.execute("SELECT * FROM card_groups WHERE session_id=? AND state=? "
                                "AND updated_at < ? ORDER BY settled_at",
                                (str(session_id), G_WOKEN, float(before))).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def open_count(self, session_id: str) -> int:
        conn = self._conn()
        try:
            # Only a group that holds a card: a refused propose_action leaves an
            # empty open group (reused by the next proposal), which waits on nothing.
            r = conn.execute("SELECT COUNT(*) AS n FROM card_groups g WHERE g.session_id=? "
                             "AND g.state=? AND EXISTS (SELECT 1 FROM cards c WHERE "
                             "c.user_id=g.user_id AND json_extract(c.doc, '$.group_id')"
                             "=g.group_id)", (str(session_id), G_OPEN)).fetchone()
        finally:
            conn.close()
        return int(r["n"]) if r else 0


_GROUPS: Dict[str, GroupStore] = {}


def groups(cards_store: Any) -> GroupStore:
    """The group table inside ``cards_store``'s ``cards.db``. The caller passes
    the store (``cards`` sits in the big core import cycle; this module reaches
    it only through :func:`install`, never the other way)."""
    path = cards_store.db_path
    gs = _GROUPS.get(path)
    if gs is None:
        gs = _GROUPS[path] = GroupStore(path)
    return gs


def wake_queue_path(cards_store: Any) -> str:
    """``wakes.db`` in the same data home as ``cards.db`` — the file the
    owning process's wake drain reads (``polyrob.app.autonomy_runtime``)."""
    from core.runtime_paths import store_db_path
    path = cards_store.db_path
    return store_db_path("wakes.db", os.path.dirname(os.path.abspath(path)))


#: The card states (``core.surfaces.cards``) a group reads, by value.
_DECIDED_STATES = ("done", "failed", "cancelled", "expired")
_REPLACED = "replaced"


# ── settlement ───────────────────────────────────────────────────────────────

def _decided(card: Any, child_of: Dict[str, Any]) -> bool:
    if card.state in _DECIDED_STATES:
        return True
    if card.state == _REPLACED:
        # Superseded by a child (its quote) → the child decides. A replaced
        # card with no child is decided only once its line's reply is stored
        # (``cards.note_result``) — not in the instant between the tap and the
        # quote card it is about to make.
        return card.card_id in child_of or bool((card.result or "").strip())
    return False


def leaves(group_cards: List[Any]) -> List[Any]:
    """The cards whose outcome counts: every card no other card replaced."""
    parents = {c.parent_id for c in group_cards if c.parent_id}
    return [c for c in group_cards
            if not (c.state == _REPLACED and c.card_id in parents)]


def is_settled(group_cards: List[Any]) -> bool:
    if not group_cards:
        return False
    child_of = {c.parent_id: c for c in group_cards if c.parent_id}
    return all(_decided(c, child_of) for c in group_cards)


def on_transition(card: Any, st: Any) -> bool:
    """``cards._notify`` calls this after every state change of a grouped card.
    Settles the group once and writes its one wake row. True iff this call
    settled it. Fail-open is the caller's (it logs)."""
    gid = getattr(card, "group_id", None)
    if not gid:
        return False
    gs = groups(st)
    g = gs.get(gid)
    if g is None or g["state"] != G_OPEN:
        return False
    return settle(gid, card.user_id, st, g=g)


def settle(gid: str, user_id: str, st: Any, *, g: Optional[Dict[str, Any]] = None) -> bool:
    """Settle an open group whose every card is decided, and write its one
    wake row. A row that cannot be written re-opens the group, so
    :func:`heal` (the timed sweep) tries again."""
    gs = groups(st)
    g = g or gs.get(gid)
    if g is None or g["state"] != G_OPEN:
        return False
    members = st.group_cards(user_id, gid)
    if not is_settled(members):
        return False
    if not gs.transition(gid, frozenset({G_OPEN}), G_SETTLED):
        return False      # another process settled it first
    counts = _counts(leaves(members))
    logger.info("card group %s settled: session=%s origin=%s %s", gid,
                g["session_id"], g["origin"], counts)
    try:
        from core.event_kinds import CARD_GROUP_SETTLED
        from core.event_log import emit
        emit(CARD_GROUP_SETTLED, source="cards", user_id=g["user_id"],
             session_id=g["session_id"], attrs=dict(counts, group_id=gid, origin=g["origin"]))
    except Exception:
        logger.debug("card group %s: settle event not recorded", gid, exc_info=True)
    rowid = None
    try:
        from core.wake_queue import get_wake_queue
        rowid = get_wake_queue(wake_queue_path(st)).enqueue(
            g["session_id"], g["user_id"],
            render_outcomes(g["user_id"], gid, st=st) or "cards decided", kind=WAKE_KIND,
            metadata={"kind": WAKE_KIND, "source": WAKE_KIND, "group_id": gid,
                      "session_id": g["session_id"], "origin": g["origin"],
                      "origin_ref": g["origin_ref"]})
    except Exception:
        logger.warning("card group %s: wake row raised", gid, exc_info=True)
    if rowid is None:
        logger.warning("card group %s settled but its wake row was not written — "
                       "re-opened for the sweep", gid)
        gs.transition(gid, frozenset({G_SETTLED}), G_OPEN)
        return False
    return True


#: How far back :func:`heal` looks for an open group to settle.
HEAL_WINDOW_S = 3 * 24 * 3600


def heal(st: Any, *, now: Optional[float] = None) -> int:
    """The timed sweep's second half: settle every recent open group whose
    cards are all decided but which no transition settled (its wake row could
    not be written). Returns how many it settled."""
    now = time.time() if now is None else now
    gs = groups(st)
    conn = gs._conn()
    try:
        rows = conn.execute("SELECT group_id, user_id FROM card_groups WHERE state=? AND "
                            "updated_at > ?", (G_OPEN, now - HEAL_WINDOW_S)).fetchall()
    finally:
        conn.close()
    return sum(1 for r in rows if settle(r["group_id"], r["user_id"], st))


def rejoin(group_id: str, st: Any) -> None:
    """A card was just added to ``group_id``: if the group settled between
    :meth:`GroupStore.open_group_for` and the insert, re-open it so the new
    card is waited for (its pending wake row then finds it open and drops)."""
    gs = groups(st)
    g = gs.get(group_id)
    if g is not None and g["state"] == G_SETTLED:
        gs.transition(group_id, frozenset({G_SETTLED}), G_OPEN)


def _counts(cards_list: List[Any]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for c in cards_list:
        out[c.state] = out.get(c.state, 0) + 1
    return out


_STATE_WORD = {"done": "DONE", "failed": "FAILED", "cancelled": "DISMISSED",
               "expired": "EXPIRED", "replaced": "NO QUOTE"}


def _first_line(text: str) -> str:
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if ln:
            return ln if len(ln) <= _LINE_RESULT_CHARS else ln[:_LINE_RESULT_CHARS - 1] + "…"
    return ""


def render_outcomes(user_id: str, group_id: str, *, st: Any) -> str:
    """One line per decided card of the group, the agent's own follow-up, and
    a pointer to commands the owner typed outside the cards. Pure read."""
    g = groups(st).get(group_id)
    if g is None:
        return ""
    lines = []
    for c in leaves(st.group_cards(user_id, group_id)):
        word = _STATE_WORD.get(c.state, c.state.upper())
        res = _first_line(c.result)
        lines.append(f"- {c.line} → {word}" + (f": {res}" if res else "")
                     + f" (card {c.card_id})")
    out = [f"The owner decided all {len(lines)} card(s) you raised:", *lines]
    if (g.get("then_text") or "").strip():
        out.append(f"Your follow-up, in your own words when you raised them: {g['then_text']}")
    out.append("Commands the owner typed by hand meanwhile are in the owner thread, "
               "labelled 'owner typed a command'.")
    text = "\n".join(out)
    return text if len(text) <= OUTCOMES_MAX_CHARS else text[:OUTCOMES_MAX_CHARS - 1] + "…"


#: A group still ``woken`` this long after its wake ran no run to its end
#: (the process stopped): the next turn shows it again.
WOKEN_STALE_S = 30 * 60


def close_woken(group_ids: List[str], ok: bool, *, st: Any) -> None:
    """The woken run ended: ``consumed`` if it ran well, else ``settled`` again
    so the session's next turn shows the outcomes."""
    gs = groups(st)
    for gid in group_ids:
        gs.transition(gid, frozenset({G_WOKEN}), G_CONSUMED if ok else G_SETTLED)


def take_outcomes(*, session_id: str, user_id: str, origin: str = "", origin_ref: str = "",
                  since: float = 0.0, st: Any) -> str:
    """The ``<card-outcomes>`` text for the start of a turn: every group of this
    session (or this goal / cron job) decided since ``since``. Each group shown
    is marked ``consumed`` so its pending wake is dropped — the turn already
    knows. Empty string when there is nothing new. Never raises."""
    try:
        if st is None:
            return ""      # no cards.db yet: nothing raised, and a read creates no file
        gs = groups(st)
        found = gs.decided_for(session_id="" if origin else session_id, origin=origin,
                               origin_ref=origin_ref, since=since)
        if session_id and not origin:
            found += gs.stale_woken(session_id, time.time() - WOKEN_STALE_S)
        parts = []
        for g in found:
            if not gs.transition(g["group_id"], frozenset({G_SETTLED, G_WOKEN}), G_CONSUMED):
                continue
            body = render_outcomes(user_id, g["group_id"], st=st)
            if body:
                parts.append(body)
        waiting = gs.open_count(session_id) if session_id and not origin else 0
        if waiting:
            parts.append(f"Still waiting on the owner: {waiting} group(s) of cards (/cards).")
        if not parts:
            return ""
        return "\n".join(["<card-outcomes>",
                          "The owner's decisions on the cards you raised. Context only.",
                          *parts, "</card-outcomes>"])
    except Exception:
        logger.debug("card outcomes read failed (fail-open)", exc_info=True)
        return ""


def install() -> None:
    """Hook :func:`on_transition` into every card state change (idempotent)."""
    from core.surfaces import cards
    cards.set_group_hook(on_transition)


install()


__all__ = [
    "G_CONSUMED", "G_OPEN", "G_SETTLED", "G_WOKEN", "GroupStore", "ORIGIN_CHAT",
    "ORIGIN_CRON", "ORIGIN_GOAL", "WAKE_KIND", "groups", "is_settled", "leaves",
    "on_transition", "render_outcomes", "take_outcomes", "wake_queue_path",
]
