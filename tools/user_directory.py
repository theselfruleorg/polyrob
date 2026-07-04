"""Shared UserDirectory — surface-agnostic identity store (P2c, Singular Chat Interface).

ONE identity store behind POLYROB's many surfaces (CLI / WebView / Telegram). It maps a
raw platform id (a Telegram chat id, a CLI id, …) to a STABLE INTERNAL ``user_id``
and provides the reverse / email lookups that out-of-band delivery
(``cron/delivery.py``) needs — keyed on the internal id, sync.

Ported (reused, not forked) from the old Telegram-only bot
(../rob_dev_telegram_version):
- ``modules/database/user_profiles.py`` — the ``user_profiles`` table shape
  (``user_id TEXT PK`` + ``tg_user_id TEXT UNIQUE``), ``get_or_create_by_tg_id``,
  and the SHA256 ``generate_user_id`` derivation.
- ``utils/user_id_utils.py`` — ``UserIDResolver`` bidirectional tg<->internal mapping.

Key differences from the old bot (deliberate):
- The internal id is **deterministically derived** from ``(surface, raw_id)`` via
  SHA256, so it is stable WITHOUT a prior row (the old code minted a random UUID per
  row). A row is still upserted so the reverse lookup (internal -> chat id) works.
- Identity is **namespaced by surface** — the same raw "12345" on telegram vs cli
  resolves to different internal ids, so cross-surface ids never collide.
- Sync API (no async DB layer) — ``cron/delivery.py`` calls ``get_telegram_chat_id``
  / ``get_email`` without ``await``; SQLite + ``core/sqlite_util`` is the right shape.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Optional

from core.sqlite_util import execute_retry, wal_connect

logger = logging.getLogger(__name__)

# Length of the hex slice used for the internal id (mirrors the old 24-char hash).
_ID_HEX_LEN = 24


class UserDirectory:
    """Container-registerable identity store over a SQLite ``user_profiles`` table."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ensure_table()

    # --- schema --------------------------------------------------------------
    def _ensure_table(self) -> None:
        conn = wal_connect(self.db_path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id     TEXT PRIMARY KEY,
                    surface_id  TEXT,
                    tg_user_id  TEXT UNIQUE,
                    chat_id     TEXT,
                    email       TEXT,
                    profile     TEXT,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_profiles_tg "
                "ON user_profiles(tg_user_id)"
            )
            conn.commit()
        finally:
            conn.close()

    # --- id derivation -------------------------------------------------------
    @staticmethod
    def _derive_internal_id(surface_id: str, raw_id: str) -> str:
        """Deterministic, stable internal id from (surface, raw_id).

        SHA256 of ``surface_id:raw_id`` so the same raw id on two surfaces never
        collides and the id is reproducible without a prior DB row.
        """
        seed = f"{surface_id}:{str(raw_id).strip()}".encode("utf-8")
        digest = hashlib.sha256(seed).hexdigest()[:_ID_HEX_LEN]
        return f"u_{digest}"

    # --- public API (contracts) ---------------------------------------------
    def get_or_create_by_tg_id(self, tg_id: str, profile: Optional[dict] = None) -> str:
        """Return a STABLE internal ``user_id`` for a Telegram id (idempotent).

        Upserts a row mapping ``tg_id`` <-> internal id, persisting ``chat_id`` (==
        the tg id, for reverse lookup), an optional ``email`` and JSON profile blob.
        """
        tg_id = str(tg_id).strip()
        user_id = self._derive_internal_id("telegram", tg_id)
        self._upsert(
            user_id=user_id,
            surface_id="telegram",
            tg_user_id=tg_id,
            chat_id=tg_id,
            profile=profile,
        )
        return user_id

    def resolve_internal(self, raw_user_id: str, surface_id: str) -> str:
        """Map a raw platform id to a stable internal ``user_id`` (idempotent).

        ``surface_id == "telegram"`` is exactly ``get_or_create_by_tg_id``. Other
        surfaces derive/persist the same way, namespaced by ``surface_id`` so the
        same raw id on different surfaces resolves to different internal ids.
        """
        raw_user_id = str(raw_user_id).strip()
        if surface_id == "telegram":
            return self.get_or_create_by_tg_id(raw_user_id)
        user_id = self._derive_internal_id(surface_id, raw_user_id)
        self._upsert(
            user_id=user_id,
            surface_id=surface_id,
            tg_user_id=None,
            chat_id=raw_user_id,
            profile=None,
        )
        return user_id

    def get_telegram_chat_id(self, user_id: str) -> Optional[str]:
        """Reverse lookup: internal ``user_id`` -> telegram chat id (the tg id).

        Exact name/shape consumed by ``cron/delivery.py::_owner_telegram``.
        """
        row = execute_retry(
            self.db_path,
            "SELECT tg_user_id, chat_id, surface_id FROM user_profiles WHERE user_id = ?",
            (user_id,),
            fetch="one",
        )
        if not row:
            return None
        # Only telegram-origin rows have a meaningful tg chat id.
        if row["surface_id"] != "telegram":
            return None
        return row["tg_user_id"] or row["chat_id"]

    def get_email(self, user_id: str) -> Optional[str]:
        """Read the optional email for an internal ``user_id`` (None if unset)."""
        row = execute_retry(
            self.db_path,
            "SELECT email FROM user_profiles WHERE user_id = ?",
            (user_id,),
            fetch="one",
        )
        if not row:
            return None
        return row["email"]

    # --- internals -----------------------------------------------------------
    def _upsert(
        self,
        *,
        user_id: str,
        surface_id: str,
        tg_user_id: Optional[str],
        chat_id: Optional[str],
        profile: Optional[dict],
    ) -> None:
        email = profile.get("email") if profile else None
        profile_json = json.dumps(profile) if profile else None
        # COALESCE preserves previously-set fields when a later call passes None.
        execute_retry(
            self.db_path,
            """
            INSERT INTO user_profiles (user_id, surface_id, tg_user_id, chat_id, email, profile)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                surface_id = excluded.surface_id,
                tg_user_id = COALESCE(excluded.tg_user_id, user_profiles.tg_user_id),
                chat_id    = COALESCE(excluded.chat_id, user_profiles.chat_id),
                email      = COALESCE(excluded.email, user_profiles.email),
                profile    = COALESCE(excluded.profile, user_profiles.profile),
                updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, surface_id, tg_user_id, chat_id, email, profile_json),
        )
