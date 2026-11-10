"""Encrypted per-tenant custody of the agent's X login (Task 8).

Reuses the two proven primitives rather than inventing storage:
- :class:`tools.oauth.file_store.FileTokenStore` — atomic 0600 JSON file of
  opaque bytes keyed ``(user_id, provider)``;
- :class:`tools.mcp.security.MCPEncryption` (via ``get_encryption()``) — Fernet;
  the key comes from ``MCP_ENCRYPTION_KEY`` (REQUIRED in production).

Record shape (decrypted): ``{"storage_state": dict, "password": str|None,
"handle": str|None, "created_at": iso-str}``. ``save`` merge-updates so a
storage_state refresh never drops the stored password. Signup progress uses the
same store under provider ``"x_signup"`` (see polyrob_x/x_browser/signup.py).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SESSION_FILENAME = ".x_session.json"
PROVIDER = "x"


class XSessionUndecryptable(Exception):
    """A record exists but cannot be decrypted (the key changed).

    098 CL-11: "unreadable" is not "absent". ``load`` still answers None for a
    reader, but a WRITE refuses — a merge from an empty record dropped the
    stored ``storage_state``/``handle`` and let a second signup register a
    second X account."""


def _default_path() -> Path:
    from core.runtime_paths import resolve_data_home
    return resolve_data_home() / SESSION_FILENAME


class XSessionStore:
    """Fernet-encrypted ``(user_id, "x")`` login store."""

    def __init__(self, path: Optional[Path] = None, *, provider: str = PROVIDER) -> None:
        from tools.oauth.file_store import FileTokenStore
        self._provider = provider
        self._store = FileTokenStore(Path(path) if path is not None else _default_path())

    def _enc(self):
        from tools.mcp.security import get_encryption
        return get_encryption()

    def save(self, user_id: str, *, storage_state: Optional[dict] = None,
             password: Optional[str] = None, handle: Optional[str] = None,
             extra: Optional[dict] = None) -> None:
        """Merge-update the tenant's record; unspecified fields are kept.

        CL-11: the read-merge-write runs under a cross-process file lock and
        re-reads the FILE (not this instance's snapshot), so a server refreshing
        ``storage_state`` and a CLI signup saving its progress keep both fields.
        An undecryptable record raises :class:`XSessionUndecryptable`. A field
        set to None in ``extra`` is removed."""
        from polyrob.sdk import file_lock
        with file_lock(str(self._store._path)):
            self._store._data = self._store._load(strict=True)  # unreadable: raise, never replace
            record = self.load_checked(user_id) or {
                "created_at": datetime.now(timezone.utc).isoformat()}
            if storage_state is not None:
                record["storage_state"] = storage_state
            if password is not None:
                record["password"] = password
            if handle is not None:
                record["handle"] = handle
            for k, v in (extra or {}).items():
                if v is None:
                    record.pop(k, None)
                else:
                    record[k] = v
            self._store[(str(user_id), self._provider)] = self._enc().encrypt_dict(record)

    def load_checked(self, user_id: str) -> Optional[dict]:
        """The record, None when there is none, or raise
        :class:`XSessionUndecryptable` when it exists but cannot be read."""
        blob = self._store.get((str(user_id), self._provider))
        if not blob:
            return None
        try:
            return self._enc().decrypt_dict(blob)
        except Exception as e:
            raise XSessionUndecryptable(
                f"x session record for {user_id} cannot be decrypted ({e})") from e

    def load(self, user_id: str) -> Optional[dict]:
        try:
            return self.load_checked(user_id)
        except XSessionUndecryptable as e:
            logger.warning("%s — treating as absent for reads; re-capture the session", e)
            return None

    def exists(self, user_id: str) -> bool:
        return (str(user_id), self._provider) in self._store

    def delete(self, user_id: str) -> None:
        try:
            del self._store[(str(user_id), self._provider)]
        except KeyError:
            pass
