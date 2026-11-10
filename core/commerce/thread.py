"""Public-thread commerce (109): the contract a surface fills, and its two small tables.

A buyer may order in a PUBLIC reply thread that the agent started (an announcement),
on any surface whose pack registers a :class:`ThreadChannel` for it. Code answers —
no model reads the reply (``modules.commerce.storefront.thread_intake``).

* :class:`PublicPost` — one post, its author authenticated by the PLATFORM (the
  buyer key), its conversation root and the post it answers.
* :class:`ThreadChannel` — what a pack implements: read new replies after a cursor,
  look one post up, say whether a write is affordable, and reply under a post.
* ``offer_threads`` — which post announced which offer on which surface (written by
  ``offer_manage(op="announce")``); the intake reads ONLY replies under these roots.
* ``thread_cursors`` — one read cursor per surface, plus the last poll and its stop
  reason (status).

Nothing here names a platform or a product. Tier-0: imports nothing above ``core``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, List, Optional, Protocol, Tuple

THREAD_SCHEMA = """
CREATE TABLE IF NOT EXISTS offer_threads (
    surface     TEXT NOT NULL,
    root_id     TEXT NOT NULL,
    offer_id    TEXT NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (surface, root_id)
);
CREATE INDEX IF NOT EXISTS idx_offer_threads_offer ON offer_threads(offer_id);
CREATE TABLE IF NOT EXISTS thread_cursors (
    surface      TEXT PRIMARY KEY,
    since_id     TEXT,
    polled_at    REAL,
    stop_reason  TEXT
);
"""


@dataclass(frozen=True)
class PublicPost:
    surface: str
    post_id: str
    #: The platform-authenticated author id: the buyer key. Never a handle.
    author_id: str
    text: str
    #: The conversation root (an announcement, for a post the intake reads).
    root_id: str
    #: The post this one answers ("" = not a reply).
    reply_to_id: str = ""
    created_at: float = 0.0
    #: True = an edited version, not the first one. The intake ignores it: the
    #: buyer posts a new reply (an edit could rewrite an address after the answer).
    edited: bool = False


class ThreadChannel(Protocol):
    """One surface's public threads. Every method fails closed and never raises."""

    surface: str

    async def poll(self, since_id: Optional[str]) -> Tuple[List[PublicPost], Optional[str]]:
        """Replies addressed to the agent after ``since_id``, oldest first, and the
        new cursor. Raises :class:`ThreadUnavailable` when the platform refuses."""

    async def lookup(self, post_id: str) -> Optional[PublicPost]:
        """One post, or ``None``. ``author_id`` lets the caller check it is the agent's."""

    def agent_id(self) -> str:
        """The agent's own author id on this surface ("" = unknown)."""

    def can_reply(self) -> bool:
        """True when a reply is affordable now (the surface's write share)."""

    async def reply(self, post_id: str, text: str, *, subject: str = "",
                    user_id: str = "") -> bool:
        """Reply under ``post_id`` for the offer owner ``user_id`` (the tenant whose
        live card approved these replies). True only when the platform accepted it."""


class ThreadUnavailable(RuntimeError):
    """The platform refused a read (no credit, no auth, an outage). The text is the
    stop reason a person reads in /status."""


# -- the two tables ------------------------------------------------------------------

def _p(path: Optional[str]) -> str:
    from core.commerce.store import init_schema
    return init_schema(path)


def _read(path, sql, args, fetch):
    from core.commerce.store import read
    return read(path, sql, args, fetch)


def announce(offer_id: str, surface: str, root_id: str, *, path: Optional[str] = None,
             now: Optional[float] = None) -> None:
    """Record ``root_id`` as an announcement of ``offer_id`` on ``surface``. A root
    belongs to one offer; re-recording the same pair is a no-op."""
    from core.sqlite_util import execute_retry
    p = _p(path)
    cur = offer_of(surface, root_id, path=p)
    if cur and cur != offer_id:
        raise ValueError(f"post {root_id} already announces offer {cur}")
    execute_retry(p, "INSERT OR IGNORE INTO offer_threads (surface, root_id, offer_id, "
                     "created_at) VALUES (?,?,?,?)",
                  (str(surface), str(root_id), str(offer_id),
                   time.time() if now is None else float(now)))


def offer_of(surface: str, root_id: str, *, path: Optional[str] = None) -> Optional[str]:
    r = _read(path, "SELECT offer_id FROM offer_threads WHERE surface=? AND root_id=?",
              (str(surface), str(root_id)), "one")
    return r["offer_id"] if r else None


def threads(*, offer_id: Optional[str] = None, surface: Optional[str] = None,
            path: Optional[str] = None) -> List[Any]:
    sql, args = "SELECT * FROM offer_threads WHERE 1=1", []
    if offer_id:
        sql, args = sql + " AND offer_id=?", args + [str(offer_id)]
    if surface:
        sql, args = sql + " AND surface=?", args + [str(surface)]
    return list(_read(path, sql + " ORDER BY created_at", tuple(args), "all") or [])


def cursor(surface: str, *, path: Optional[str] = None) -> Optional[Any]:
    return _read(path, "SELECT * FROM thread_cursors WHERE surface=?", (str(surface),), "one")


def save_cursor(surface: str, since_id: Optional[str], *, stop_reason: str = "",
                path: Optional[str] = None, now: Optional[float] = None) -> None:
    """Move the cursor (``since_id=None`` keeps the old one) and stamp the poll."""
    from core.sqlite_util import execute_retry
    p = _p(path)
    ts = time.time() if now is None else float(now)
    execute_retry(p, "INSERT INTO thread_cursors (surface, since_id, polled_at, stop_reason) "
                     "VALUES (?,?,?,?) ON CONFLICT(surface) DO UPDATE SET "
                     "since_id=COALESCE(excluded.since_id, thread_cursors.since_id), "
                     "polled_at=excluded.polled_at, stop_reason=excluded.stop_reason",
                  (str(surface), since_id, ts, str(stop_reason or "")[:300]))


__all__ = ["PublicPost", "THREAD_SCHEMA", "ThreadChannel", "ThreadUnavailable", "announce",
           "cursor", "offer_of", "save_cursor", "threads"]
