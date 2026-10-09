"""Ingress access-control via owner-allowlist + DM pairing (polyrob Phase D3).

Ingress is gated by an owner allowlist plus DM pairing, in a
multi-tenant-shaped version:

- the **owner** (local single-user OR the bound owner principal) is always allowed;
- a **paired** user is allowed;
- an **unknown** user is denied and issued a one-time pairing code, which the
  operator approves out-of-band (``polyrob owner pair approve <code>``; list codes
  with ``polyrob owner pair pending``), at which point the user becomes paired;
- an **anonymous** (empty user_id) request is denied with no code (unidentifiable).

Gated ``POLYROB_REQUIRE_PAIRING`` (default OFF → ``evaluate_access`` allows everyone
→ byte-identical). The store mirrors the cron/goals WAL+retry SQLite pattern.
"""
from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass
from typing import List, Mapping, Optional, Tuple

from core.env import bool_from
from core.sqlite_util import execute_retry, wal_connect

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

    def request(self, user_id: str) -> Optional[str]:
        """Issue (or return the existing pending) pairing code for ``user_id``.

        Returns None if the user is already paired (no code needed) or anonymous.
        Stable for 15 minutes; expired codes are replaced on the next request.
        """
        if not user_id:
            return None
        row = execute_retry(
            self.db_path, "SELECT code, paired, created_at FROM paired_users WHERE user_id=?",
            (user_id,), fetch="one")
        if row and row["paired"]:
            return None
        if row and row["code"] and (row["created_at"] or 0) > time.time() - PAIRING_CODE_TTL_SEC:
            return row["code"]
        code = secrets.token_hex(8)  # 16 hex chars (64-bit entropy)
        execute_retry(
            self.db_path,
            "INSERT INTO paired_users (user_id, code, paired, created_at) VALUES (?,?,0,?) "
            "ON CONFLICT(user_id) DO UPDATE SET code=excluded.code, created_at=excluded.created_at",
            (user_id, code, time.time()))
        return code

    def approve(self, code: str) -> Optional[str]:
        """Mark the user holding ``code`` as paired; return their user_id or None."""
        if not code:
            return None
        row = execute_retry(
            self.db_path,
            "UPDATE paired_users SET paired=1, code=NULL "
            "WHERE code=? AND paired=0 AND created_at>? RETURNING user_id",
            (code, time.time() - PAIRING_CODE_TTL_SEC), fetch="one")
        return row["user_id"] if row else None

    def revoke(self, user_id: str) -> None:
        if user_id:
            execute_retry(self.db_path, "DELETE FROM paired_users WHERE user_id=?", (user_id,))

    def list_pending(self) -> List[Tuple[str, str]]:
        rows = execute_retry(
            self.db_path,
            "SELECT user_id, code FROM paired_users WHERE paired=0 AND created_at>? ORDER BY created_at",
            (time.time() - PAIRING_CODE_TTL_SEC,), fetch="all") or []
        return [(r["user_id"], r["code"]) for r in rows]


@dataclass
class AccessDecision:
    allowed: bool
    reason: str
    pairing_code: Optional[str] = None


def evaluate_access(user_id: Optional[str], *, store: PairingStore,
                    owner_principal: Optional[str] = None, local: bool = False,
                    required: Optional[bool] = None) -> AccessDecision:
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
    code = store.request(uid)
    return AccessDecision(False, "pairing required", pairing_code=code)


# Surfaces where the single-user local operator is trusted as owner. MUST mirror
# core.surfaces.access._LOCAL_OWNER_SURFACES — POLYROB_LOCAL must NEVER auto-own a
# forgeable network sender (telegram/email/whatsapp). Duplicated (not imported) to
# keep the core→core import surface minimal.
_LOCAL_OWNER_SURFACES = frozenset({"cli", "local", "repl"})


def guard_inbound(
    container, user_id: Optional[str], surface_id: Optional[str] = None
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
        from core.runtime_paths import data_dir_or_home
        data_dir = data_dir_or_home(getattr(cfg, "data_dir", None))
        store = PairingStore(os.path.join(data_dir, "pairing.db"))
        from core.instance import resolve_owner_principal
        local = (
            surface_id in _LOCAL_OWNER_SURFACES
            and bool_from(None, "POLYROB_LOCAL", False)
        )
        decision = evaluate_access(user_id, store=store,
                                   owner_principal=resolve_owner_principal(),
                                   local=local, required=True)
        return None if decision.allowed else decision
    except Exception:
        return AccessDecision(False, "pairing store unavailable")


__all__ = [
    "PairingStore",
    "AccessDecision",
    "pairing_required",
    "evaluate_access",
    "guard_inbound",
]
