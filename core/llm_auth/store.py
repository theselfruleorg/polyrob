"""Durable LLM credential store — ``~/.polyrob/auth.json`` (proposal 024, L1).

File discipline copied from Hermes' auth store because it is correct (§3.1):

- created via ``os.open(O_CREAT|O_EXCL|O_WRONLY, 0o600)`` — never a
  world-readable window;
- cross-process advisory locking (``fcntl.flock``) with a bounded timeout —
  the REPL, ``polyrob telegram``, the cron ticker, and the webview share this
  file;
- atomic writes (temp file in the same dir + ``os.replace``) — a crash
  mid-write cannot corrupt it;
- health writes reconcile on ``last_status_at`` so a lost exhaustion marker
  cannot resurrect a spent credential (two racing writers: newest stamp wins).

Store shape (version 1)::

    {
      "version": 1,
      "active_provider": "anthropic-oauth" | null,
      "providers": {
        "<name>": {"auth_type": "...", "access_token": "...",
                    "refresh_token": "...", "expires_at": 0.0, "scope": "...",
                    "health": {"state": "ok", "last_status_at": 0.0,
                               "retry_after": null},
                    "connected_at": 0.0}
      },
      "borrowed": {"<name>": {"source": "codex_cli", "access_token": "...",
                               "imported_at": 0.0, "consented": true}}
    }

This module is pure core-tier: no ``modules``/``tools`` imports (5-tier
ratchet), no knowledge of ProviderSpec. Values never appear in logs or repr.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

from core.paths import polyrob_home

logger = logging.getLogger(__name__)

_LOCK_TIMEOUT_SEC = 15.0
_LOCK_POLL_SEC = 0.1

_EMPTY_STORE: Dict[str, Any] = {
    "version": 1,
    "active_provider": None,
    "providers": {},
    "borrowed": {},
}


def auth_store_path(env=None) -> Path:
    """Store path: ``POLYROB_AUTH_STORE`` override (test isolation) or
    ``<polyrob_home>/auth.json``."""
    env = os.environ if env is None else env
    override = env.get("POLYROB_AUTH_STORE")
    if override and str(override).strip():
        return Path(str(override).strip())
    return polyrob_home() / "auth.json"


class AuthStoreError(RuntimeError):
    pass


class AuthStore:
    """File-backed credential store. All mutation goes through ``_mutate`` so
    every writer holds the exclusive flock for its read-modify-write."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else auth_store_path()
        # The agent-tool denial for the store is the bare `auth.json` name glob
        # (core/security/secret_guard.py). A relocated store with a DIFFERENT
        # basename escapes it — warn loudly rather than silently degrade.
        if self.path.name != "auth.json":
            logger.warning(
                "auth store path %s does not end in auth.json — agent file-tool "
                "denial is name-based; keep the auth.json basename.", self.path
            )

    # -- file discipline ----------------------------------------------------

    def _ensure_file(self) -> None:
        if self.path.exists():
            # A store copied in with loose permissions is silently world-readable
            # forever otherwise (review M3) — tighten and say so.
            try:
                mode = os.stat(self.path).st_mode & 0o777
                if mode & 0o077:
                    os.chmod(self.path, 0o600)
                    logger.warning(
                        "auth store %s had mode %o — tightened to 600", self.path, mode
                    )
            except OSError:
                pass
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return  # another process won the race — fine
        try:
            os.write(fd, json.dumps(_EMPTY_STORE, indent=2).encode("utf-8"))
        finally:
            os.close(fd)

    def _flock(self, fh, timeout: float = _LOCK_TIMEOUT_SEC) -> None:
        import fcntl
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise AuthStoreError(
                        f"auth store lock timeout after {timeout:.0f}s ({self.path})"
                    )
                time.sleep(_LOCK_POLL_SEC)

    def _read_unlocked(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return dict(_EMPTY_STORE)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("auth store unreadable (%s) — treating as empty", exc)
            return dict(_EMPTY_STORE)
        if not isinstance(data, dict):
            return dict(_EMPTY_STORE)
        data.setdefault("version", 1)
        data.setdefault("active_provider", None)
        data.setdefault("providers", {})
        data.setdefault("borrowed", {})
        return data

    def _write_atomic(self, data: Dict[str, Any]) -> None:
        payload = json.dumps(data, indent=2).encode("utf-8")
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".auth.json.", suffix=".tmp"
        )
        try:
            try:
                os.fchmod(fd, 0o600)
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, str(self.path))
        except BaseException:
            # A failed write must not strand a temp file holding every token
            # (review I5) — the temp is 0600, but it would sit there unmanaged.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        # Best-effort directory fsync so the rename survives power loss (M4).
        try:
            dfd = os.open(str(self.path.parent), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass

    def _mutate(self, fn) -> Any:
        """Run ``fn(data) -> result`` under the exclusive lock; persist ``data``.

        flock+rename guard: the lock lives on the fd we opened, but a concurrent
        writer's ``os.replace`` swaps the path to a NEW inode — a lock acquired
        on the old (now-unlinked) inode serializes nothing. After acquiring the
        lock, verify the locked fd still IS the file at ``self.path``; if not,
        reopen and retry. Once the check passes, any other writer must lock the
        same live inode, so the read-modify-write below is genuinely serialized.
        """
        self._ensure_file()
        for _ in range(64):  # bounded: each miss re-contends the (15s-capped) lock
            fh = open(self.path, "r+", encoding="utf-8")
            try:
                self._flock(fh)
                try:
                    if os.fstat(fh.fileno()).st_ino != os.stat(self.path).st_ino:
                        continue  # lock landed on a replaced inode — retry
                except FileNotFoundError:
                    continue  # file swapped mid-open — retry
                data = self._read_unlocked()
                result = fn(data)
                self._write_atomic(data)
                return result
            finally:
                fh.close()
        raise AuthStoreError(f"auth store never stabilized under contention ({self.path})")

    # -- read API ------------------------------------------------------------

    def load(self) -> Dict[str, Any]:
        # No lock needed: writes are atomic (temp + os.replace), so a plain read
        # always sees a complete snapshot. A shared lock here would be decorative
        # anyway (review M1) — it would guard a different fd than the one read.
        return self._read_unlocked()

    def get_provider(self, name: str) -> Optional[Dict[str, Any]]:
        entry = self.load()["providers"].get(name)
        return dict(entry) if isinstance(entry, dict) else None

    def get_borrowed(self, name: str) -> Optional[Dict[str, Any]]:
        entry = self.load()["borrowed"].get(name)
        return dict(entry) if isinstance(entry, dict) else None

    def list_providers(self) -> Dict[str, Dict[str, Any]]:
        return dict(self.load()["providers"])

    @property
    def active_provider(self) -> Optional[str]:
        return self.load().get("active_provider")

    # -- write API -----------------------------------------------------------

    def set_provider(self, name: str, entry: Dict[str, Any]) -> None:
        now = time.time()

        def _set(data):
            record = dict(entry)
            record.setdefault("connected_at", now)
            record.setdefault(
                "health", {"state": "ok", "last_status_at": now, "retry_after": None}
            )
            data["providers"][name] = record

        self._mutate(_set)

    def remove_provider(self, name: str) -> bool:
        return bool(self._mutate(lambda d: d["providers"].pop(name, None) is not None))

    def set_active_provider(self, name: Optional[str]) -> None:
        self._mutate(lambda d: d.update(active_provider=name))

    def set_borrowed(self, name: str, entry: Dict[str, Any]) -> None:
        if not entry.get("consented"):
            raise AuthStoreError("borrowed credentials require explicit consent")
        self._mutate(lambda d: d["borrowed"].__setitem__(name, dict(entry)))

    def remove_borrowed(self, name: str) -> bool:
        return bool(self._mutate(lambda d: d["borrowed"].pop(name, None) is not None))

    def stamp_health(
        self,
        name: str,
        state: str,
        *,
        retry_after: Optional[float] = None,
        at: Optional[float] = None,
    ) -> bool:
        """Record credential health, reconciling on ``last_status_at``.

        Returns True if the stamp was applied, False if a NEWER stamp already
        exists (the Hermes race rule: a lost exhaustion marker must not
        resurrect a spent credential — the freshest observation wins).
        """
        # Review M2: an unknown state string would be written verbatim and then
        # mapped to "ok" by health.effective_state — a typo silently HEALING an
        # exhausted credential is the exact failure this layer exists to stop.
        if state not in ("ok", "rate_limited", "exhausted"):
            logger.warning("stamp_health: invalid state %r for %r — ignored", state, name)
            return False
        at = time.time() if at is None else at

        def _stamp(data):
            entry = data["providers"].get(name)
            if not isinstance(entry, dict):
                return False
            current = entry.get("health") or {}
            if float(current.get("last_status_at") or 0.0) > at:
                return False
            entry["health"] = {
                "state": state,
                "last_status_at": at,
                "retry_after": retry_after,
            }
            return True

        return bool(self._mutate(_stamp))


_default_store: Optional[AuthStore] = None


def get_auth_store() -> AuthStore:
    """Process-wide default store (path re-resolved when env changes it)."""
    global _default_store
    path = auth_store_path()
    if _default_store is None or _default_store.path != path:
        _default_store = AuthStore(path)
    return _default_store
