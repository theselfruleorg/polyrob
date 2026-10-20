"""Action cards: one decision the owner can take with a tap, on every seat.

Why (crypto-UX review, 2026-09-27): a money quote ended with a line to type
again — ``To send it: /send 0.5 native to 0x… on base go`` — because a button
may only carry ONE token (``core.surfaces.actions``). On a phone the owner
copied the line or re-typed a 42-character address, and a second typo could
differ from the address the quote showed. Decision buttons also stayed live
after a decision, and the agent had no way to offer a choice at all.

A card is a stored decision with an opaque id. Its buttons are one-token tap
commands that name the card and an act — ``/card_<id>_ok`` — so the one-token
rule holds and the payload never carries an amount or an address. What a tap
DOES is decided from the stored row, never from the button.

Four kinds:

* ``quote``    — a money verb's quote, built by the SEAT from the verb's own
                 dry run. The row stores the exact confirm line the quote
                 printed (``/send 0.5 native to 0x… on base max 12.34 go``).
                 Confirm re-runs THAT line through the seat's own dispatch —
                 the same gates as the owner typing it — once only.
* ``proposal`` — the AGENT names a money verb and its arguments. The card has
                 no confirm: its one action runs the QUOTE, which answers with
                 a system ``quote`` card. The model never writes a confirm
                 button or a money field.
* ``choice``   — the agent asks the owner to pick one of 2–6 options. The pick
                 is stored; the waiting tool reads it back into the live turn.
* ``builder``  — a bare ``/send`` or ``/swap``: chain, token, recipient (or the
                 token to buy) and amount, one row of buttons per step
                 (``surfaces/card_builder.py``); the last tap runs the QUOTE.

⚠️ Invariants (each pinned by ``tests/unit/core/surfaces/test_cards.py``):

* a card belongs to ONE owner; another presser gets "no such card";
* every transition is a compare-and-swap on the row — a second tap on a used
  card is refused, never re-run;
* a card never parses free text into a command (the no-plain-word rule): the
  confirm line is the code-produced line the verb itself printed, re-checked
  against the verb and the typed arguments before it is stored;
* the text of a card keeps the typed form of every action, so a seat that
  cannot render buttons loses nothing.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import math
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)

KIND_QUOTE = "quote"
KIND_PROPOSAL = "proposal"
KIND_CHOICE = "choice"
#: A /send or /swap built from buttons, one field per step (surfaces/card_builder.py).
#: A pick advances the SAME card; the last pick runs the verb's QUOTE.
KIND_BUILDER = "builder"
#: 085 W3 (D3): standing MONEY work the agent wrote on an owner turn — a goal
#: or a job carrying a money tool, a money rig or a buy target. The card holds
#: the owner seat's own line (``/goal new …``, ``/job new …``); Confirm runs it
#: on the seat, which writes the row with the owner's grant.
KIND_JOB = "job"

ORIGIN_SYSTEM = "system"
ORIGIN_AGENT = "agent"

S_OPEN = "open"
S_CONFIRMED = "confirmed"   # the confirm line is running
S_DONE = "done"
S_FAILED = "failed"
S_CANCELLED = "cancelled"
S_EXPIRED = "expired"
S_REPLACED = "replaced"     # a refresh (or a proposal's quote) superseded it

#: How long a quote card may be confirmed. The send re-simulates and is bound
#: by the caps either way; past this the owner refreshes the price first.
QUOTE_TTL_S = 15 * 60
#: A confirmed card with no result after this long is closed as not confirmed.
STUCK_AFTER_S = 30 * 60
#: AC5: a decided card (and its message refs) is kept this long, then pruned.
#: Long enough for ``recent_recipients`` and a late result (``finish``).
DECIDED_RETENTION_S = 90 * 24 * 3600
#: How often (at most) one process prunes its store.
_PRUNE_EVERY_S = 3600
PROPOSAL_TTL_S = 24 * 3600
CHOICE_TTL_S = 24 * 3600

MAX_OPTIONS = 6
MAX_OPTION_CHARS = 80
MAX_TITLE_CHARS = 200
MAX_BODY_CHARS = 3000

#: The verbs a quote or a proposal card may carry: every owner money verb that
#: quotes first and acts on a trailing ``go``. A verb outside this set is never
#: carded, so a card can never become a way to run a verb that has no quote.
CONFIRMABLE_VERBS = frozenset({
    "/send", "/swap", "/bridge", "/pay", "/claim", "/launch", "/deploy",
    "/nft", "/identity", "/writeoff", "/unquarantine", "/wallet",
    "/restart", "/update",  # 077: restart / update the agent, quote -> owner confirm
})

#: Confirmable verbs whose BARE form already quotes (``/restart`` → "To restart:
#: /restart go"). Every other verb needs at least one typed word before its
#: quote is carded — a bare money verb prints usage, never a quote.
BARE_CONFIRMABLE_VERBS = frozenset({"/restart", "/update"})

#: A card tap token: ``/card_<10 hex>_<act>``. Acts: ``ok`` (confirm), ``no``
#: (cancel), ``re`` (refresh / quote), ``1``–``6`` (pick an option).
_ID_HEX = 10
_CARD_TOKEN_RE = re.compile(r"^/card_([0-9a-f]{10})_(ok|no|re|[1-6])$")
ACTS = ("ok", "no", "re") + tuple(str(i) for i in range(1, MAX_OPTIONS + 1))

#: One argument of a carded command line: no whitespace, no control bytes, no
#: backtick or angle bracket (a usage placeholder like ``<max_usd>`` is not an
#: argument), bounded.
_ARG_RE = re.compile(r"^[^\s`<>\x00-\x1f]{1,256}$")
#: The ONE set of words that turn a quoting money verb into an executing one.
GO_WORDS = ("go", "execute", "confirm")


def is_go_word(word: str) -> bool:
    """True when *word* is one of :data:`GO_WORDS` (any case)."""
    return str(word or "").lower() in GO_WORDS


#: Verbs with subcommands: the only ones a PROPOSAL may name. ``/wallet
#: autonomous <usd>`` acts without a `go` (it raises the agent's own no-ask
#: ceiling), so it is never proposable; neither is anything else that acts
#: without its quote.
_PROPOSAL_SUBCOMMANDS = {
    "/wallet": frozenset({"trust", "untrust"}),
    "/nft": frozenset({"transfer", "revoke", "send"}),
    "/identity": frozenset({"register", "set-uri"}),
}


#: 085 W3: the seat lines a JOB card may carry — the owner verbs that WRITE
#: standing work. ``/job edit`` only as ``/job edit <id> grant …``.
JOB_CARD_SUBCOMMANDS = {
    "/goal": frozenset({"new", "grant"}),
    "/job": frozenset({"new", "edit"}),
}
#: The most words one job card line holds (a task is prose).
MAX_JOB_WORDS = 400


def executes(args: List[str]) -> bool:
    """True when these words would EXECUTE rather than quote: a `go` word
    ANYWHERE (``/pay`` strips ``id=`` first; ``/writeoff`` reads `go` as its
    third word, before a free-text reason)."""
    return any(is_go_word(a) for a in (args or []))


def card_token(card_id: str, act: str) -> str:
    return f"/card_{card_id}_{act}"


def parse_card_token(text: str) -> Tuple[Optional[str], Optional[str]]:
    """``("<card id>", "<act>")`` for ``/card_<id>_<act>``, else ``(None, None)``."""
    token = (text or "").strip().split()[0] if (text or "").strip() else ""
    m = _CARD_TOKEN_RE.match(token.lower())
    if not m:
        return None, None
    return m.group(1), m.group(2)


def new_card_id() -> str:
    return secrets.token_hex(_ID_HEX // 2)


@dataclass
class Card:
    card_id: str
    user_id: str
    kind: str
    origin: str
    title: str
    body: str = ""
    verb: str = ""                      # "/send" for quote + proposal cards
    args: List[str] = field(default_factory=list)   # the confirm line, WITHOUT go
    options: List[str] = field(default_factory=list)
    state: str = S_OPEN
    answer: Optional[str] = None        # the picked option (choice)
    result: str = ""                    # what happened, for the in-place edit
    session_id: Optional[str] = None
    #: A builder's fields so far and the hidden value of each option shown
    #: (``draft["_values"]``); the button carries only the option's number.
    draft: dict = field(default_factory=dict)
    #: CA1: the words the OWNER typed (a quote card), apart from the bound the
    #: seat added to the confirm line. A refresh re-runs THESE, so a new quote
    #: computes a new bound instead of inheriting the stale one.
    typed: List[str] = field(default_factory=list)
    #: 084: the cards one session raised form a GROUP; when every card of the
    #: group is decided, the session is woken once (``core.surfaces.card_groups``).
    #: A quote, extra-check or recovery card inherits the group of the card it
    #: came from (``parent_id``) through :func:`lineage`.
    group_id: Optional[str] = None
    parent_id: Optional[str] = None
    created_at: float = 0.0
    expires_at: float = 0.0
    updated_at: float = 0.0

    @property
    def line(self) -> str:
        """The typed command this card carries (quote/proposal)."""
        return " ".join([self.verb] + list(self.args)).strip()

    @property
    def confirm_line(self) -> str:
        return f"{self.line} go"

    @property
    def refresh_line(self) -> str:
        """The line a Refresh (or a proposal's quote) runs: the typed words
        only — never a bound the seat added to the confirm line (CA1)."""
        if self.typed:
            return " ".join([self.verb] + list(self.typed)).strip()
        return self.line

    def expired(self, now: Optional[float] = None) -> bool:
        return self.state == S_OPEN and (now or time.time()) >= self.expires_at


def card_actions(card: Card) -> list:
    """The buttons this card shows in its CURRENT state (none once decided)."""
    from core.surfaces.envelopes import Action
    if card.state != S_OPEN:
        return []
    cid = card.card_id
    if card.kind == KIND_QUOTE:
        return [Action("Confirm", card_token(cid, "ok"), "primary"),
                Action("Refresh", card_token(cid, "re")),
                Action("Cancel", card_token(cid, "no"), "danger")]
    if card.kind == KIND_JOB:
        return [Action("Confirm", card_token(cid, "ok"), "primary"),
                Action("Dismiss", card_token(cid, "no"), "danger")]
    if card.kind == KIND_PROPOSAL:
        if card.origin == ORIGIN_SYSTEM and is_ack_step(card.args):
            return [Action("I understand — continue", card_token(cid, "re"), "primary"),
                    Action("Cancel", card_token(cid, "no"), "danger")]
        return [Action("Get a quote", card_token(cid, "re"), "primary"),
                Action("Dismiss", card_token(cid, "no"), "danger")]
    out = [Action(_short(opt, 40), card_token(cid, str(i)))
           for i, opt in enumerate(card.options[:MAX_OPTIONS], start=1)]
    if card.kind == KIND_BUILDER:
        out.append(Action("Cancel", card_token(cid, "no"), "danger"))
    return out


def _short(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


_STATE_WORDS = {
    S_CONFIRMED: "⏳ Confirmed — running now.",
    S_DONE: "✅ Done.",
    S_FAILED: "⚠️ Not confirmed as done — read the result below.",
    S_CANCELLED: "✖️ Cancelled. Nothing was done.",
    S_EXPIRED: "⌛ Expired. Nothing was done.",
    S_REPLACED: "↻ Replaced by a newer card.",
}


def render_text(card: Card) -> str:
    """The card as text: every seat's fallback, and the body of every button
    message. Each action's typed token is in the text, so nothing depends on a
    button rendering."""
    lines: List[str] = []
    head = card.title
    if card.origin == ORIGIN_AGENT:
        head = f"🤖 From the agent — {head}"
    lines.append(head)
    if card.body:
        lines.append("")
        lines.append(card.body.rstrip())
    if card.kind in (KIND_CHOICE, KIND_BUILDER) and card.options:
        lines.append("")
        for i, opt in enumerate(card.options, start=1):
            lines.append(f"  {i}. {opt}")
    if card.kind in (KIND_QUOTE, KIND_PROPOSAL, KIND_JOB) and card.line:
        lines.append("")
        lines.append(f"Command: {card.confirm_line if card.kind == KIND_QUOTE else card.line}")
    lines.append("")
    if card.state == S_OPEN:
        cid = card.card_id
        if card.kind == KIND_QUOTE:
            mins = max(1, int((card.expires_at - time.time()) // 60))
            lines.append(f"Confirm: {card_token(cid, 'ok')}   Refresh: {card_token(cid, 're')}"
                         f"   Cancel: {card_token(cid, 'no')}")
            lines.append(f"(the quote can be confirmed for {mins} min, once)")
        elif card.kind == KIND_JOB:
            lines.append(f"Confirm: {card_token(cid, 'ok')}   Dismiss: {card_token(cid, 'no')}")
            lines.append("(Confirm runs the command above as if you typed it — nothing is "
                         "written before)")
        elif card.kind == KIND_PROPOSAL and card.origin == ORIGIN_SYSTEM and is_ack_step(card.args):
            lines.append(f"I understand: {card_token(cid, 're')}   Cancel: {card_token(cid, 'no')}")
            lines.append("(nothing moves from this card — the next quote has the Confirm)")
        elif card.kind == KIND_PROPOSAL:
            lines.append(f"Quote it: {card_token(cid, 're')}   Dismiss: {card_token(cid, 'no')}")
            lines.append("(nothing moves from this card — the quote comes first)")
        else:
            picks = "  ".join(card_token(cid, str(i)) for i in range(1, len(card.options) + 1))
            if picks:
                lines.append("Pick: " + picks)
            if card.kind == KIND_BUILDER:
                lines.append(f"Cancel: {card_token(cid, 'no')}")
    else:
        lines.append(_STATE_WORDS.get(card.state, card.state))
        if card.answer:
            lines.append(f"Picked: {card.answer}")
        if card.result:
            lines.append(_short_block(card.result, 700))
    return "\n".join(lines)


def _short_block(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


# ── the store ────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    card_id    TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    kind       TEXT NOT NULL,
    origin     TEXT NOT NULL,
    state      TEXT NOT NULL,
    doc        TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS cards_user_state ON cards(user_id, state);
CREATE TABLE IF NOT EXISTS card_refs (
    card_id    TEXT NOT NULL,
    surface    TEXT NOT NULL,
    chat_id    TEXT NOT NULL,
    message_id TEXT NOT NULL,
    PRIMARY KEY (card_id, surface, chat_id, message_id)
);
"""

_DOC_KEYS = ("title", "body", "verb", "args", "options", "answer", "result", "session_id",
             "draft", "typed", "group_id", "parent_id")


class CardStore:
    """``cards.db`` — one row per card. Every state change is a CAS."""

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            from core.runtime_paths import data_home_db_path
            db_path = data_home_db_path("cards.db")
        self.db_path = str(db_path)
        from core.sqlite_util import init_schema
        init_schema(self.db_path, _SCHEMA, mkdir=True)

    def _conn(self):
        from core.sqlite_util import wal_connect
        return wal_connect(self.db_path)

    @staticmethod
    def _row(r) -> Card:
        doc = json.loads(r["doc"] or "{}")
        return Card(card_id=r["card_id"], user_id=r["user_id"], kind=r["kind"],
                    origin=r["origin"], state=r["state"],
                    title=doc.get("title", ""), body=doc.get("body", ""),
                    verb=doc.get("verb", ""), args=list(doc.get("args") or []),
                    options=list(doc.get("options") or []),
                    answer=doc.get("answer"), result=doc.get("result", ""),
                    session_id=doc.get("session_id"), draft=dict(doc.get("draft") or {}),
                    typed=list(doc.get("typed") or []),
                    group_id=doc.get("group_id"), parent_id=doc.get("parent_id"),
                    created_at=r["created_at"], expires_at=r["expires_at"],
                    updated_at=r["updated_at"])

    @staticmethod
    def _doc(card: Card) -> str:
        return json.dumps({k: getattr(card, k) for k in _DOC_KEYS})

    def insert(self, card: Card) -> Card:
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO cards (card_id, user_id, kind, origin, state, doc, "
                "created_at, expires_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (card.card_id, card.user_id, card.kind, card.origin, card.state,
                 self._doc(card), card.created_at, card.expires_at, card.updated_at))
            conn.commit()
        finally:
            conn.close()
        self.maybe_prune()
        return card

    def get(self, card_id: str) -> Optional[Card]:
        conn = self._conn()
        try:
            r = conn.execute("SELECT * FROM cards WHERE card_id=?", (card_id,)).fetchone()
        finally:
            conn.close()
        return self._row(r) if r else None

    def transition(self, card_id: str, expect: frozenset, new_state: str,
                   expect_updated_at: Optional[float] = None,
                   **updates: Any) -> Optional[Card]:
        """Move ``card_id`` from one of ``expect`` to ``new_state`` — atomically,
        or not at all. Returns the updated card, or None when the row was not in
        an expected state (someone else decided it first)."""
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            r = conn.execute("SELECT * FROM cards WHERE card_id=?", (card_id,)).fetchone()
            if r is None or r["state"] not in expect or (
                    expect_updated_at is not None and r["updated_at"] != expect_updated_at):
                conn.rollback()
                return None
            card = self._row(r)
            for k, v in updates.items():
                setattr(card, k, v)
            card.state = new_state
            card.updated_at = time.time()
            conn.execute("UPDATE cards SET state=?, doc=?, updated_at=? WHERE card_id=? "
                         "AND state=?", (new_state, self._doc(card), card.updated_at,
                                         card_id, r["state"]))
            conn.commit()
            return card
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def open_cards(self, user_id: str, limit: int = 20) -> List[Card]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM cards WHERE user_id=? AND state=? ORDER BY created_at DESC "
                "LIMIT ?", (user_id, S_OPEN, int(limit))).fetchall()
        finally:
            conn.close()
        now = time.time()
        return [c for c in (self._row(r) for r in rows) if not c.expired(now)]

    def sweep_stuck(self, user_id: str, older_than_s: float = STUCK_AFTER_S) -> List[Card]:
        """A card left ``confirmed`` (a crash, a restart, a run that reported
        nothing) is closed as NOT confirmed, never left "running" forever."""
        conn = self._conn()
        try:
            rows = conn.execute("SELECT card_id FROM cards WHERE user_id=? AND state=? "
                                "AND updated_at < ?", (str(user_id), S_CONFIRMED,
                                                       time.time() - older_than_s)).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            c = self.transition(r["card_id"], frozenset({S_CONFIRMED}), S_FAILED,
                                result=_NO_RESULT)
            if c is not None:
                out.append(c)
        return out

    def sweep_expired(self, *, now: Optional[float] = None) -> List[Card]:
        """084 WS2: every tenant's OPEN card past its expiry → ``expired``, and
        every card stuck ``confirmed`` → failed (``sweep_stuck``). Expiry used to
        happen only on a tap or ``/cards``, so a group the owner walked away
        from never settled. The caller notifies (the settle hook runs there)."""
        now = time.time() if now is None else now
        conn = self._conn()
        try:
            rows = conn.execute("SELECT card_id FROM cards WHERE state=? AND expires_at < ?",
                                (S_OPEN, now)).fetchall()
            users = conn.execute("SELECT DISTINCT user_id FROM cards WHERE state=? "
                                 "AND updated_at < ?",
                                 (S_CONFIRMED, now - STUCK_AFTER_S)).fetchall()
        finally:
            conn.close()
        out = [c for c in (self.transition(r["card_id"], frozenset({S_OPEN}), S_EXPIRED)
                           for r in rows) if c is not None]
        for u in users:
            out.extend(self.sweep_stuck(u["user_id"]))
        return out

    def group_cards(self, user_id: str, group_id: str) -> List[Card]:
        """Every card of one 084 group, oldest first."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM cards WHERE user_id=? AND json_extract(doc, '$.group_id')=? "
                "ORDER BY created_at, card_id", (str(user_id), str(group_id))).fetchall()
        finally:
            conn.close()
        return [self._row(r) for r in rows]

    def prune(self, *, now: Optional[float] = None,
              retention_s: float = DECIDED_RETENTION_S) -> int:
        """AC5: delete decided cards (and expired open ones) older than the
        retention, and every message ref whose card is gone. A ``confirmed``
        card is never pruned here — ``sweep_stuck`` closes it first. Returns
        the number of cards removed."""
        cutoff = (now if now is not None else time.time()) - retention_s
        conn = self._conn()
        try:
            cur = conn.execute(
                "DELETE FROM cards WHERE (state NOT IN (?, ?) AND updated_at < ?) "
                "OR (state = ? AND expires_at < ?)",
                (S_OPEN, S_CONFIRMED, cutoff, S_OPEN, cutoff))
            removed = cur.rowcount or 0
            conn.execute("DELETE FROM card_refs WHERE card_id NOT IN "
                         "(SELECT card_id FROM cards)")
            conn.commit()
            return removed
        finally:
            conn.close()

    def maybe_prune(self) -> None:
        """Prune at most once per :data:`_PRUNE_EVERY_S` per process. Fail-open:
        a prune fault costs disk, never the caller's card."""
        now = time.time()
        if now - getattr(self, "_last_prune", 0.0) < _PRUNE_EVERY_S:
            return
        self._last_prune = now
        try:
            self.prune(now=now)
        except Exception:
            logger.warning("card store prune failed (rows kept)", exc_info=True)

    def add_ref(self, card_id: str, surface: str, chat_id: Any, message_id: Any) -> None:
        if message_id is None or chat_id is None:
            return
        conn = self._conn()
        try:
            conn.execute("INSERT OR IGNORE INTO card_refs VALUES (?,?,?,?)",
                         (card_id, surface, str(chat_id), str(message_id)))
            conn.commit()
        finally:
            conn.close()

    def refs(self, card_id: str) -> List[Tuple[str, str, str]]:
        conn = self._conn()
        try:
            rows = conn.execute("SELECT surface, chat_id, message_id FROM card_refs "
                                "WHERE card_id=?", (card_id,)).fetchall()
        finally:
            conn.close()
        return [(r["surface"], r["chat_id"], r["message_id"]) for r in rows]

    def recent_recipients(self, user_id: str, verb: str = "/send",
                          limit: int = 200) -> List[str]:
        """The ``to`` addresses of this owner's COMPLETED cards for ``verb``."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT doc FROM cards WHERE user_id=? AND kind=? AND state=? "
                "ORDER BY updated_at DESC LIMIT ?",
                (user_id, KIND_QUOTE, S_DONE, int(limit))).fetchall()
        finally:
            conn.close()
        out: List[str] = []
        for r in rows:
            doc = json.loads(r["doc"] or "{}")
            if doc.get("verb") != verb:
                continue
            res = str(doc.get("result") or "")
            if not any(m in res for m in _SENT_MARKS) or failed_result(res):
                continue   # only a send the rail itself confirmed
            args = [str(a) for a in doc.get("args") or []]
            for i, a in enumerate(args[:-1]):
                if a.lower() == "to" and args[i + 1] not in out:
                    out.append(args[i + 1])
        return out


_STORE: Optional[CardStore] = None


def store() -> CardStore:
    """The process card store (the data home's ``cards.db``)."""
    global _STORE
    if _STORE is None:
        _STORE = CardStore()
    return _STORE


def existing_store() -> Optional[CardStore]:
    """The process card store when its file exists, else None — a READ never
    mints an empty ``cards.db`` (084: every turn's first step reads groups)."""
    if _STORE is not None:
        return _STORE
    import os
    from core.runtime_paths import data_home_db_path
    return store() if os.path.exists(data_home_db_path("cards.db")) else None


def set_store(s: Optional[CardStore]) -> None:
    """Test seam: point the process at another store (None = the default)."""
    global _STORE
    _STORE = s


# ── listeners: a seat that can edit a card in place registers here ──────────

_LISTENERS: List[Callable[[Card], Any]] = []


#: 084: ``core.surfaces.card_groups.on_transition``, installed when that module
#: is imported (every seat that presses or raises cards imports it). A hook,
#: not an import: cards sits in the big core cycle and card_groups must not.
_GROUP_HOOK: Optional[Callable[[Card, "CardStore"], Any]] = None


def set_group_hook(fn: Optional[Callable[[Card, "CardStore"], Any]]) -> None:
    global _GROUP_HOOK
    _GROUP_HOOK = fn


def add_listener(fn: Callable[[Card], Any]) -> None:
    """``fn(card)`` runs after every state change (sync or async). A Telegram
    harness edits its messages; the console pushes an event. Fail-open."""
    if fn not in _LISTENERS:
        _LISTENERS.append(fn)


def remove_listener(fn: Callable[[Card], Any]) -> None:
    if fn in _LISTENERS:
        _LISTENERS.remove(fn)


def _notify(card: Card) -> None:
    import asyncio
    import inspect
    for fn in list(_LISTENERS):
        try:
            res = fn(card)
            if inspect.isawaitable(res):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None:
                    from core.async_bridge import spawn_retained
                    spawn_retained(res, _PENDING_EDITS)
                else:
                    res.close()
        except Exception:
            logger.debug("card listener failed (fail-open)", exc_info=True)
    if card.group_id and _GROUP_HOOK is not None:
        # 084: every state change passes here, so this is the ONE place a
        # group can settle (and wake the session that raised it).
        try:
            _GROUP_HOOK(card, store())
        except Exception:
            logger.warning("card group settle check failed (fail-open)", exc_info=True)


_PENDING_EDITS: set = set()


# ── building cards ───────────────────────────────────────────────────────────

def _valid_args(args: List[str]) -> bool:
    return all(isinstance(a, str) and _ARG_RE.match(a) for a in args)


def _new(user_id: str, kind: str, origin: str, title: str, *, body: str = "",
         verb: str = "", args: Optional[List[str]] = None,
         options: Optional[List[str]] = None, session_id: Optional[str] = None,
         typed: Optional[List[str]] = None, group_id: Optional[str] = None,
         ttl: float, st: Optional[CardStore] = None) -> Card:
    now = time.time()
    parent = _PARENT.get()
    parent_id = None
    if parent is not None and str(parent.user_id) == str(user_id):
        # 084: a card made while a seat runs another card's line (its quote,
        # its extra check, its recovery) belongs to that card's session + group.
        session_id = session_id or parent.session_id
        group_id = group_id or parent.group_id
        parent_id = parent.card_id
    card = Card(card_id=new_card_id(), user_id=str(user_id), kind=kind, origin=origin,
                title=_short(title, MAX_TITLE_CHARS), body=(body or "")[:MAX_BODY_CHARS],
                verb=verb, args=list(args or []), options=list(options or []),
                session_id=session_id, typed=list(typed or []), group_id=group_id,
                parent_id=parent_id, created_at=now, expires_at=now + ttl, updated_at=now)
    return (st or store()).insert(card)


#: 084: the card whose line a seat is running right now (a contextvar: it
#: crosses ``await`` and is copied into the background task of a confirm).
_PARENT: "contextvars.ContextVar[Optional[Card]]" = contextvars.ContextVar(
    "card_parent", default=None)


@contextlib.contextmanager
def lineage(parent: Optional[Card]):
    """Run a card's line so that any card it makes inherits ``parent``'s
    session and group (084 WS1). A no-op for ``None``."""
    token = _PARENT.set(parent) if parent is not None else None
    try:
        yield
    finally:
        if token is not None:
            _PARENT.reset(token)


def note_result(card_id: str, text: str, *, st: Optional[CardStore] = None) -> Optional[Card]:
    """Store what a REPLACED card's line produced when it made no new card (a
    refused quote, an error), so its group can settle and report it (084)."""
    st = st or store()
    cur = st.get(card_id)
    if cur is None or cur.state != S_REPLACED:
        return None
    done = st.transition(card_id, frozenset({S_REPLACED}), S_REPLACED,
                         expect_updated_at=cur.updated_at,
                         result=_short_block(text or _NO_RESULT, 1500))
    if done:
        _notify(done)
    return done


def _is_number(word: str) -> bool:
    """A positive FINITE number (money L-2: ``inf`` and ``nan`` are not bounds)."""
    try:
        value = float(word)
    except ValueError:
        return False
    return math.isfinite(value) and value > 0


def _bound_only(extra: List[str]) -> bool:
    """What a verb may ADD to the typed words in its confirm line: nothing, a
    price (``/pay``'s max), or ``max <usd>`` (``/send``, ``/swap``). Anything
    else is refused — a quote echoes third-party text (a token's name, a URL's
    reply), and a line that ADDS ``to 0x…`` would change what the confirm does."""
    if not extra:
        return True
    if len(extra) == 1:
        return _is_number(extra[0])
    if len(extra) in (2, 4):
        pairs = list(zip(extra[::2], extra[1::2]))
        keys = [key.lower() for key, _ in pairs]
        return (len(set(keys)) == len(keys) and
                all(key.lower() in {"max", "min_raw"} and _is_number(value)
                    and (key.lower() != "min_raw" or value.isdecimal())
                    for key, value in pairs))
    return False


def confirm_line_in(verb: str, typed_args: List[str], text: str) -> Optional[List[str]]:
    """The confirm line a quote reply printed for THIS command, as its argument
    list without ``go`` — or None.

    The line must: start with ``verb``; end with ``go``; begin with every
    argument the owner typed (the verb may add one, like ``/pay``'s max price
    or ``/send``'s ``max``); and carry only argument-shaped words. A usage
    example or a placeholder (``<max_usd>``) never matches, so a card is only
    ever made from the verb's own confirm line for the quote just shown."""
    typed = [a for a in typed_args if a]
    bare_ok = verb in BARE_CONFIRMABLE_VERBS
    if (not typed and not bare_ok) or executes(typed):
        return None
    found: Optional[List[str]] = None
    distinct: set = set()
    for raw in (text or "").splitlines():
        idx = raw.find(verb + " ")
        if idx < 0:
            continue
        words = raw[idx:].strip().rstrip(".").strip("`").split()
        if (len(words) < (2 if bare_ok else 3) or words[0] != verb
                or words[-1].lower() != "go"):
            continue
        args = words[1:-1]
        if len(args) < len(typed):
            continue
        if args[:len(typed)] != typed:
            continue
        if not _valid_args(args) or not _bound_only(args[len(typed):]):
            continue
        found = args
        distinct.add(tuple(args))
    # CHAT-24: the quote text can carry third-party data (a token name, a
    # memo, a page title). Taking the LAST matching line made safety depend on
    # every verb printing its own hint last. Two DIFFERENT candidate lines are
    # ambiguous: make no card rather than pick one.
    if len(distinct) > 1:
        return None
    return found


#: 079 W5: the guard's STRUCTURED refusal, as the money rails print it — the
#: ``lane: refuse`` line of a tx_guard verdict (any spacing, ``; lane: refuse``
#: on the LP verbs) or the ``RESULT: NOT SENT`` line. Narrower than
#: :func:`failed_result` on purpose: that one matches any "refused" or "Nothing
#: was" in a quote body (a value-check note, a token name) and would hide good
#: quotes; this one matches only the guard saying no.
_REFUSE_LANE_RE = re.compile(r"\blane:\s*refuse\b", re.IGNORECASE)
_NOT_SENT_MARK = "RESULT: NOT SENT"

#: Confirmable verbs with no dry run under them (no guard verdict to read), so
#: the refusal check never applies: the quote IS the plan.
_NO_GUARD_VERBS = frozenset({"/restart", "/update"})


def refused_quote(text: Any) -> bool:
    """True when a quote reply carries the guard's structured refusal — the
    same order with ``go`` would be refused too, so it is not confirmable."""
    if not isinstance(text, str) or not text:
        return False
    return _NOT_SENT_MARK in text or bool(_REFUSE_LANE_RE.search(text))


def with_go_hint(body: str, hint: str) -> str:
    """``body + hint`` — the "add `go` to …" line a quote ends with — unless
    the body is a guard refusal. A refused quote that still said "add `go` to
    execute" invited a second act the guard refuses again (079 R6). The ONE
    rule every money seat (``surfaces/telegram/*_ops.py``) applies."""
    return body if refused_quote(body) else body + hint


def quote_card(user_id: str, verb: str, typed_args: List[str], reply: str, *,
               st: Optional[CardStore] = None,
               title: Optional[str] = None) -> Tuple[str, Optional[Card]]:
    """After a seat ran a quote: ``(text to show, card or None)``.

    No card when the verb is not confirmable, the owner already said ``go``,
    the reply carries the guard's refusal (:func:`refused_quote`; never for
    ``/restart``, ``/update``, which have no guard), or the reply
    printed no confirm line for this command (an error, a usage text). Then
    the reply is returned unchanged."""
    verb = (verb or "").lower()
    if not user_id or verb not in CONFIRMABLE_VERBS or not isinstance(reply, str):
        return reply, None
    if verb not in _NO_GUARD_VERBS and refused_quote(reply):
        return reply, None
    ack = _ack_step_card(user_id, verb, list(typed_args or []), reply, st=st)
    if ack is not None:
        return render_text(ack), ack
    args = confirm_line_in(verb, list(typed_args or []), reply)
    if args is None:
        return reply, None
    try:
        n_typed = len([a for a in (typed_args or []) if a])
        card = _new(user_id, KIND_QUOTE, ORIGIN_SYSTEM,
                    title or f"{verb} — quote", body=_strip_go_hint(reply, verb),
                    verb=verb, args=args, typed=args[:n_typed], ttl=QUOTE_TTL_S, st=st)
    except Exception:
        logger.warning("quote card could not be stored — plain quote shown", exc_info=True)
        return reply, None
    return render_text(card), card


#: The /send word for the extra confirmation of a NOT VERIFIED value.
ACK_WORD = "anyway"


def is_ack_step(args: List[str]) -> bool:
    """True for the args of the extra-confirmation step (they end in ``anyway``)."""
    return bool(args) and str(args[-1]).lower() == ACK_WORD


def _ack_step_card(user_id: str, verb: str, typed: List[str], reply: str, *,
                   st: Optional[CardStore] = None) -> Optional[Card]:
    """A ``/send`` quote whose value is NOT VERIFIED and that does not carry
    ``anyway`` yet: the extra confirmation (owner, 2026-10-17 — a wallet sends
    any token, after one more deliberate step). The card's one action runs the
    typed words plus ``anyway`` as a NEW quote; only that quote's Confirm sends.
    The args are the typed words only, never text from the quote."""
    from core.wallet.tx_guard_steps.value import UNVERIFIED_MARK
    typed = [a for a in typed if a]
    if (verb != "/send" or not typed or UNVERIFIED_MARK not in reply
            or any(a.lower() == ACK_WORD for a in typed)):
        return None
    args = typed + [ACK_WORD]
    if executes(args) or not _valid_args(args):
        return None
    body = "\n".join(ln for ln in reply.splitlines()
                     if not ln.strip().startswith("To continue:")).rstrip()
    try:
        return _new(user_id, KIND_PROPOSAL, ORIGIN_SYSTEM,
                    f"{verb} — ⚠️ value not verified", body=body, verb=verb,
                    args=args, ttl=QUOTE_TTL_S, st=st)
    except Exception:
        logger.warning("extra-check card could not be stored — plain text shown",
                       exc_info=True)
        return None


def _strip_go_hint(text: str, verb: str) -> str:
    """The quote without its "type this line again" hint — the card's Confirm
    replaces it (the card text keeps the typed form in its Command line)."""
    out = []
    for raw in (text or "").splitlines():
        s = raw.strip()
        if verb + " " in s and s.rstrip("`. ").lower().endswith(" go"):
            continue
        out.append(raw)
    return "\n".join(out).rstrip()


#: The verbs a FORM may build (the console's Money › Moves form, the button
#: builder): the two whose whole order is a handful of named fields.
FORM_VERBS = ("/send", "/swap")


def form_line(verb: str, form: dict) -> Tuple[Optional[List[str]], Optional[str]]:
    """``(words, None)`` — the QUOTE line (never ``go``) a form's fields make —
    or ``(None, why)``. The line is then run through the seat exactly as if the
    owner typed it, so the verb's own parser still has the last word; this
    only refuses what could never be one argument."""
    verb = "/" + str(verb or "").strip().lstrip("/").lower()
    if verb not in FORM_VERBS:
        return None, f"a form builds only {' and '.join(FORM_VERBS)}"
    f = {k: " ".join(str(v or "").split()) for k, v in (form or {}).items()}
    try:
        if not float(f.get("amount") or "0") > 0:
            raise ValueError
    except ValueError:
        return None, "the amount must be a number above zero"
    need = ("amount", "token", "to", "chain")
    missing = [k for k in need if not f.get(k)]
    if missing:
        return None, "missing: " + ", ".join(missing)
    words = [f["amount"], f["token"], "to", f["to"], "on", f["chain"].lower()]
    if verb == "/swap" and f.get("slippage"):
        words += ["slippage", f["slippage"]]
    if executes(words) or not _valid_args(words):
        return None, "each field must be one word with no spaces, backticks or angle brackets"
    return [verb] + words, None


def newest_quote_for(user_id: str, verb: str, args: List[str], *, since: float = 0.0,
                     st: Optional[CardStore] = None) -> Optional[Card]:
    """The owner's newest OPEN quote card for this exact command (the card a
    form's quote just made), or None when the quote made none."""
    want = [a.lower() for a in args]
    for c in (st or store()).open_cards(str(user_id), limit=10):
        if (c.kind == KIND_QUOTE and c.verb == verb and c.created_at >= since
                and [a.lower() for a in c.args[:len(want)]] == want):
            return c
    return None


def proposal_card(user_id: str, verb: str, args: List[str], *, why: str = "",
                  session_id: Optional[str] = None, group_id: Optional[str] = None,
                  st: Optional[CardStore] = None) -> Card:
    """An agent-proposed owner action (money, ``/restart``,
    ``/update``). Raises ValueError on a bad shape."""
    verb = (verb or "").strip().lower()
    if not verb.startswith("/"):
        verb = "/" + verb
    if verb not in CONFIRMABLE_VERBS:
        raise ValueError(f"{verb} is not a verb a card can carry "
                         f"({', '.join(sorted(CONFIRMABLE_VERBS))})")
    args = [str(a) for a in (args or []) if str(a).strip()]
    if not args and verb not in BARE_CONFIRMABLE_VERBS:
        raise ValueError("a proposal needs the verb's arguments")
    if executes(args) or any(a.lower().startswith("id=") for a in args):
        raise ValueError("a proposal never carries `go` (or a payment id) — the owner "
                         "confirms the quote")
    if any(a.lower() == ACK_WORD for a in args):
        # money L-1: `anyway` is the OWNER's extra step for a value that is not
        # verified (the seat's own card adds it). A proposal carrying it would
        # skip that step and land straight on a confirmable quote.
        raise ValueError(f"a proposal never carries `{ACK_WORD}` — the owner takes that "
                         f"extra step on the quote's own card")
    first = args[0].lower() if args else ""
    shape = _PROPOSAL_SUBCOMMANDS.get(verb)
    if shape is not None and first not in shape:
        raise ValueError(f"{verb} proposals are only: {' | '.join(sorted(shape))}")
    if len(args) > 24 or not _valid_args(args):
        raise ValueError("each argument must be one word with no spaces, "
                         "backticks or angle brackets")
    if verb == "/send":
        _refuse_send_ticker(args)
    return _new(user_id, KIND_PROPOSAL, ORIGIN_AGENT, f"Proposed: {verb}",
                body=_short_block(why, 600), verb=verb, args=args,
                session_id=session_id, group_id=group_id, ttl=PROPOSAL_TTL_S, st=st)


def card_words(text: Any) -> List[str]:
    """Free text (a task, a goal title) as card-line words: whitespace-split, a
    backtick or angle bracket replaced (``_ARG_RE`` refuses them). Raises
    ValueError for one word longer than an argument may be.

    The words must come back as the SAME prose when a seat runs the line:
    a quote mark or backslash is replaced by a typographic one (the REPL splits
    a line with ``shlex``, where ``it's`` is an open quote). The job card's
    builder (``tools.goal_tools.prose_words``) also disarms a prose word shaped
    like a grant option (085 W3)."""
    table = {i: " " for i in range(32)}
    table.update({ord("`"): "\u2019", ord("<"): "(", ord(">"): ")",
                  ord("'"): "\u2019", ord('"'): "\u201d", ord("\\"): "/"})
    raw = str(text or "").translate(table)
    words = raw.split()
    if any(len(w) > 256 for w in words):
        raise ValueError("one word is longer than 256 characters")
    return words


def job_card(user_id: str, words: List[str], *, title: str, body: str = "",
             session_id: Optional[str] = None, st: Optional[CardStore] = None) -> Card:
    """085 W3 (D3): the agent's standing money work as ONE owner decision.

    ``words`` is the owner seat's own line (``/goal new …``, ``/goal grant <id> …``,
    ``/job new …``, ``/job edit <id> grant …``). Confirm runs it on the seat exactly
    as if the owner typed it, so the seat's own owner gate, room refusal and lane
    gate run again; the seat writes the row with ``actor="owner_seat"``. Raises
    ValueError on any other shape."""
    words = [str(w) for w in (words or []) if str(w).strip()]
    verb = words[0].lower() if words else ""
    shape = JOB_CARD_SUBCOMMANDS.get(verb)
    if shape is None or len(words) < 3 or words[1].lower() not in shape:
        raise ValueError("a job card carries only /goal new|grant or /job new|edit")
    if verb == "/job" and words[1].lower() == "edit" and (
            len(words) < 5 or words[3].lower() != "grant"):
        raise ValueError("a job card edits a job only as /job edit <id> grant …")
    args = words[1:]
    if len(args) > MAX_JOB_WORDS or not _valid_args(args):
        raise ValueError("the line is too long or has a word a command cannot carry")
    return _new(user_id, KIND_JOB, ORIGIN_AGENT, title, body=body, verb=verb, args=args,
                session_id=session_id, ttl=PROPOSAL_TTL_S, st=st)


_TICKER = re.compile(r"^[A-Za-z][A-Za-z0-9.]{0,11}$")


def _refuse_send_ticker(args: List[str]) -> None:
    """``/send`` takes `native`, the chain's own symbol or a contract address —
    never a ticker. A card carrying one fails on the owner's tap (2026-10-14:
    `/send 30 USDC … on base`), so refuse it here, naming the pinned USDC."""
    token = args[1] if len(args) > 1 else ""
    if token.lower() == "native" or not _TICKER.match(token):
        return
    chain = ""
    for i, w in enumerate(args[:-1]):
        if w.lower() == "on":
            chain = args[i + 1].lower()
    try:
        from core.wallet import chains
        row = chains.get(chain)
    except Exception:
        row = None
    own = "SOL" if chain == "solana" else str(getattr(row, "native_symbol", "") or "")
    if own and token.upper() == own.upper():
        return
    hint = ""
    usdc = str(getattr(row, "usdc", "") or "")
    if token.upper() == "USDC" and usdc:
        hint = f" USDC on {chain} is {usdc}."
    raise ValueError(f"/send needs `native` or the token's contract address, not the "
                     f"ticker {token!r} — the owner's tap would fail.{hint}")


def choice_card(user_id: str, question: str, options: List[str], *,
                session_id: Optional[str] = None, ttl: float = CHOICE_TTL_S,
                st: Optional[CardStore] = None) -> Card:
    """An agent's question with 2–6 options. Raises ValueError on a bad shape."""
    opts = [" ".join(str(o).split()) for o in (options or []) if str(o).strip()]
    if not (2 <= len(opts) <= MAX_OPTIONS):
        raise ValueError(f"give 2 to {MAX_OPTIONS} options")
    if any(len(o) > MAX_OPTION_CHARS for o in opts):
        raise ValueError(f"an option is at most {MAX_OPTION_CHARS} characters")
    q = " ".join(str(question or "").split())
    if not q:
        raise ValueError("a choice needs a question")
    return _new(user_id, KIND_CHOICE, ORIGIN_AGENT, _short(q, MAX_TITLE_CHARS),
                options=opts, session_id=session_id, ttl=ttl, st=st)


# ── pressing ─────────────────────────────────────────────────────────────────

@dataclass
class Press:
    """What a seat does with a tap.

    ``run`` — a command line the seat must dispatch AS THE OWNER TYPING IT
    (the confirm line, or a quote line); ``reply`` — text to show; ``card`` —
    the card after the transition (None when there is no such card)."""
    reply: str = ""
    run: Optional[str] = None
    card: Optional[Card] = None
    #: A builder pick (1-based): the SEAT advances the card (it reads balances
    #: and addresses, which core does not).
    pick: Optional[int] = None


_NO_CARD = "No such card — it may belong to another chat or be gone. See /cards."


def press(card_id: str, act: str, user_id: str, *,
          st: Optional[CardStore] = None) -> Press:
    """Apply one tap. Pure store logic: the seat runs ``Press.run`` itself."""
    st = st or store()
    card = st.get(card_id) if card_id else None
    if card is None or str(card.user_id) != str(user_id):
        return Press(reply=_NO_CARD)
    if card.state != S_OPEN:
        return Press(reply=f"That card is already decided — "
                           f"{_STATE_WORDS.get(card.state, card.state)}", card=card)
    if card.expired() and act == "re" and card.kind in (KIND_QUOTE, KIND_PROPOSAL):
        # 084 R14: the Refresh button is still drawn on an expired quote — it
        # runs a fresh quote (the old card is REPLACED by it, so its group
        # waits for the new quote instead of settling on "expired").
        done = st.transition(card_id, frozenset({S_OPEN}), S_REPLACED)
        if not done:
            return Press(reply="That card was decided a moment ago.", card=st.get(card_id))
        _notify(done)
        return Press(run=done.refresh_line, card=done)
    if card.expired():
        done = st.transition(card_id, frozenset({S_OPEN}), S_EXPIRED)
        if done:
            _notify(done)
        hint = (f" Refresh: {card.refresh_line}" if card.kind == KIND_QUOTE else "")
        return Press(reply=f"⌛ That card expired. Nothing was done.{hint}", card=done)
    if act == "no":
        done = st.transition(card_id, frozenset({S_OPEN}), S_CANCELLED)
        if done:
            _notify(done)
            return Press(reply=_STATE_WORDS[S_CANCELLED], card=done)
        return Press(reply="That card was decided a moment ago.", card=st.get(card_id))
    if card.kind in (KIND_QUOTE, KIND_JOB) and act == "ok":
        done = st.transition(card_id, frozenset({S_OPEN}), S_CONFIRMED)
        if not done:
            return Press(reply="That card was decided a moment ago — nothing was "
                               "run twice.", card=st.get(card_id))
        _notify(done)
        # A job card's line is the seat's own write verb: no `go` exists for it.
        return Press(run=done.line if done.kind == KIND_JOB else done.confirm_line,
                     card=done)
    if card.kind in (KIND_QUOTE, KIND_PROPOSAL) and act == "re":
        done = st.transition(card_id, frozenset({S_OPEN}), S_REPLACED)
        if not done:
            return Press(reply="That card was decided a moment ago.", card=st.get(card_id))
        _notify(done)
        return Press(run=done.refresh_line, card=done)
    if card.kind == KIND_BUILDER and act.isdigit():
        i = int(act)
        if not (1 <= i <= len(card.options)):
            return Press(reply="That option does not exist on this card.", card=card)
        return Press(pick=i, card=card)
    if card.kind == KIND_CHOICE and act.isdigit():
        i = int(act)
        if not (1 <= i <= len(card.options)):
            return Press(reply="That option does not exist on this card.", card=card)
        pick = card.options[i - 1]
        done = st.transition(card_id, frozenset({S_OPEN}), S_DONE, answer=pick)
        if not done:
            return Press(reply="That card was answered a moment ago.", card=st.get(card_id))
        _notify(done)
        return Press(reply=f"Noted: {pick}", card=done)
    return Press(reply="That button does not apply to this card.", card=card)


def tap_line(text: str, user_id: str, *, st: Optional[CardStore] = None) -> str:
    """The owner-thread words for a card tap: the LINE the tap stands for, not
    the opaque ``/card_<id>_ok`` token (079 R5 — the agent read "/card_3f…_ok"
    and could not say what the owner had confirmed). Read BEFORE the press, so
    the card is still the one the owner saw. Any other text, an unknown card or
    an unreadable store returns ``text`` unchanged. Never raises."""
    try:
        words = (text or "").strip().split()
        cid, act = parse_card_token(text)
        if cid is None and len(words) == 3 and words[0].lower() == "/cards":
            cid, act = words[1].lower(), words[2].lower()
        if cid is None:
            return text
        card = (st or store()).get(cid)
        if card is None or str(card.user_id) != str(user_id):
            return text
        if act == "ok" and card.kind == KIND_QUOTE:
            return card.confirm_line
        if act == "ok" and card.kind == KIND_JOB:
            return card.line
        if act == "re" and card.kind in (KIND_QUOTE, KIND_PROPOSAL):
            return card.refresh_line
        if act == "no":
            return f"(cancelled the card: {card.title})"
        if act.isdigit() and 1 <= int(act) <= len(card.options):
            return f"(picked \"{card.options[int(act) - 1]}\" on the card: {card.title})"
    except Exception:
        logger.debug("card tap line unreadable (fail-open)", exc_info=True)
    return text


_NO_RESULT = "No result was reported — check /book before trying again."

#: Words a money rail or a seat uses when the act did NOT (certainly) happen.
#: A card is marked done only when none of them is in the result: a guard
#: refusal ("RESULT: NOT SENT"), a revert, an unconfirmed broadcast and an
#: unknown outcome all read as "not confirmed as done", never as ✅.
_FAILURE_MARKS = ("❌", "🔒", "Command failed", "NOT SENT", "REVERTED",
                  "NOT CONFIRMED", "refused", "outcome unknown", "Nothing was",
                  "price moved", _NO_RESULT)

#: The rails' own words for a transfer that landed (EVM send; Solana).
_SENT_MARKS = ("SENT AND CONFIRMED", "RESULT: CONFIRMED")


def failed_result(text: str) -> bool:
    return any(m in (text or "") for m in _FAILURE_MARKS)


def finish(card_id: str, result_text: str, *, st: Optional[CardStore] = None) -> Optional[Card]:
    """Record what a confirmed card's run produced (the in-place edit)."""
    st = st or store()
    text = (result_text or "").strip() or _NO_RESULT
    new_state = S_FAILED if failed_result(text) else S_DONE
    done = st.transition(card_id, frozenset({S_CONFIRMED}), new_state,
                         result=_short_block(text, 1500))
    if done is None and text != _NO_RESULT:
        # AC2: ``sweep_stuck`` closes a card that reported nothing for 30 min
        # as "no result" — but a `/bridge … go` that waited on the owner's
        # /approve can still finish later. Its REAL result replaces the
        # placeholder (CAS on the swept row, so only that row is touched).
        cur = st.get(card_id)
        if cur is not None and cur.state == S_FAILED and cur.result == _NO_RESULT:
            done = st.transition(card_id, frozenset({S_FAILED}), new_state,
                                 expect_updated_at=cur.updated_at,
                                 result=_short_block(text, 1500))
    if done:
        _notify(done)
    return done


def sweep(*, st: Optional[CardStore] = None) -> int:
    """The timed sweep (084): expire overdue cards and close stuck ones, with
    the listeners + settle hook run for each. Returns how many moved."""
    moved = (st or store()).sweep_expired()
    for c in moved:
        _notify(c)
    return len(moved)


def list_reply(user_id: str, *, st: Optional[CardStore] = None) -> str:
    """``/cards`` — the owner's open cards, newest first."""
    try:
        st = st or store()
        for stuck in st.sweep_stuck(str(user_id)):
            _notify(stuck)
        cards = st.open_cards(str(user_id))
    except Exception:
        logger.warning("/cards: store unreadable", exc_info=True)
        return "I could not read the card store, so I cannot say what is open."
    if not cards:
        return "No open cards."
    out = ["Open cards:"]
    for c in cards:
        who = "agent" if c.origin == ORIGIN_AGENT else "you"
        out.append(f"• {c.title} ({c.kind}, from {who})")
        for a in card_actions(c):
            out.append(f"    {a.label}: {a.command}")
    return "\n".join(out)


#: ``result`` of a choice the owner answered by TYPING instead of tapping.
TEXT_REPLY_RESULT = "owner replied in text: "


def close_choices_for_text(user_id: str, session_id: str, text: str, *,
                           st: Optional[CardStore] = None) -> List[Card]:
    """A typed line into a session with an OPEN choice card closes that card,
    so ``present_choice`` stops waiting and the agent reads the line. The text
    is never parsed into a pick (prod 2026-10-15: "Judge" sat 2.5 min)."""
    if not session_id:
        return []
    try:
        st = st or store()
        open_ = st.open_cards(user_id)
    except Exception:
        logger.debug("choice close: card store unreadable", exc_info=True)
        return []   # fail open: never block the owner's line over a card
    closed = []
    for c in open_:
        if c.kind != KIND_CHOICE or c.session_id != session_id:
            continue
        try:
            done = st.transition(c.card_id, frozenset({S_OPEN}), S_CANCELLED,
                                 result=TEXT_REPLY_RESULT + " ".join(str(text or "").split())[:300])
        except Exception:
            logger.debug("choice close: transition failed", exc_info=True)
            continue
        if done is not None:
            closed.append(done)
            try:
                _notify(done)
            except Exception:
                logger.debug("choice close notify failed", exc_info=True)
    return closed


def wait_for_answer(card_id: str, timeout_s: float, *, poll_s: float = 1.0,
                    st: Optional[CardStore] = None) -> Optional[Card]:
    """Block (a worker thread) until the card leaves ``open`` or the time is up."""
    st = st or store()
    end = time.time() + max(0.0, timeout_s)
    while True:
        card = st.get(card_id)
        if card is None or card.state != S_OPEN:
            return card
        if time.time() >= end:
            return card
        time.sleep(poll_s)


__all__ = [
    "ACTS", "CONFIRMABLE_VERBS", "Card", "CardStore", "TEXT_REPLY_RESULT", "KIND_BUILDER", "KIND_CHOICE",
    "KIND_PROPOSAL",
    "KIND_QUOTE", "Press", "add_listener", "card_actions", "card_token", "choice_card", "close_choices_for_text",
    "confirm_line_in", "finish", "list_reply", "parse_card_token", "press",
    "proposal_card", "quote_card", "refused_quote", "remove_listener", "render_text",
    "set_store", "store", "tap_line", "wait_for_answer", "with_go_hint",
]
