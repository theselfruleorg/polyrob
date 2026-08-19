"""Encrypted per-tenant custody of the agent's X login (Task 8).

Reuses the two proven primitives rather than inventing storage:
- :class:`tools.oauth.file_store.FileTokenStore` — atomic 0600 JSON file of
  opaque bytes keyed ``(user_id, provider)``;
- :class:`tools.mcp.security.MCPEncryption` (via ``get_encryption()``) — Fernet;
  the key comes from ``MCP_ENCRYPTION_KEY`` (REQUIRED in production).

Record shape (decrypted): ``{"storage_state": dict, "password": str|None,
"handle": str|None, "created_at": iso-str}``. ``save`` merge-updates so a
storage_state refresh never drops the stored password. Signup progress uses the
same store under provider ``"x_signup"`` (see tools/x_browser/signup.py).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SESSION_FILENAME = ".x_session.json"
PROVIDER = "x"


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
        """Merge-update the tenant's record; unspecified fields are kept."""
        record = self.load(user_id) or {
            "created_at": datetime.now(timezone.utc).isoformat()}
        if storage_state is not None:
            record["storage_state"] = storage_state
        if password is not None:
            record["password"] = password
        if handle is not None:
            record["handle"] = handle
        if extra:
            record.update(extra)
        self._store[(str(user_id), self._provider)] = self._enc().encrypt_dict(record)

    def load(self, user_id: str) -> Optional[dict]:
        blob = self._store.get((str(user_id), self._provider))
        if not blob:
            return None
        try:
            return self._enc().decrypt_dict(blob)
        except Exception as e:
            logger.warning("x session record for %s undecryptable (%s) — treating "
                           "as absent; re-capture the session", user_id, e)
            return None

    def exists(self, user_id: str) -> bool:
        return (str(user_id), self._provider) in self._store

    def delete(self, user_id: str) -> None:
        try:
            del self._store[(str(user_id), self._provider)]
        except KeyError:
            pass
