"""061 — the ONE owner thread.

The owner has ONE conversation with the agent on their phone; the agent kept it
in four places that never met (the session history, the ``user_delivery``
telemetry row, the correspondent conversation store, the episode summary), so a
reply to something an autonomous rail said three minutes earlier arrived in a
session that had never seen it (prod 2026-09-22 06:07Z — "Explain better" →
"Which thing should I explain better?").

This module is the one store and the one seam:

- every owner-bound line that actually REACHED the owner, on any rail
  (``deliver_user_message`` ``sent``, the outbox ``delivered`` transition, the
  quiet-hours release, the ``message`` tool's owner tier, the chat mirror), is
  recorded through :func:`record_owner_out`;
- every accepted owner line, on any owner seat (Telegram DM, console, REPL), is
  recorded through :func:`record_owner_in`;
- a session READS the thread — a tail on its first turn, a delta of what other
  sessions said since its last turn, the referent of a quote-reply, or (for an
  autonomous run) the slice this rail exchanged with the owner — and never owns
  it. ``render_block`` is the ONE renderer.

Rules: tenant-scoped on every read and write; a READ never creates the file;
fail-open (a missing or unreadable store costs the context block, never the
turn or the send); bounded (per-tenant row prune, per-body cap, per-block char
cap). Pure storage plus rendering — no surface, agent or transport imports
(``core/`` may not import ``agents.*``; the layering ratchet pins that).
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.env import bool_env, int_env
from core.sqlite_util import execute_retry, wal_connect

logger = logging.getLogger(__name__)

DB_NAME = "owner_thread.db"
SERVICE_NAME = "owner_thread"

_BODY_CAP = 2000          # chars kept per line
_KEEP_ROWS = 3000         # newest rows kept per tenant (~30 days of prod traffic)
_DEDUP_WINDOW_S = 120.0   # an identical line inside this window is the same line
BLOCK_MAX_CHARS = 2000    # the rendered <owner-thread> block, whatever the row count
LINE_MAX_CHARS = 400      # one rendered line

#: Outcomes of ``deliver_user_message`` that mean the owner actually SAW the
#: text. ``queued`` is recorded later by the outbox on ``delivered``;
#: ``quiet_held`` by the release; everything else stays in ``/missed``.
DELIVERED_OUTCOMES = frozenset({"sent"})


# --- flags -------------------------------------------------------------------

def owner_thread_enabled() -> bool:
    """Write the owner thread from every rail (default ON — repairs a live defect)."""
    return bool_env("OWNER_THREAD_ENABLED", True)


def owner_thread_inject() -> bool:
    """Read the thread into owner sessions (tail/delta/referent/rail slice)."""
    return owner_thread_enabled() and bool_env("OWNER_THREAD_INJECT", True)


def tail_hours() -> float:
    return float(max(1, int_env("OWNER_THREAD_TAIL_HOURS", 6)))


def tail_rows() -> int:
    return max(1, int_env("OWNER_THREAD_TAIL_ROWS", 12))


# --- store -------------------------------------------------------------------

class OwnerThreadStore:
    """One table: the owner's transcript, every rail, every seat, per tenant."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        conn = wal_connect(db_path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS owner_thread (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id      TEXT NOT NULL,
                    direction    TEXT NOT NULL,           -- 'out' (agent→owner) | 'in' (owner→agent)
                    ts           REAL NOT NULL,
                    via          TEXT NOT NULL DEFAULT '', -- the surface it travelled on
                    session_id   TEXT NOT NULL DEFAULT '',
                    mid          TEXT NOT NULL DEFAULT '', -- surface message id (referent key)
                    reply_to_mid TEXT NOT NULL DEFAULT '', -- inbound: the mid the owner answered
                    source       TEXT NOT NULL DEFAULT '', -- out: agent_send|cron|message_tool|mirror|…
                    rail_id      TEXT NOT NULL DEFAULT '', -- cron job id | goal id | ''
                    rail_label   TEXT NOT NULL DEFAULT '',
                    ask_id       TEXT NOT NULL DEFAULT '',
                    kind         TEXT NOT NULL DEFAULT '', -- in: comment|command|…
                    body         TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ot_user_ts ON owner_thread(user_id, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ot_mid ON owner_thread(user_id, via, mid)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ot_rail ON owner_thread(user_id, rail_id, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ot_sess "
                         "ON owner_thread(user_id, session_id, direction, ts)")
            conn.commit()
        finally:
            conn.close()

    # --- write ---
    def record(self, direction: str, user_id: str, body: str, *, via: str = "",
               session_id: str = "", mid: str = "", reply_to_mid: str = "",
               source: str = "", rail_id: str = "", rail_label: str = "",
               ask_id: str = "", kind: str = "", now: Optional[float] = None) -> int:
        ts = time.time() if now is None else float(now)
        uid = str(user_id or "")
        text = (body or "")[:_BODY_CAP]
        # Idempotent within a short window: two rails can legitimately see the
        # same reply (the turn latch and the delivery rail on a resumed session),
        # and a retry must never append the same line twice (OpenClaw's cron
        # result carries an idempotency key for the same reason).
        #
        # ⚠️ AC8: when the line carries a message id, the id IS its identity.
        # The body-only window dropped an owner's second identical line ("yes",
        # "yes") sent inside it — two messages, two mids. With a mid: the same
        # mid on the same rail is a retry (any age); the same body inside the
        # window is a duplicate only when that row has NO mid (the other rail of
        # the same reply). Without a mid the body window stands as before.
        d = "in" if direction == "in" else "out"
        if mid:
            dup = execute_retry(
                self.db_path,
                "SELECT 1 FROM owner_thread WHERE user_id=? AND direction=? AND ("
                "(via=? AND mid=?) OR (mid='' AND body=? AND ts>=?)) LIMIT 1",
                (uid, d, str(via or ""), str(mid), text, ts - _DEDUP_WINDOW_S),
                fetch="one")
        else:
            dup = execute_retry(
                self.db_path,
                "SELECT 1 FROM owner_thread WHERE user_id=? AND direction=? AND body=? "
                "AND ts>=? LIMIT 1",
                (uid, d, text, ts - _DEDUP_WINDOW_S),
                fetch="one")
        if dup is not None:
            return 0
        rowid = execute_retry(
            self.db_path,
            "INSERT INTO owner_thread (user_id, direction, ts, via, session_id, mid, "
            "reply_to_mid, source, rail_id, rail_label, ask_id, kind, body) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (uid, "in" if direction == "in" else "out", ts, str(via or ""),
             str(session_id or ""), str(mid or ""), str(reply_to_mid or ""),
             str(source or ""), str(rail_id or ""), str(rail_label or "")[:80],
             str(ask_id or ""), str(kind or ""), text),
            fetch="lastrowid")
        execute_retry(
            self.db_path,
            "DELETE FROM owner_thread WHERE user_id=? AND id NOT IN "
            "(SELECT id FROM owner_thread WHERE user_id=? ORDER BY ts DESC, id DESC LIMIT ?)",
            (uid, uid, _KEEP_ROWS))
        return int(rowid or 0)

    # --- read ---
    def tail(self, user_id: str, *, since_ts: float, limit: int) -> List[dict]:
        """Newest ``limit`` rows not older than ``since_ts``, oldest first."""
        rows = execute_retry(
            self.db_path,
            "SELECT * FROM owner_thread WHERE user_id=? AND ts>=? "
            "ORDER BY ts DESC, id DESC LIMIT ?",
            (str(user_id or ""), float(since_ts), max(1, int(limit))), fetch="all") or []
        return [dict(r) for r in reversed(rows)]

    def recent(self, user_id: str, *, limit: int) -> List[dict]:
        rows = execute_retry(
            self.db_path,
            "SELECT * FROM owner_thread WHERE user_id=? ORDER BY ts DESC, id DESC LIMIT ?",
            (str(user_id or ""), max(1, int(limit))), fetch="all") or []
        return [dict(r) for r in reversed(rows)]

    def rows_since(self, user_id: str, *, since_ts: float,
                   exclude_session_id: str = "", limit: int = 50) -> List[dict]:
        """Rows strictly after ``since_ts`` that another session produced, oldest first."""
        rows = execute_retry(
            self.db_path,
            "SELECT * FROM owner_thread WHERE user_id=? AND ts>? AND session_id!=? "
            "ORDER BY ts ASC, id ASC LIMIT ?",
            (str(user_id or ""), float(since_ts), str(exclude_session_id or ""),
             max(1, int(limit))), fetch="all") or []
        return [dict(r) for r in rows]

    def last_inbound_ts(self, user_id: str, session_id: str) -> Optional[float]:
        row = execute_retry(
            self.db_path,
            "SELECT MAX(ts) AS t FROM owner_thread WHERE user_id=? AND session_id=? "
            "AND direction='in'",
            (str(user_id or ""), str(session_id or "")), fetch="one")
        t = row["t"] if row is not None else None
        return float(t) if t is not None else None

    def by_mid(self, user_id: str, *, via: str, mid: str) -> Optional[dict]:
        if not mid:
            return None
        row = execute_retry(
            self.db_path,
            "SELECT * FROM owner_thread WHERE user_id=? AND via=? AND mid=? "
            "ORDER BY id DESC LIMIT 1",
            (str(user_id or ""), str(via or ""), str(mid)), fetch="one")
        return dict(row) if row is not None else None

    def rail_rows(self, user_id: str, *, rail_id: str, limit: int) -> List[dict]:
        """What THIS rail told the owner, plus any owner line that quote-replied
        to one of those messages. Oldest first."""
        if not rail_id:
            return []
        uid = str(user_id or "")
        outs = execute_retry(
            self.db_path,
            "SELECT * FROM owner_thread WHERE user_id=? AND rail_id=? AND direction='out' "
            "ORDER BY ts DESC, id DESC LIMIT ?",
            (uid, str(rail_id), max(1, int(limit))), fetch="all") or []
        outs = [dict(r) for r in outs]
        mids = [(r["via"], r["mid"]) for r in outs if r.get("mid")]
        ins: List[dict] = []
        for via, mid in mids:
            rows = execute_retry(
                self.db_path,
                "SELECT * FROM owner_thread WHERE user_id=? AND direction='in' "
                "AND via=? AND reply_to_mid=? ORDER BY ts ASC LIMIT 5",
                (uid, str(via), str(mid)), fetch="all") or []
            ins.extend(dict(r) for r in rows)
        merged = sorted(outs + ins, key=lambda r: (float(r["ts"]), int(r["id"])))
        return merged[-max(1, int(limit)):]

    def outbound_bodies_since(self, user_id: str, since_secs: float, *,
                              now: Optional[float] = None, limit: int = 50) -> List[str]:
        """Bodies the owner received within the window, newest first (the
        autonomous owner-resend cooldown reads this)."""
        ts = time.time() if now is None else now
        rows = execute_retry(
            self.db_path,
            "SELECT body FROM owner_thread WHERE user_id=? AND direction='out' AND ts>=? "
            "ORDER BY ts DESC LIMIT ?",
            (str(user_id or ""), ts - float(since_secs), max(1, int(limit))),
            fetch="all") or []
        return [str(r["body"] or "") for r in rows]

    def counts_since(self, user_id: str, since_secs: float, *,
                     now: Optional[float] = None) -> Dict[str, int]:
        ts = time.time() if now is None else now
        rows = execute_retry(
            self.db_path,
            "SELECT direction, COUNT(*) AS n FROM owner_thread WHERE user_id=? AND ts>=? "
            "GROUP BY direction",
            (str(user_id or ""), ts - float(since_secs)), fetch="all") or []
        out = {"out": 0, "in": 0}
        for r in rows:
            out[str(r["direction"])] = int(r["n"])
        return out

    # --- one-shot adoption of the legacy owner conversations ---
    def adopt_legacy_conversations(self, conv_store: Any, user_id: str,
                                   pairs: List[tuple]) -> int:
        """Move the owner's rows out of the correspondent conversation store.

        Before 061 the ``message`` tool's owner branch wrote the owner's lines
        into ``conversations.db`` under the raw target — prod held BOTH
        ``28436760`` and ``telegram:28436760``. Each row is copied here (via =
        the conversation's surface, source = ``message_tool``) and the legacy
        conversation is deleted, so ``/contacts`` stops listing the owner as a
        correspondent. Idempotent: an absent conversation moves nothing.
        Returns the number of rows moved. Fail-open per pair.
        """
        moved = 0
        for surface, address in pairs or []:
            try:
                conv = conv_store.get(user_id, surface, address)
                if conv is None:
                    continue
                rows = conv_store.history(user_id, surface, address, limit=_KEEP_ROWS)
                for m in rows:
                    self.record(
                        "in" if m.get("direction") == "in" else "out", user_id,
                        m.get("body") or "", via=str(surface), session_id=m.get("session_id") or "",
                        mid=m.get("mid") or "", source="message_tool", now=float(m.get("ts") or 0))
                    moved += 1
                execute_retry(conv_store.db_path,
                              "DELETE FROM conversation_messages WHERE conversation_id=?",
                              (int(conv["id"]),))
                execute_retry(conv_store.db_path,
                              "DELETE FROM conversations WHERE id=?", (int(conv["id"]),))
            except Exception:
                logger.warning("owner thread: legacy adoption skipped for %s:%s",
                               surface, address, exc_info=True)
        return moved


# --- resolution ----------------------------------------------------------------

def _data_dir(container: Any) -> Optional[str]:
    cfg_dir = getattr(getattr(container, "config", None), "data_dir", None)
    if not cfg_dir:
        return None
    try:
        from core.runtime_paths import data_dir_or_home
        return data_dir_or_home(cfg_dir)
    except Exception:
        return None


def resolve_store(container: Any, *, create: bool,
                  data_dir: Optional[str] = None) -> Optional[OwnerThreadStore]:
    """The registered store, else one opened on the data home.

    ``create=False`` (every READ) returns None when the file does not exist —
    an absent thread is "nothing recorded", never a freshly minted empty db.
    """
    try:
        svc = container.get_service(SERVICE_NAME) if container is not None else None
        if svc is not None:
            return svc
    except Exception:
        svc = None
    # A container that cannot name its data dir gets NO store: falling back to
    # the process home from a bare stand-in (every test double) would write the
    # developer's real home. CLI seats pass ``data_dir`` explicitly.
    base = data_dir or (_data_dir(container) if container is not None else None)
    if not base:
        return None
    path = os.path.join(str(base), DB_NAME)
    if not create and not os.path.isfile(path):
        return None
    try:
        store = OwnerThreadStore(path)
    except Exception as e:
        logger.debug("owner thread store unavailable at %s: %s", path, e)
        return None
    if container is not None and create:
        try:
            container.register_service(SERVICE_NAME, store)
        except Exception:
            pass
    return store


# --- the seam ------------------------------------------------------------------

def _attachment_suffix(attachments: Any) -> str:
    names: List[str] = []
    for e in attachments or []:
        try:
            p = e.get("path") if isinstance(e, dict) else getattr(e, "path", None)
            name = os.path.basename(str(p)) if p else ""
            if not name:
                name = str(e.get("name") if isinstance(e, dict) else getattr(e, "name", ""))
            if name:
                names.append(name)
        except Exception:
            continue
    return "".join(f" [attached: {n}]" for n in names[:5])


def record_owner_out(container: Any, user_id: str, text: str, *, via: str,
                     session_id: Optional[str], source: str, mid: Optional[str] = None,
                     rail_id: Optional[str] = None, rail_label: Optional[str] = None,
                     ask_id: Optional[str] = None, attachments: Any = None,
                     now: Optional[float] = None) -> bool:
    """Record a line the owner RECEIVED. Never raises; False = not recorded."""
    if not owner_thread_enabled():
        return False
    body = (text or "").strip() + _attachment_suffix(attachments)
    if not body.strip() or not str(user_id or "").strip():
        return False
    try:
        store = resolve_store(container, create=True)
        if store is None:
            return False
        store.record("out", user_id, body, via=via or "", session_id=session_id or "",
                     mid=mid or "", source=source or "", rail_id=rail_id or "",
                     rail_label=rail_label or "", ask_id=ask_id or "", now=now)
        return True
    except Exception:
        logger.debug("owner thread: out record skipped (fail-open)", exc_info=True)
        return False


def record_owner_in(container: Any, user_id: str, text: str, *, via: str,
                    session_id: Optional[str], mid: Optional[str] = None,
                    reply_to_mid: Optional[str] = None, kind: str = "comment",
                    now: Optional[float] = None) -> bool:
    """Record a line the owner SENT (owner tier only — the caller has resolved
    the tier; a correspondent or room line never comes here). Never raises."""
    if not owner_thread_enabled():
        return False
    body = (text or "").strip()
    if not body or not str(user_id or "").strip():
        return False
    try:
        store = resolve_store(container, create=True)
        if store is None:
            return False
        store.record("in", user_id, body, via=via or "", session_id=session_id or "",
                     mid=mid or "", reply_to_mid=reply_to_mid or "", kind=kind or "comment",
                     now=now)
        return True
    except Exception:
        logger.debug("owner thread: in record skipped (fail-open)", exc_info=True)
        return False


def thread_tail(container: Any, user_id: str, *, hours: Optional[float] = None,
                max_rows: Optional[int] = None, now: Optional[float] = None) -> List[dict]:
    try:
        store = resolve_store(container, create=False)
        if store is None:
            return []
        ts = time.time() if now is None else now
        return store.tail(user_id, since_ts=ts - (hours or tail_hours()) * 3600,
                          limit=max_rows or tail_rows())
    except Exception:
        logger.debug("owner thread: tail read failed (fail-open)", exc_info=True)
        return []


def thread_delta(container: Any, user_id: str, *, session_id: str, since_ts: float,
                 max_rows: int = 20) -> List[dict]:
    """Rows OTHER sessions produced after ``since_ts``."""
    try:
        store = resolve_store(container, create=False)
        if store is None:
            return []
        return store.rows_since(user_id, since_ts=since_ts,
                                exclude_session_id=session_id, limit=max_rows)
    except Exception:
        logger.debug("owner thread: delta read failed (fail-open)", exc_info=True)
        return []


def last_owner_turn_ts(container: Any, user_id: str, session_id: str) -> Optional[float]:
    """The store's own watermark for a session: its newest recorded owner line."""
    try:
        store = resolve_store(container, create=False)
        return store.last_inbound_ts(user_id, session_id) if store is not None else None
    except Exception:
        return None


def rail_slice(container: Any, user_id: str, *, rail_id: str,
               max_rows: int = 6) -> List[dict]:
    try:
        store = resolve_store(container, create=False)
        if store is None or not rail_id:
            return []
        return store.rail_rows(user_id, rail_id=rail_id, limit=max_rows)
    except Exception:
        logger.debug("owner thread: rail slice failed (fail-open)", exc_info=True)
        return []


def referent(container: Any, user_id: str, *, via: str, mid: str) -> Optional[dict]:
    try:
        store = resolve_store(container, create=False)
        if store is None or not mid:
            return None
        return store.by_mid(user_id, via=via, mid=str(mid))
    except Exception:
        logger.debug("owner thread: referent lookup failed (fail-open)", exc_info=True)
        return None


def recent_outbound_bodies(container: Any, user_id: str, since_secs: float, *,
                           now: Optional[float] = None) -> Optional[List[str]]:
    """What the owner received in the window, any rail — None when unreadable
    (the caller must not read None as "nothing was sent")."""
    try:
        store = resolve_store(container, create=False)
        if store is None:
            return None
        return store.outbound_bodies_since(user_id, since_secs, now=now)
    except Exception:
        return None


def thread_counts(container: Any, user_id: str, *, hours: float = 24.0,
                  data_dir: Optional[str] = None) -> Optional[Dict[str, int]]:
    """``{"out": n, "in": m}`` over the window, or None when there is no store."""
    try:
        store = resolve_store(container, create=False, data_dir=data_dir)
        if store is None:
            return None
        return store.counts_since(user_id, hours * 3600)
    except Exception:
        return None


def thread_recent(container: Any, user_id: str, *, limit: int = 20,
                  hours: Optional[float] = None,
                  data_dir: Optional[str] = None) -> Optional[List[dict]]:
    """The owner seat's read (``/thread``): newest-last. None = no store."""
    try:
        store = resolve_store(container, create=False, data_dir=data_dir)
        if store is None:
            return None
        if hours:
            return store.tail(user_id, since_ts=time.time() - hours * 3600, limit=limit)
        return store.recent(user_id, limit=limit)
    except Exception:
        logger.debug("owner thread: recent read failed", exc_info=True)
        return None


_ADOPTED: set = set()


def adopt_legacy_owner_rows(container: Any, user_id: str, owner_targets: Any) -> int:
    """Fold the pre-061 owner rows out of the correspondent store, once per
    process per tenant (the store-side move is idempotent anyway). Both prod
    spellings are tried: the bare address and ``<surface>:<address>``."""
    uid = str(user_id or "")
    if not uid or uid in _ADOPTED or not owner_targets:
        return 0
    _ADOPTED.add(uid)
    try:
        conv = container.get_service("conversation_store") if container is not None else None
        if conv is None:
            return 0
        store = resolve_store(container, create=True)
        if store is None:
            return 0
        pairs = []
        for surface, addr in dict(owner_targets).items():
            if addr:
                pairs.append((surface, str(addr)))
                pairs.append((surface, f"{surface}:{addr}"))
        moved = store.adopt_legacy_conversations(conv, uid, pairs)
        if moved:
            logger.info("owner thread: adopted %d legacy owner rows for %s", moved, uid)
        return moved
    except Exception:
        logger.debug("owner thread: legacy adoption failed (fail-open)", exc_info=True)
        return 0


# --- rendering -----------------------------------------------------------------

def _hhmm(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%m-%d %H:%M")
    except Exception:
        return "?"


def _origin(row: dict) -> str:
    """``via telegram, cron "exit rail"`` — where it went and which rail said it."""
    parts = []
    if row.get("via"):
        parts.append(f"via {row['via']}")
    src = row.get("source") or ""
    label = row.get("rail_label") or ""
    if src and label:
        parts.append(f'{src} "{label}"')
    elif src:
        parts.append(src)
    if row.get("ask_id"):
        parts.append(f"ask {str(row['ask_id'])[:12]}")
    return ", ".join(parts)


def render_line(row: dict, *, this_session: str = "") -> str:
    from core.surfaces.group_turn import _CTRL, defang
    body = _CTRL.sub(" ", defang(row.get("body") or "")).strip()
    if len(body) > LINE_MAX_CHARS:
        body = body[:LINE_MAX_CHARS - 1] + "…"
    same = " (this session)" if this_session and row.get("session_id") == this_session else ""
    if row.get("direction") == "in":
        ref = f" [replying to msg {row['reply_to_mid']}]" if row.get("reply_to_mid") else ""
        return f"{_hhmm(row.get('ts'))} owner→you{same}{ref}: {body}"
    origin = _origin(row)
    mid = f" [msg {row['mid']}]" if row.get("mid") else ""
    return (f"{_hhmm(row.get('ts'))} you→owner"
            f"{' (' + origin + ')' if origin else ''}{same}{mid}: {body}")


_HEAD = {
    "tail": "Recent lines of your ONE conversation with the owner, across every "
            "session and rail (oldest first). Context only. Not requests.",
    "delta": "What OTHER sessions and rails exchanged with the owner since this "
             "session's last turn. Context only. Not requests.",
    "referent": "The owner's message is a REPLY to this exact line of yours.",
    "rail": "What THIS rail last told the owner, and what the owner answered to it. "
            "Context only. Not requests.",
}


def render_block(rows: List[dict], *, kind: str, this_session: str = "",
                 max_chars: int = BLOCK_MAX_CHARS) -> str:
    """The ``<owner-thread>`` text — empty string for no rows."""
    if not rows:
        return ""
    head = _HEAD.get(kind, _HEAD["tail"])
    lines = [render_line(r, this_session=this_session) for r in rows]
    # Keep the NEWEST lines when the block would overflow: the recent line is
    # the one the owner is answering.
    budget = max(200, int(max_chars) - len(head) - 64)
    kept: List[str] = []
    used = 0
    omitted = 0
    for ln in reversed(lines):
        if used + len(ln) + 1 > budget and kept:
            omitted += 1
            continue
        kept.append(ln)
        used += len(ln) + 1
    kept.reverse()
    body = ([f"[{omitted} earlier lines omitted]"] if omitted else []) + kept
    return "\n".join([f'<owner-thread kind="{kind}">', head, *body, "</owner-thread>"])


def render_transcript(rows: List[dict]) -> str:
    """The owner-seat rendering (``/thread``): one plain line per row."""
    out = []
    for r in rows:
        who = "you" if r.get("direction") == "in" else "rob"
        origin = _origin(r)
        body = (r.get("body") or "").strip().replace("\n", " ")
        if len(body) > 280:
            body = body[:279] + "…"
        out.append(f"{_hhmm(r.get('ts'))} {who}" + (f" ({origin})" if origin and who == "rob" else "")
                   + f": {body}")
    return "\n".join(out)
