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

import json
import logging
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
})

#: A card tap token: ``/card_<10 hex>_<act>``. Acts: ``ok`` (confirm), ``no``
#: (cancel), ``re`` (refresh / quote), ``1``–``6`` (pick an option).
_ID_HEX = 10
_CARD_TOKEN_RE = re.compile(r"^/card_([0-9a-f]{10})_(ok|no|re|[1-6])$")
ACTS = ("ok", "no", "re") + tuple(str(i) for i in range(1, MAX_OPTIONS + 1))

#: One argument of a carded command line: no whitespace, no control bytes, no
#: backtick or angle bracket (a usage placeholder like ``<max_usd>`` is not an
#: argument), bounded.
_ARG_RE = re.compile(r"^[^\s`<>\x00-\x1f]{1,256}$")
_GO_WORDS = ("go", "execute", "confirm")


#: Verbs with subcommands: the only ones a PROPOSAL may name. ``/wallet
#: autonomous <usd>`` acts without a `go` (it raises the agent's own no-ask
#: ceiling), so it is never proposable; neither is anything else that acts
#: without its quote.
_PROPOSAL_SUBCOMMANDS = {
    "/wallet": frozenset({"trust", "untrust"}),
    "/nft": frozenset({"transfer", "revoke", "send"}),
    "/identity": frozenset({"register", "set-uri"}),
}


def executes(args: List[str]) -> bool:
    """True when these words would EXECUTE rather than quote: a `go` word
    ANYWHERE (``/pay`` strips ``id=`` first; ``/writeoff`` reads `go` as its
    third word, before a free-text reason)."""
    return any(str(a).lower() in _GO_WORDS for a in (args or []))


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
    if card.kind == KIND_PROPOSAL:
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
    if card.kind in (KIND_QUOTE, KIND_PROPOSAL) and card.line:
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
             "draft")


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


def set_store(s: Optional[CardStore]) -> None:
    """Test seam: point the process at another store (None = the default)."""
    global _STORE
    _STORE = s


# ── listeners: a seat that can edit a card in place registers here ──────────

_LISTENERS: List[Callable[[Card], Any]] = []


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


_PENDING_EDITS: set = set()


# ── building cards ───────────────────────────────────────────────────────────

def _valid_args(args: List[str]) -> bool:
    return all(isinstance(a, str) and _ARG_RE.match(a) for a in args)


def _new(user_id: str, kind: str, origin: str, title: str, *, body: str = "",
         verb: str = "", args: Optional[List[str]] = None,
         options: Optional[List[str]] = None, session_id: Optional[str] = None,
         ttl: float, st: Optional[CardStore] = None) -> Card:
    now = time.time()
    card = Card(card_id=new_card_id(), user_id=str(user_id), kind=kind, origin=origin,
                title=_short(title, MAX_TITLE_CHARS), body=(body or "")[:MAX_BODY_CHARS],
                verb=verb, args=list(args or []), options=list(options or []),
                session_id=session_id, created_at=now, expires_at=now + ttl,
                updated_at=now)
    return (st or store()).insert(card)


def _is_number(word: str) -> bool:
    try:
        return float(word) > 0
    except ValueError:
        return False


def _bound_only(extra: List[str]) -> bool:
    """What a verb may ADD to the typed words in its confirm line: nothing, a
    price (``/pay``'s max), or ``max <usd>`` (``/send``, ``/swap``). Anything
    else is refused — a quote echoes third-party text (a token's name, a URL's
    reply), and a line that ADDS ``to 0x…`` would change what the confirm does."""
    if not extra:
        return True
    if len(extra) == 1:
        return _is_number(extra[0])
    return len(extra) == 2 and extra[0].lower() == "max" and _is_number(extra[1])


def confirm_line_in(verb: str, typed_args: List[str], text: str) -> Optional[List[str]]:
    """The confirm line a quote reply printed for THIS command, as its argument
    list without ``go`` — or None.

    The line must: start with ``verb``; end with ``go``; begin with every
    argument the owner typed (the verb may add one, like ``/pay``'s max price
    or ``/send``'s ``max``); and carry only argument-shaped words. A usage
    example or a placeholder (``<max_usd>``) never matches, so a card is only
    ever made from the verb's own confirm line for the quote just shown."""
    typed = [a for a in typed_args if a]
    if not typed or executes(typed):
        return None
    found: Optional[List[str]] = None
    for raw in (text or "").splitlines():
        idx = raw.find(verb + " ")
        if idx < 0:
            continue
        words = raw[idx:].strip().rstrip(".").strip("`").split()
        if len(words) < 3 or words[0] != verb or words[-1].lower() != "go":
            continue
        args = words[1:-1]
        if len(args) < len(typed):
            continue
        if [a.lower() for a in args[:len(typed)]] != [a.lower() for a in typed]:
            continue
        if not _valid_args(args) or not _bound_only(args[len(typed):]):
            continue
        found = args
    return found


def quote_card(user_id: str, verb: str, typed_args: List[str], reply: str, *,
               st: Optional[CardStore] = None,
               title: Optional[str] = None) -> Tuple[str, Optional[Card]]:
    """After a seat ran a quote: ``(text to show, card or None)``.

    No card when the verb is not confirmable, the owner already said ``go``,
    or the reply printed no confirm line for this command (an error, a usage
    text, a refusal). Then the reply is returned unchanged."""
    verb = (verb or "").lower()
    if not user_id or verb not in CONFIRMABLE_VERBS or not isinstance(reply, str):
        return reply, None
    args = confirm_line_in(verb, list(typed_args or []), reply)
    if args is None:
        return reply, None
    try:
        card = _new(user_id, KIND_QUOTE, ORIGIN_SYSTEM,
                    title or f"{verb} — quote", body=_strip_go_hint(reply, verb),
                    verb=verb, args=args, ttl=QUOTE_TTL_S, st=st)
    except Exception:
        logger.warning("quote card could not be stored — plain quote shown", exc_info=True)
        return reply, None
    return render_text(card), card


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
                  session_id: Optional[str] = None,
                  st: Optional[CardStore] = None) -> Card:
    """An agent-proposed money action. Raises ValueError on a bad shape."""
    verb = (verb or "").strip().lower()
    if not verb.startswith("/"):
        verb = "/" + verb
    if verb not in CONFIRMABLE_VERBS:
        raise ValueError(f"{verb} is not a verb a card can carry "
                         f"({', '.join(sorted(CONFIRMABLE_VERBS))})")
    args = [str(a) for a in (args or []) if str(a).strip()]
    if not args:
        raise ValueError("a proposal needs the verb's arguments")
    if executes(args) or any(a.lower().startswith("id=") for a in args):
        raise ValueError("a proposal never carries `go` (or a payment id) — the owner "
                         "confirms the quote")
    first = args[0].lower()
    shape = _PROPOSAL_SUBCOMMANDS.get(verb)
    if shape is not None and first not in shape:
        raise ValueError(f"{verb} proposals are only: {' | '.join(sorted(shape))}")
    if len(args) > 24 or not _valid_args(args):
        raise ValueError("each argument must be one word with no spaces, "
                         "backticks or angle brackets")
    return _new(user_id, KIND_PROPOSAL, ORIGIN_AGENT, f"Proposed: {verb}",
                body=_short_block(why, 600), verb=verb, args=args,
                session_id=session_id, ttl=PROPOSAL_TTL_S, st=st)


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
    if card.expired():
        done = st.transition(card_id, frozenset({S_OPEN}), S_EXPIRED)
        if done:
            _notify(done)
        hint = (f" Refresh: {card.line}" if card.kind == KIND_QUOTE else "")
        return Press(reply=f"⌛ That card expired. Nothing was done.{hint}", card=done)
    if act == "no":
        done = st.transition(card_id, frozenset({S_OPEN}), S_CANCELLED)
        if done:
            _notify(done)
            return Press(reply=_STATE_WORDS[S_CANCELLED], card=done)
        return Press(reply="That card was decided a moment ago.", card=st.get(card_id))
    if card.kind == KIND_QUOTE and act == "ok":
        done = st.transition(card_id, frozenset({S_OPEN}), S_CONFIRMED)
        if not done:
            return Press(reply="That card was decided a moment ago — nothing was "
                               "run twice.", card=st.get(card_id))
        _notify(done)
        return Press(run=done.confirm_line, card=done)
    if card.kind in (KIND_QUOTE, KIND_PROPOSAL) and act == "re":
        done = st.transition(card_id, frozenset({S_OPEN}), S_REPLACED)
        if not done:
            return Press(reply="That card was decided a moment ago.", card=st.get(card_id))
        _notify(done)
        return Press(run=done.line, card=done)
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
    done = st.transition(card_id, frozenset({S_CONFIRMED}),
                         S_FAILED if failed_result(text) else S_DONE,
                         result=_short_block(text, 1500))
    if done:
        _notify(done)
    return done


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
    "ACTS", "CONFIRMABLE_VERBS", "Card", "CardStore", "KIND_BUILDER", "KIND_CHOICE",
    "KIND_PROPOSAL",
    "KIND_QUOTE", "Press", "add_listener", "card_actions", "card_token", "choice_card",
    "confirm_line_in", "finish", "list_reply", "parse_card_token", "press",
    "proposal_card", "quote_card", "remove_listener", "render_text", "set_store",
    "store", "wait_for_answer",
]
