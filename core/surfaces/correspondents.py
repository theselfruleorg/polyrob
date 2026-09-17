"""WS-A correspondent registry — the SOLE routing authority for third-party replies.

A "correspondent" is a third party the agent INITIATED contact with (the agent
emailed john@acme; john replies). Their reply is DATA, never an instruction, and it
may enter ONLY the session that contacted them. This registry maps
``(surface, address[, thread_id]) -> session_id`` and is the only authority that
decides whether an inbound from a non-owner is routable.

Security model (Fusion-validated):
- **Tier = authenticated sender; thread = delivery target.** Resolution keys on the
  sender ADDRESS, so an unknown sender forging ``In-Reply-To`` of an existing thread
  resolves to nothing (thread-hijack defense). ``thread_id`` only disambiguates among
  one verified sender's own sessions; it never elevates an unseen address.
- **No self-bootstrapped trust.** A seed whose causing outbound was downstream of
  untrusted content (``provenance != "owner"``) is ALWAYS ``pending`` and never
  routable until an owner approves it — even when ``require_approval`` is False.
- **Approval + TTL + per-tenant cap** are first-class so the caller can enforce the
  auto-seed guardrails.

Pure storage + policy; no surface/transport imports. WAL + jittered retry via
``core/sqlite_util`` (mirrors the session-chat registry).
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional

from core.sqlite_util import execute_retry, wal_connect

logger = logging.getLogger(__name__)

STATE_PENDING = "pending"
STATE_ACTIVE = "active"
STATE_EXPIRED = "expired"


def _norm_addr(address: str) -> str:
    """The ONE key for a counterparty address — ``core.surfaces.address_key.
    canonical_addr`` (case-folded, a leading ``@`` and a ``t.me/`` wrapper
    folded away), the same rule the conversation store keys on, so "have we
    spoken" and "may this address reach a session" never disagree about who
    someone is. Rows written under the older lowercase-only rule are collapsed
    once per file on open (:meth:`CorrespondentRegistry._merge_legacy_spellings`)."""
    from core.surfaces.address_key import canonical_addr
    return canonical_addr(address)


class CorrespondentRegistry:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        conn = wal_connect(db_path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS correspondents (
                    surface     TEXT NOT NULL,
                    address     TEXT NOT NULL,
                    thread_id   TEXT NOT NULL DEFAULT '',
                    session_id  TEXT NOT NULL,
                    user_id     TEXT NOT NULL,
                    state       TEXT NOT NULL DEFAULT 'pending',
                    provenance  TEXT NOT NULL DEFAULT 'owner',
                    created_at  REAL NOT NULL,
                    updated_at  REAL NOT NULL,
                    PRIMARY KEY (surface, address, thread_id, user_id)
                )
                """
            )
            conn.commit()
            self._merge_legacy_spellings(conn)
        finally:
            conn.close()

    #: Bumped when an on-open migration lands, so it runs once per file.
    _SCHEMA_VERSION = 1

    @staticmethod
    def _merge_legacy_spellings(conn) -> None:
        """Collapse rows keyed under the older lowercase-only rule.

        Mirrors ``ConversationStore._merge_legacy_spellings``: a live install
        may hold ``@handle`` and ``handle`` as two bindings. Per canonical key
        ONE row survives — an ``active`` binding beats a pending one (the owner
        granted it), then the most recently updated (the registry's own
        "latest binding wins" resolution) — and its address is rewritten to the
        canonical spelling. Runs once per file (``PRAGMA user_version``),
        idempotent, fail-open: a migration fault must not make the store
        unusable.
        """
        try:
            if int(conn.execute("PRAGMA user_version").fetchone()[0]) >= \
                    CorrespondentRegistry._SCHEMA_VERSION:
                return
            rows = conn.execute(
                "SELECT surface, address, thread_id, user_id, state, updated_at "
                "FROM correspondents").fetchall()
            best: dict = {}
            for surface, address, tid, uid, state, updated in rows:
                key = (surface, _norm_addr(address), tid, uid)
                rank = (1 if state == STATE_ACTIVE else 0, float(updated or 0))
                if key not in best or rank > best[key][0]:
                    best[key] = (rank, address)
            # Two passes: drop every loser, then rename each winner to canonical.
            losers = [(s, a, t, u) for (s, a, t, u, _st, _up) in rows
                      if best[(s, _norm_addr(a), t, u)][1] != a]
            for s, a, t, u in losers:
                conn.execute("DELETE FROM correspondents WHERE surface=? AND address=? "
                             "AND thread_id=? AND user_id=?", (s, a, t, u))
            for (surface, canon, tid, uid), (_rank, winner) in best.items():
                if winner != canon:
                    conn.execute("UPDATE correspondents SET address=? WHERE surface=? "
                                 "AND address=? AND thread_id=? AND user_id=?",
                                 (canon, surface, winner, tid, uid))
            conn.execute(f"PRAGMA user_version = {CorrespondentRegistry._SCHEMA_VERSION}")
            conn.commit()
        except Exception:
            logger.warning("correspondent address merge skipped", exc_info=True)
            try:
                conn.rollback()
            except Exception:
                pass

    # --- write -------------------------------------------------------------
    def seed(
        self,
        *,
        surface: str,
        address: str,
        session_id: str,
        user_id: str,
        thread_id: Optional[str] = None,
        provenance: str = "owner",
        require_approval: bool = True,
        now: Optional[float] = None,
    ) -> str:
        """Register a correspondent the agent contacted. Returns the resulting state.

        - ``provenance != "owner"`` -> always ``pending`` (no self-granted trust).
        - ``provenance == "owner"`` and ``require_approval`` -> ``pending``.
        - ``provenance == "owner"`` and not ``require_approval`` -> ``active``.

        Idempotent on ``(surface, normalized-address, thread_id)``: re-seeding does not
        downgrade an already-active binding nor inflate the per-tenant seed count.
        """
        ts = time.time() if now is None else now
        addr = _norm_addr(address)
        tid = thread_id or ""
        if provenance != "owner":
            state = STATE_PENDING
        else:
            state = STATE_PENDING if require_approval else STATE_ACTIVE

        existing = execute_retry(
            self.db_path,
            "SELECT state FROM correspondents "
            "WHERE surface=? AND address=? AND thread_id=? AND user_id=?",
            (surface, addr, tid, user_id),
            fetch="one",
        )
        if existing is not None:
            # idempotent: keep the existing row (never silently downgrade an active one)
            return existing["state"]

        execute_retry(
            self.db_path,
            """INSERT INTO correspondents
                 (surface, address, thread_id, session_id, user_id, state, provenance,
                  created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (surface, addr, tid, session_id, user_id, state, provenance, ts, ts),
        )
        return state

    def approve(
        self,
        *,
        surface: str,
        address: str,
        thread_id: Optional[str] = None,
        user_id: Optional[str] = None,
        now: Optional[float] = None,
    ) -> bool:
        """Promote a pending binding to active. Returns True if a row was promoted.

        Pass ``user_id`` to scope the promotion to one tenant. Without it
        (single-owner admin default) the promotion is still SAFE: if the matching
        pending rows span more than one tenant it is REFUSED (returns False) rather
        than cross-promoting every tenant's row — the caller must disambiguate with
        ``user_id``. Previously an unscoped call promoted any tenant's matching
        pending row, a cross-tenant issue when two tenants shared a
        (surface, address, thread_id). (P1 finalization.)
        """
        ts = time.time() if now is None else now
        addr = _norm_addr(address)
        tid = thread_id or ""
        if user_id is None:
            # Safety guard: never let an unscoped promotion cross tenants.
            distinct = execute_retry(
                self.db_path,
                "SELECT DISTINCT user_id FROM correspondents "
                "WHERE surface=? AND address=? AND thread_id=? AND state=?",
                (surface, addr, tid, STATE_PENDING),
                fetch="all",
            ) or []
            if len(distinct) > 1:
                logger.warning(
                    "correspondents.approve refused: %s:%s (thread %r) has pending rows "
                    "for %d tenants; pass user_id to disambiguate",
                    surface, addr, tid, len(distinct),
                )
                return False
        sql = ("UPDATE correspondents SET state=?, updated_at=? "
               "WHERE surface=? AND address=? AND thread_id=? AND state=?")
        params = [STATE_ACTIVE, ts, surface, addr, tid, STATE_PENDING]
        if user_id is not None:
            sql += " AND user_id=?"
            params.append(user_id)
        n = execute_retry(self.db_path, sql, tuple(params))
        return bool(n)

    def reject(
        self,
        *,
        surface: str,
        address: str,
        thread_id: Optional[str] = None,
        user_id: Optional[str] = None,
        now: Optional[float] = None,
    ) -> bool:
        """Owner decision: reject a PENDING binding (pending -> expired).

        The counterpart to :meth:`approve` (030 WS-C C3 — before this the only
        way to clear a pending contact was to approve it or wait out the TTL).
        Same key shape and the same cross-tenant safety guard as ``approve``:
        an unscoped call that would span more than one tenant is REFUSED.

        Only a *pending* row flips — an ACTIVE binding is never demoted here
        (revoking an active correspondent is a separate, deliberate act). The
        rejected row becomes an EXPIRED tombstone (the same terminal state the
        TTL sweep produces): ``resolve`` never routes it, the pending listing
        drops it, and the idempotent :meth:`seed` cannot silently re-open the
        same contact as a fresh pending row. Returns True if a row flipped.
        """
        ts = time.time() if now is None else now
        addr = _norm_addr(address)
        tid = thread_id or ""
        if user_id is None:
            # Safety guard: never let an unscoped rejection cross tenants.
            distinct = execute_retry(
                self.db_path,
                "SELECT DISTINCT user_id FROM correspondents "
                "WHERE surface=? AND address=? AND thread_id=? AND state=?",
                (surface, addr, tid, STATE_PENDING),
                fetch="all",
            ) or []
            if len(distinct) > 1:
                logger.warning(
                    "correspondents.reject refused: %s:%s (thread %r) has pending rows "
                    "for %d tenants; pass user_id to disambiguate",
                    surface, addr, tid, len(distinct),
                )
                return False
        sql = ("UPDATE correspondents SET state=?, updated_at=? "
               "WHERE surface=? AND address=? AND thread_id=? AND state=?")
        params = [STATE_EXPIRED, ts, surface, addr, tid, STATE_PENDING]
        if user_id is not None:
            sql += " AND user_id=?"
            params.append(user_id)
        n = execute_retry(self.db_path, sql, tuple(params))
        return bool(n)

    def deactivate(
        self,
        *,
        surface: str,
        address: str,
        thread_id: Optional[str] = None,
        user_id: Optional[str] = None,
        now: Optional[float] = None,
    ) -> bool:
        """Revoke an ACTIVE binding (active -> expired tombstone) — the deliberate
        counterpart :meth:`reject` leaves alone. 031 T15: used by the boot sweep
        that removes a binding to the agent's OWN address. Same cross-tenant
        guard as ``reject``: an unscoped call spanning tenants is refused.
        Returns True if a row flipped."""
        ts = time.time() if now is None else now
        addr = _norm_addr(address)
        tid = thread_id or ""
        if user_id is None:
            distinct = execute_retry(
                self.db_path,
                "SELECT DISTINCT user_id FROM correspondents "
                "WHERE surface=? AND address=? AND thread_id=? AND state=?",
                (surface, addr, tid, STATE_ACTIVE),
                fetch="all",
            ) or []
            if len(distinct) > 1:
                logger.warning(
                    "correspondents.deactivate refused: %s:%s (thread %r) is active for %d "
                    "tenants; pass user_id to disambiguate", surface, addr, tid, len(distinct))
                return False
        sql = ("UPDATE correspondents SET state=?, updated_at=? "
               "WHERE surface=? AND address=? AND thread_id=? AND state=?")
        params = [STATE_EXPIRED, ts, surface, addr, tid, STATE_ACTIVE]
        if user_id is not None:
            sql += " AND user_id=?"
            params.append(user_id)
        n = execute_retry(self.db_path, sql, tuple(params))
        return bool(n)

    def seed_thread_anchor(
        self,
        *,
        surface: str,
        address: str,
        thread_id: str,
        session_id: str,
        user_id: str,
        now: Optional[float] = None,
    ) -> Optional[str]:
        """Bind an outbound message-id to its SENDING session (A3, 2026-07-13).

        A reply carrying ``In-Reply-To == thread_id`` then exact-matches in
        :meth:`resolve`, disambiguating multiple sessions talking to one address.
        Structural rows: ``provenance='thread'``, EXCLUDED from the per-day
        NEW-correspondent cap, and the state MIRRORS the base (address-level)
        row — no base row means no anchor (an anchor is never a trust bootstrap).
        Returns the anchor's state, or None when skipped.
        """
        if not thread_id:
            return None
        ts = time.time() if now is None else now
        addr = _norm_addr(address)
        base = execute_retry(
            self.db_path,
            "SELECT state FROM correspondents "
            "WHERE surface=? AND address=? AND user_id=? AND provenance != 'thread' "
            "ORDER BY updated_at DESC LIMIT 1",
            (surface, addr, user_id),
            fetch="one",
        )
        if base is None or base["state"] == STATE_EXPIRED:
            return None
        state = base["state"]
        existing = execute_retry(
            self.db_path,
            "SELECT state FROM correspondents "
            "WHERE surface=? AND address=? AND thread_id=? AND user_id=?",
            (surface, addr, thread_id, user_id),
            fetch="one",
        )
        if existing is not None:
            return existing["state"]
        execute_retry(
            self.db_path,
            """INSERT INTO correspondents
                 (surface, address, thread_id, session_id, user_id, state, provenance,
                  created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'thread', ?, ?)""",
            (surface, addr, thread_id, session_id, user_id, state, ts, ts),
        )
        return state

    def rebind_session(
        self,
        *,
        surface: str,
        address: str,
        user_id: str,
        new_session_id: str,
        now: Optional[float] = None,
    ) -> int:
        """E6: re-point every binding for (surface, address, tenant) to a replacement
        session (the original died). Returns the number of rows updated."""
        ts = time.time() if now is None else now
        return int(
            execute_retry(
                self.db_path,
                "UPDATE correspondents SET session_id=?, updated_at=? "
                "WHERE surface=? AND address=? AND user_id=?",
                (new_session_id, ts, surface, _norm_addr(address), user_id),
            )
        )

    def purge_expired(self, ttl_secs: float, *, now: Optional[float] = None) -> int:
        """Mark bindings idle longer than ``ttl_secs`` as expired. Returns the count."""
        ts = time.time() if now is None else now
        cutoff = ts - ttl_secs
        return int(
            execute_retry(
                self.db_path,
                """UPDATE correspondents SET state=?, updated_at=?
                   WHERE state!=? AND updated_at < ?""",
                (STATE_EXPIRED, ts, STATE_EXPIRED, cutoff),
            )
        )

    # --- read --------------------------------------------------------------
    def exists(
        self,
        *,
        surface: str,
        address: str,
        user_id: str,
        thread_id: Optional[str] = None,
    ) -> bool:
        """True if a binding row exists for this exact key, in ANY state."""
        addr = _norm_addr(address)
        tid = thread_id or ""
        row = execute_retry(
            self.db_path,
            "SELECT 1 FROM correspondents "
            "WHERE surface=? AND address=? AND thread_id=? AND user_id=?",
            (surface, addr, tid, user_id),
            fetch="one",
        )
        return row is not None

    def resolve(
        self,
        *,
        surface: str,
        address: str,
        thread_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Resolve an inbound to its originating session — or None if not routable.

        Keys on the sender ADDRESS (an unknown address never resolves, defeating
        thread-hijack). With ``thread_id``: an exact ``(address, thread)`` active
        binding wins. Without it (or no exact match): resolves only if the address has
        EXACTLY ONE active binding (else ambiguous -> None -> quarantine).
        """
        addr = _norm_addr(address)
        if thread_id:
            row = execute_retry(
                self.db_path,
                """SELECT * FROM correspondents
                   WHERE surface=? AND address=? AND thread_id=? AND state=?""",
                (surface, addr, thread_id, STATE_ACTIVE),
                fetch="one",
            )
            if row is not None:
                return dict(row)
        rows: List = execute_retry(
            self.db_path,
            "SELECT * FROM correspondents WHERE surface=? AND address=? AND state=?",
            (surface, addr, STATE_ACTIVE),
            fetch="all",
        )
        if not rows:
            return None
        if len(rows) == 1:
            return dict(rows[0])
        # A4 (2026-07-13 review): >1 active bindings. CROSS-tenant ambiguity stays
        # unroutable — guessing the tenant would leak a reply into the wrong
        # account. SAME-tenant: route to the most recently active binding (default
        # ON, CORRESPONDENT_RESOLVE_LATEST=false restores the legacy deny) — the
        # old None starved every session after the first, silently.
        tenants = {r["user_id"] for r in rows}
        if len(tenants) > 1:
            return None
        try:
            from core.surfaces.config import SurfaceConfig
            latest_ok = SurfaceConfig.correspondent_resolve_latest()
        except Exception:
            latest_ok = True
        if not latest_ok:
            return None
        best = max(rows, key=lambda r: r["updated_at"])
        logger.info(
            "correspondent resolve: %s:%s has %d active bindings (one tenant) — "
            "routing to most recent session %s", surface, addr, len(rows),
            best["session_id"])
        return dict(best)

    def list(self, user_id: Optional[str] = None) -> List[dict]:
        """List correspondent bindings (all, or tenant-scoped) for owner review."""
        if user_id is not None:
            rows = execute_retry(
                self.db_path,
                "SELECT * FROM correspondents WHERE user_id=? ORDER BY created_at DESC",
                (user_id,), fetch="all")
        else:
            rows = execute_retry(
                self.db_path,
                "SELECT * FROM correspondents ORDER BY created_at DESC", fetch="all")
        return [dict(r) for r in (rows or [])]

    def count_seeds_since(
        self,
        *,
        user_id: str,
        since_secs: float,
        now: Optional[float] = None,
    ) -> int:
        """Count this tenant's correspondents created within ``since_secs`` (cap input).

        Thread-anchor rows (``provenance='thread'``) are structural — one per
        outbound message to an ALREADY-seeded address — and never count toward
        the NEW-correspondent cap."""
        ts = time.time() if now is None else now
        cutoff = ts - since_secs
        row = execute_retry(
            self.db_path,
            "SELECT COUNT(*) AS n FROM correspondents "
            "WHERE user_id=? AND created_at >= ? AND provenance != 'thread'",
            (user_id, cutoff),
            fetch="one",
        )
        return int(row["n"]) if row is not None else 0
