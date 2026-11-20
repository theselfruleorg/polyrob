"""Buttons that run a whole command line (2026-11-20 Telegram UX review, W2).

A button carries ONE token (``core.surfaces.actions._TOKEN_RE``): ``/model reset``,
``/chat 2`` or ``/job pause <id>`` cannot ride a button as typed. The one-token rule is
right — it stops a token the model quotes in its text from becoming an act — so the line
is STORED instead and the button carries an opaque id:

    reply helper:  nav.actions(user_id, [NavButton("Reset", "/model reset")])
    the button:    /nav_3f9a1c2b4d5e
    a tap:         the seat looks the line up for the SAME tenant and runs it as if the
                   owner typed it — every gate (owner, group, a money quote) runs again.

Rules that keep this safe:

* only code writes a line (a reply helper); the model has no path here, and an id is
  random, so a ``/nav_…`` token in model text names nothing it could choose;
* a line must start with a known verb and must never carry a go word
  (``cards.executes``): a money line through a button only QUOTES, and the quote
  card is the one place money is confirmed;
* a tap never consumes the line (a menu button works again); a line expires after
  :data:`TTL_S`, and an expired tap says so and names ``/help``.
"""
from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

from core.surfaces.tappable import NAV_TOKEN_RE, NAV_TOKEN_PREFIX as TOKEN_PREFIX
#: How long a stored line answers: a menu stays usable for a month.
TTL_S = 30 * 24 * 3600
#: Lines kept per tenant; the oldest go first.
MAX_PER_TENANT = 2000
EXPIRED_TEXT = "⌛ This button has expired. Send /help for the menu."
#: A button whose stored line could not be read (the store failed, not the button).
UNREADABLE_TEXT = ("⚠️ I could not read what this button runs (its store did not open). "
                   "Nothing ran. Type the command, or send /help for the menu.")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nav (
    nav_id     TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    line       TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS nav_user_line ON nav(user_id, line);
"""


@dataclass(frozen=True)
class NavButton:
    """One button: its label, the line a tap runs, and its colour
    (``core.surfaces.envelopes.ACTION_STYLES``)."""
    label: str
    line: str
    style: str = "default"


def is_nav_token(token: str) -> bool:
    return bool(NAV_TOKEN_RE.match((token or "").strip().lower()))


def line_ok(line: str) -> bool:
    """A line a button may carry: a known verb first, never a go word."""
    words = (line or "").split()
    if not words or not words[0].startswith("/"):
        return False
    from core.verbs import verb_for
    if verb_for(words[0].lower()) is None:
        return False
    from core.surfaces.go_words import executes
    return not executes(words[1:])


class NavStore:
    """``nav.db`` — one row per (tenant, line)."""

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            from core.runtime_paths import data_home_db_path
            db_path = data_home_db_path("nav.db")
        self.db_path = str(db_path)
        from core.sqlite_util import init_schema
        init_schema(self.db_path, _SCHEMA, mkdir=True)

    def _conn(self):
        from core.sqlite_util import wal_connect
        return wal_connect(self.db_path)

    def put(self, user_id: str, line: str, *, now: Optional[float] = None) -> str:
        """The id of ``line`` for this tenant (the same line keeps its id; its expiry
        moves forward)."""
        now = time.time() if now is None else now
        conn = self._conn()
        try:
            row = conn.execute("SELECT nav_id FROM nav WHERE user_id=? AND line=?",
                               (user_id, line)).fetchone()
            if row:
                conn.execute("UPDATE nav SET expires_at=? WHERE nav_id=?",
                             (now + TTL_S, row[0]))
                conn.commit()
                return row[0]
            nav_id = secrets.token_hex(6)
            conn.execute("INSERT INTO nav VALUES (?,?,?,?,?)",
                         (nav_id, user_id, line, now, now + TTL_S))
            conn.execute("DELETE FROM nav WHERE expires_at < ?", (now,))
            conn.execute(
                "DELETE FROM nav WHERE user_id=? AND nav_id NOT IN (SELECT nav_id FROM nav "
                "WHERE user_id=? ORDER BY expires_at DESC LIMIT ?)",
                (user_id, user_id, MAX_PER_TENANT))
            conn.commit()
            return nav_id
        finally:
            conn.close()

    def get(self, user_id: str, nav_id: str, *, now: Optional[float] = None) -> Optional[str]:
        now = time.time() if now is None else now
        conn = self._conn()
        try:
            row = conn.execute("SELECT line, expires_at FROM nav WHERE nav_id=? AND user_id=?",
                               (nav_id, user_id)).fetchone()
        finally:
            conn.close()
        if not row or row[1] < now:
            return None
        return row[0]


_STORE: Optional[NavStore] = None


def store() -> NavStore:
    global _STORE
    if _STORE is None:
        _STORE = NavStore()
    return _STORE


def set_store(s: Optional[NavStore]) -> None:
    """Test seam: point the process at another store (None = the default)."""
    global _STORE
    _STORE = s


def actions(user_id: Optional[str], buttons: Sequence[NavButton]) -> List[object]:
    """``Action``s for ``buttons``. A bare one-token verb line rides the button as
    typed; any other line is stored. Fail-open: a store fault or a refused line drops
    that button (the reply text still names every command), never the reply."""
    from core.surfaces.actions import is_action_command
    from core.surfaces.envelopes import Action
    out: List[object] = []
    for b in buttons or ():
        line = " ".join((b.line or "").split())
        try:
            if is_action_command(line):
                token = line
            elif user_id and line_ok(line):
                token = TOKEN_PREFIX + store().put(str(user_id), line)
            else:
                logger.warning("nav: button %r refused: %r is not a button line",
                               b.label, line)
                continue
            out.append(Action(label=b.label, command=token, style=b.style))
        except Exception:
            logger.warning("nav: button %r dropped", b.label, exc_info=True)
    return out


def resolve(user_id: Optional[str], token: str) -> Tuple[Optional[str], str]:
    """``(line, "")`` — the line a ``/nav_<id>`` tap runs for this tenant — or
    ``(None, why)``. An unknown, expired or other tenant's id is one answer
    (:data:`EXPIRED_TEXT`); a store that cannot be read says so
    (:data:`UNREADABLE_TEXT`), never "expired"."""
    if not (user_id and is_nav_token(token)):
        return None, EXPIRED_TEXT
    try:
        line = store().get(str(user_id), token.strip().lower()[len(TOKEN_PREFIX):])
    except Exception:
        logger.warning("nav: line not read for %s", token, exc_info=True)
        return None, UNREADABLE_TEXT
    return (line, "") if line and line_ok(line) else (None, EXPIRED_TEXT)


__all__ = ["NavButton", "NavStore", "actions", "is_nav_token", "line_ok",
           "resolve", "EXPIRED_TEXT", "TOKEN_PREFIX", "UNREADABLE_TEXT"]
