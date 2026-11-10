"""Ingress access-control via owner-allowlist + DM pairing (polyrob Phase D3).

Ingress is gated by an owner allowlist plus DM pairing, in a
multi-tenant-shaped version:

- the **owner** (local single-user OR the bound owner principal) is always allowed;
- a **paired** user is allowed;
- an **unknown** user is denied and issued a one-time pairing code, which the
  owner approves out-of-band (``/pair approve <surface> <code>`` on an owner seat, or
  ``polyrob owner pair approve <surface> <code>``; list with ``/pair``), at which
  point the user is paired;
- an **anonymous** (empty user_id) request is denied with no code (unidentifiable).

Gated ``POLYROB_REQUIRE_PAIRING`` (default OFF → ``evaluate_access`` allows everyone
→ byte-identical). The store mirrors the cron/goals WAL+retry SQLite pattern.
"""
from __future__ import annotations

import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import List, Mapping, Optional, Tuple

from core.env import bool_from
from core.sqlite_util import execute_retry, wal_connect

logger = logging.getLogger(__name__)

PAIRING_CODE_TTL_SEC = 15 * 60


def pairing_required(env: Optional[Mapping[str, str]] = None) -> bool:
    # Repo-SSOT falsey-set semantics (core.env) — was a private opt-in truth set.
    return bool_from(env, "POLYROB_REQUIRE_PAIRING", False)


class PairingStore:
    """SQLite-backed ``paired_users`` table (WAL + jittered write-retry)."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        conn = wal_connect(self.db_path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS paired_users ("
                "  user_id TEXT PRIMARY KEY,"
                "  code TEXT,"
                "  paired INTEGER NOT NULL DEFAULT 0,"
                "  created_at REAL"
                ")"
            )
            # 110: the surface and the platform id the request came
            # from, so an operator approves "<surface> <code>" and sees who it is;
            # told_at limits how often a stranger is sent the code.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(paired_users)")}
            for col, typ in (("surface", "TEXT"), ("raw_id", "TEXT"), ("told_at", "REAL")):
                if col not in cols:
                    try:
                        conn.execute(f"ALTER TABLE paired_users ADD COLUMN {col} {typ}")
                    except Exception as exc:  # another process added it first
                        if "duplicate column" not in str(exc).lower():
                            raise
            conn.commit()
        finally:
            conn.close()

    def is_paired(self, user_id: str) -> bool:
        if not user_id:
            return False
        row = execute_retry(
            self.db_path, "SELECT paired FROM paired_users WHERE user_id=?",
            (user_id,), fetch="one")
        return bool(row and row["paired"])

    def request(self, user_id: str, *, surface: Optional[str] = None,
                raw_id: Optional[str] = None) -> Optional[str]:
        """Issue (or return the existing pending) pairing code for ``user_id``.

        Returns None if the user is already paired (no code needed) or anonymous.
        Stable for 15 minutes; expired codes are replaced on the next request.
        """
        if not user_id:
            return None
        # A flood of strangers never grows the store without bound: at the cap the
        # expired requests are dropped first; a NEW sender gets no code (and stays
        # denied) only while MAX_PENDING codes are still live.
        if not self.is_pending(user_id) and self.pending_count() >= self.MAX_PENDING:
            self.purge_expired(older_than=PAIRING_CODE_TTL_SEC)
        if not self.is_pending(user_id) and self.pending_count() >= self.MAX_PENDING:
            logger.warning("pairing: %d requests are waiting; a new sender gets no code",
                           self.MAX_PENDING)
            return None
        # 098 CO-14: ONE live code per user. The upsert replaces a code only when
        # the user is unpaired and the stored code is missing or expired; two
        # concurrent requests then both return the code that was stored.
        now = time.time()
        code = secrets.token_hex(8)  # 16 hex chars (64-bit entropy)
        execute_retry(
            self.db_path,
            "INSERT INTO paired_users (user_id, code, paired, created_at, surface, raw_id) "
            "VALUES (?,?,0,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET code=excluded.code, created_at=excluded.created_at, "
            "told_at=NULL, "
            "surface=COALESCE(excluded.surface, paired_users.surface), "
            "raw_id=COALESCE(excluded.raw_id, paired_users.raw_id) "
            "WHERE paired_users.paired=0 AND (paired_users.code IS NULL "
            "OR COALESCE(paired_users.created_at, 0) <= ?)",
            (user_id, code, now, surface or None, (str(raw_id) if raw_id else None),
             now - PAIRING_CODE_TTL_SEC))
        row = execute_retry(
            self.db_path, "SELECT code, paired FROM paired_users WHERE user_id=?",
            (user_id,), fetch="one")
        if not row or row["paired"]:
            return None
        return row["code"]

    #: The most waiting (unpaired) rows the store keeps; see :meth:`request`.
    MAX_PENDING = 500

    def pending_count(self) -> int:
        row = execute_retry(self.db_path, "SELECT COUNT(*) AS n FROM paired_users WHERE paired=0",
                            (), fetch="one")
        return int(row["n"]) if row else 0

    def is_pending(self, user_id: str) -> bool:
        row = execute_retry(self.db_path,
                            "SELECT 1 FROM paired_users WHERE user_id=? AND paired=0",
                            (user_id,), fetch="one")
        return bool(row)

    def by_code(self, code: str) -> Optional[dict]:
        """The live request holding ``code`` (who the operator is about to approve)."""
        row = execute_retry(
            self.db_path, "SELECT user_id, surface, raw_id FROM paired_users "
            "WHERE code=? AND paired=0 AND created_at>?",
            (code, time.time() - PAIRING_CODE_TTL_SEC), fetch="one")
        return dict(row) if row else None

    #: Unpaired rows (expired codes) older than this are deleted by the GC tick.
    PURGE_AFTER_SEC = 7 * 86400

    def purge_expired(self, older_than: Optional[float] = None) -> int:
        """Delete UNPAIRED rows whose code was issued more than ``older_than``
        seconds ago (default 7 days). A paired user is never touched."""
        cutoff = time.time() - (self.PURGE_AFTER_SEC if older_than is None else older_than)
        return execute_retry(
            self.db_path,
            "DELETE FROM paired_users WHERE paired=0 AND COALESCE(created_at, 0) < ?",
            (cutoff,)) or 0

    def approve(self, code: str, surface: Optional[str] = None) -> Optional[str]:
        """Mark the user holding ``code`` as paired; return their user_id or None.

        With ``surface`` (``approve <surface> <code>``) the code must
        have come from THAT surface: a code read out on one chat app never approves a
        sender on another."""
        if not code:
            return None
        sql = ("UPDATE paired_users SET paired=1, code=NULL "
               "WHERE code=? AND paired=0 AND created_at>?")
        args: list = [code, time.time() - PAIRING_CODE_TTL_SEC]
        if surface:
            sql += " AND surface=?"
            args.append(str(surface).strip().lower())
        row = execute_retry(self.db_path, sql + " RETURNING user_id", tuple(args), fetch="one")
        return row["user_id"] if row else None

    #: A stranger is sent the code at most once per this many seconds (the code
    #: itself stays the same for its 15 minutes): a pairing request is not a chat.
    TELL_EVERY_SEC = 10 * 60

    def should_tell(self, user_id: str, *, now: Optional[float] = None) -> bool:
        """True (and stamps the time) when the pending code may be sent again."""
        if not user_id:
            return False
        ts = time.time() if now is None else float(now)
        n = execute_retry(
            self.db_path,
            "UPDATE paired_users SET told_at=? WHERE user_id=? AND paired=0 "
            "AND COALESCE(told_at, 0) <= ?",
            (ts, user_id, ts - self.TELL_EVERY_SEC)) or 0
        return n > 0

    def revoke(self, user_id: str, surface: Optional[str] = None) -> int:
        """Remove a paired (or pending) user: by tenant ``user_id``, or — with
        ``surface`` — by that surface's platform id (``revoke <surface> <id>``).
        Returns the number of rows removed."""
        if not user_id:
            return 0
        if surface:
            return execute_retry(
                self.db_path, "DELETE FROM paired_users WHERE surface=? AND (raw_id=? OR user_id=?)",
                (str(surface).strip().lower(), str(user_id), str(user_id))) or 0
        return execute_retry(self.db_path, "DELETE FROM paired_users WHERE user_id=?",
                             (user_id,)) or 0

    def list_pending(self) -> List[Tuple[str, str]]:
        rows = execute_retry(
            self.db_path,
            "SELECT user_id, code FROM paired_users WHERE paired=0 AND created_at>? ORDER BY created_at",
            (time.time() - PAIRING_CODE_TTL_SEC,), fetch="all") or []
        return [(r["user_id"], r["code"]) for r in rows]

    def list_all(self) -> List[dict]:
        """Every live request and every paired user, oldest first (``pair list``)."""
        rows = execute_retry(
            self.db_path,
            "SELECT user_id, code, paired, created_at, surface, raw_id FROM paired_users "
            "WHERE paired=1 OR created_at>? ORDER BY paired DESC, created_at",
            (time.time() - PAIRING_CODE_TTL_SEC,), fetch="all") or []
        return [dict(r) for r in rows]


_STORES: dict = {}


def store_at(path: str) -> "PairingStore":
    """One :class:`PairingStore` per file path (the schema check runs once, not per
    inbound message)."""
    st = _STORES.get(path)
    if st is None:
        st = _STORES[path] = PairingStore(path)
    return st


def store_for(data_dir: Optional[str] = None) -> "PairingStore":
    """THE pairing store of a data home (``<data_dir or home>/pairing.db``): the gate,
    the tier oracle, the GC, the CLI and the ``/pair`` verb all open this one."""
    from core.runtime_paths import store_db_path
    return store_at(store_db_path("pairing.db", data_dir))


def describe(row: dict) -> str:
    """One line for a pairing row: who (chat app + platform id) and its state."""
    who = f"{row.get('surface') or '?'}:{row.get('raw_id') or '?'}"
    if row.get("paired"):
        return f"paired   {who}  user={row['user_id']}"
    return f"waiting  {who}  user={row['user_id']}  code={row['code']}"


def status_line(env: Optional[Mapping[str, str]] = None) -> str:
    """DM pairing at a glance (``polyrob doctor``): off, or on with how many senders
    are paired and waiting. An unreadable store says so, never "nobody"; a missing
    store is not created."""
    if not pairing_required(env):
        return "pairing: off (POLYROB_REQUIRE_PAIRING)"
    try:
        from core.runtime_paths import store_db_path
        data_dir = (env or os.environ).get("POLYROB_DATA_DIR") or None
        if not os.path.exists(store_db_path("pairing.db", data_dir)):
            return "pairing: on — nobody paired or waiting yet (/pair)"
        rows = store_for(data_dir).list_all()
    except Exception as e:
        return f"pairing: on — store unreadable ({type(e).__name__}: {e})"
    paired = sum(1 for r in rows if r.get("paired"))
    return f"pairing: on — {paired} paired, {len(rows) - paired} waiting (/pair)"


def code_reply(surface: Optional[str], code: str) -> str:
    """What a stranger is told with their code (every surface says the same)."""
    return ("🔒 I'm not allowed to talk to you yet.\n"
            f"Pairing code: {code}\n"
            f"Ask my owner to send me: /pair approve {surface or '<surface>'} {code}")


PAIR_USAGE = ("Usage: /pair (list) · /pair approve <surface> <code> · "
              "/pair revoke <surface> <id>")


def pair_verb(args: List[str], *, store: "PairingStore") -> str:
    """The owner's ``/pair`` verb, one text for every owner seat (chat and terminal).

    No argument (or ``list``): who is paired and who waits. ``approve <surface>
    <code>``: pair the sender that code was issued to on that chat app. ``revoke
    <surface> <id>`` (or ``revoke <user_id>``): remove a paired or waiting sender.
    The caller has already passed the owner gate."""
    sub = (args[0].lower() if args else "list")
    rest = list(args[1:])
    if sub in ("list", "ls", "pending"):
        rows = store.list_all()
        if sub == "pending":
            rows = [r for r in rows if not r.get("paired")]
        state = ("on" if pairing_required() else
                 "OFF (POLYROB_REQUIRE_PAIRING=false: nobody is asked for a code, and a "
                 "paired sender has no access until pairing is on)")
        if not rows:
            return f"Pairing is {state}. Nobody is paired and nobody waits."
        lines = [f"Pairing is {state}."] + [describe(r) for r in rows]
        if any(not r.get("paired") for r in rows):
            lines.append("Approve only a code that a person you know sent you: "
                         "/pair approve <surface> <code>")
        return "\n".join(lines)
    if sub == "approve":
        if len(rest) == 1:
            # 110: the surface is what binds a code to the chat app it was read on; a
            # bare code would pair a stranger on any app who passed his code along.
            return ("Name the chat app the code came from: /pair approve <surface> "
                    f"{rest[0].strip()}. A code from one chat app never pairs a sender "
                    "on another.")
        if len(rest) != 2:
            return PAIR_USAGE
        surface, code = rest[0].strip().lower(), rest[1].strip()
        row = store.by_code(code) or {}
        uid = store.approve(code, surface=surface)
        if uid is None:
            return f"No waiting pairing request from {surface} with code {code}. See /pair."
        who = f"{row.get('surface') or '?'}:{row.get('raw_id') or '?'}"
        return (f"✅ Paired {who} (user={uid}). This sender may now command me in their "
                f"own private chat, with their own memory: never your wallet, money, "
                f"approvals, owner commands or memory. Undo: /pair revoke "
                f"{row.get('surface') or '<surface>'} {row.get('raw_id') or uid}")
    if sub == "revoke":
        if len(rest) == 2:
            n = store.revoke(rest[1].strip(), surface=rest[0].strip().lower())
        elif len(rest) == 1:
            n = store.revoke(rest[0].strip())
        else:
            return PAIR_USAGE
        if not n:
            return f"No paired or waiting sender matches {' '.join(rest)}. See /pair."
        return f"✅ Revoked {' '.join(rest)}."
    return PAIR_USAGE


@dataclass
class AccessDecision:
    allowed: bool
    reason: str
    pairing_code: Optional[str] = None
    #: True the first time this sender asks (113 W6 F-16): the owner is told once.
    new_request: bool = False


def request_notice(surface: Optional[str], raw_id: Optional[str]) -> str:
    """The ONE push to the owner when a stranger first asks to pair (113 W6
    F-16). It carries no code: the owner approves only a code the person he
    knows sends him (the ``/pair list`` warning)."""
    where = surface or "a chat app"
    who = f" ({raw_id})" if raw_id else ""
    return (f"🔒 A {where} sender{who} asked to talk to me and got a pairing code. "
            f"If you know them, ask them for the code, then send: "
            f"/pair approve {surface or '<surface>'} <code>. /pair lists every request.")


def evaluate_access(user_id: Optional[str], *, store: PairingStore,
                    owner_principal: Optional[str] = None, local: bool = False,
                    required: Optional[bool] = None, surface: Optional[str] = None,
                    raw_id: Optional[str] = None) -> AccessDecision:
    """Decide whether ``user_id`` may use this instance.

    ``required`` defaults to :func:`pairing_required`. When pairing is off, everyone
    is allowed (byte-identical). Owner/local always allowed; paired users allowed;
    an unknown user is denied and issued a pairing code; anonymous is denied.
    """
    if required is None:
        required = pairing_required()
    if not required:
        return AccessDecision(True, "pairing not required")

    from core.instance import is_owner
    uid = (str(user_id).strip() if user_id is not None else "")
    if is_owner(uid, owner_principal=owner_principal, local=local):
        return AccessDecision(True, "owner")
    if not uid:
        return AccessDecision(False, "anonymous")
    if store.is_paired(uid):
        return AccessDecision(True, "paired")
    was_waiting = store.is_pending(uid)
    code = store.request(uid, surface=surface, raw_id=raw_id)
    new_request = bool(code) and not was_waiting
    # The code is sent at most once per TELL_EVERY_SEC; between, the deny is silent.
    if code and not store.should_tell(uid):
        code = None
    return AccessDecision(False, "pairing required", pairing_code=code,
                          new_request=new_request)


def _push_request_notice(container, surface: Optional[str], raw_id: Optional[str]) -> None:
    """Tell the owner of a NEW pairing request, once, on the owner delivery rail
    (``core.surfaces.user_delivery.deliver_user_message``; the daily cap applies, so a
    flood of strangers cannot flood him). Fail-open: the request still waits in
    the Inbox and in ``/pair`` when the push cannot go out."""
    try:
        import asyncio
        asyncio.get_running_loop()
    except RuntimeError:
        logger.info("pairing: no running loop; the new request waits in /pair and the Inbox")
        return
    try:
        from core.async_bridge import spawn_retained
        from core.instance import resolve_owner_user_id
        from core.surfaces.user_delivery import deliver_user_message
        spawn_retained(deliver_user_message(container, str(resolve_owner_user_id() or ""),
                                            request_notice(surface, raw_id), source="pairing"))
    except Exception:
        logger.warning("pairing: the owner push for a new request failed", exc_info=True)


# Surfaces where the single-user local operator is trusted as owner. MUST mirror
# core.surfaces.access._LOCAL_OWNER_SURFACES — POLYROB_LOCAL must NEVER auto-own a
# forgeable network sender (telegram/email/whatsapp). Duplicated (not imported) to
# keep the core→core import surface minimal.
_LOCAL_OWNER_SURFACES = frozenset({"cli", "local", "repl"})


def guard_inbound(
    container, user_id: Optional[str], surface_id: Optional[str] = None,
    raw_id: Optional[str] = None,
) -> Optional[AccessDecision]:
    """Ingress gate for the surface dispatcher. Returns a DENIAL decision when the
    user may not proceed, or None when allowed (incl. when pairing is off).

    ``POLYROB_LOCAL`` is honored as local-owner ONLY for trusted local surfaces
    (``surface_id`` in :data:`_LOCAL_OWNER_SURFACES`). For a network surface — or
    when ``surface_id`` is unknown — local-owner is forced OFF so a forgeable
    telegram/email sender cannot bypass the pairing gate.

    Store errors deny access while pairing is enabled. Reads ``POLYROB_LOCAL`` directly
    to avoid a core→agents import (core boundary).
    """
    if not pairing_required():
        return None
    try:
        cfg = getattr(container, "config", None) if container else None
        store = store_for(getattr(cfg, "data_dir", None))
        from core.instance import resolve_owner_principal
        local = (
            surface_id in _LOCAL_OWNER_SURFACES
            and bool_from(None, "POLYROB_LOCAL", False)
        )
        decision = evaluate_access(user_id, store=store,
                                   owner_principal=resolve_owner_principal(),
                                   local=local, required=True,
                                   surface=surface_id, raw_id=raw_id)
        if decision.new_request:
            _push_request_notice(container, surface_id, raw_id)
        return None if decision.allowed else decision
    except Exception:
        return AccessDecision(False, "pairing store unavailable")


__all__ = [
    "PairingStore",
    "store_at",
    "store_for",
    "describe",
    "pair_verb",
    "status_line",
    "code_reply",
    "request_notice",
    "AccessDecision",
    "pairing_required",
    "evaluate_access",
    "guard_inbound",
]
