"""Durable owner-session token denylist (043 W5).

The owner session cookie (webview/owner_auth.py) and the wallet/SIWE cookie
(api/auth_endpoints.py) are stateless HS256 JWTs: once minted, a stolen copy is
valid until its ``exp`` regardless of ``/logout`` — deleting the browser cookie
only removes the copy the browser holds. W5 gives ``/logout`` real server-side
revocation: every token now carries a random ``jti`` claim, ``/logout`` writes
that ``jti`` here, and the auth middleware refuses any token whose ``jti`` is on
the list. Paired with the ≤24h cookie lifetime (OWNER_COOKIE_TTL_SECONDS), a
stolen-and-not-logged-out token survives at most a day; a logged-out one dies at
once.

Layering: this module is core-tier and imports NOTHING upward (only stdlib +
``core.sqlite_util`` / ``core.runtime_paths``). ``import jwt`` is a third-party
dependency, not a project import, so ``revoke_cookie_token`` may decode here.

Failure mode: authentication fails closed when revocation storage is unavailable.
Repair the store before resuming owner access; a storage outage must not revive
logged-out credentials. Session decoders require both expiry and revocation IDs.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

from core.runtime_paths import sidecar_db_path
from core.sqlite_util import execute_retry, wal_connect

logger = logging.getLogger("core.token_denylist")

# ONE source of truth for the owner/SIWE cookie lifetime. Both minters
# (webview/owner_auth.py, api/auth_endpoints.py) import this so they cannot
# drift — the W5 requirement is that both agree. 24h is the ceiling the brief
# names (≤24h); a shorter value is safe to lower to.
OWNER_COOKIE_TTL_SECONDS = 24 * 60 * 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS token_denylist (
    jti        TEXT PRIMARY KEY,
    revoked_at REAL NOT NULL,
    expires_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_td_expires ON token_denylist(expires_at);
"""


class RevocationUnavailable(RuntimeError):
    """Logout could not be persisted; callers must not report success."""


class TokenDenylist:
    """Durable jti denylist. Unavailable storage refuses authentication."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ready = False
        try:
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
            conn = wal_connect(db_path)
            try:
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()
            self._ready = True
        except Exception as e:
            # Keep the unavailable instance; reads refuse credentials.
            logger.error(f"token_denylist init failed ({db_path}): {e}")

    def revoke(self, jti: str, *, expires_at: float = 0.0) -> None:
        """Add a jti to the denylist. Idempotent; raise when durable revocation fails.

        ``expires_at`` is the token's own ``exp`` (epoch seconds); it lets
        ``prune`` drop the row once the token would be invalid anyway.
        """
        if not self._ready:
            raise RevocationUnavailable("session revocation storage unavailable")
        if not jti:
            raise ValueError("revocation requires a session identifier")
        try:
            execute_retry(
                self.db_path,
                "INSERT OR IGNORE INTO token_denylist (jti, revoked_at, expires_at) "
                "VALUES (?, ?, ?)",
                (str(jti), time.time(), float(expires_at or 0.0)),
            )
            self._prune()
        except Exception as e:
            # A failed revoke means /logout did NOT invalidate the token — the
            # operator must see it (the token still works until its exp).
            self._ready = False
            logger.error("token_denylist revoke failed: %s", e)
            raise RevocationUnavailable("session revocation could not be saved") from e

    def is_revoked(self, jti: Optional[str]) -> bool:
        """Refuse revoked tokens and tokens whose revocation status is unknown."""
        if not self._ready:
            return True
        if not jti:
            return False
        try:
            row = execute_retry(
                self.db_path,
                "SELECT 1 FROM token_denylist WHERE jti = ? LIMIT 1",
                (str(jti),),
                fetch="one",
            )
            return row is not None
        except Exception as e:
            logger.error("token_denylist read failed: %s", e)
            return True

    def _prune(self) -> None:
        """Drop rows whose token already expired (exp in the past, non-zero)."""
        try:
            execute_retry(
                self.db_path,
                "DELETE FROM token_denylist WHERE expires_at > 0 AND expires_at < ?",
                (time.time(),),
            )
        except Exception:
            pass


# --- process-wide singleton keyed by db path ------------------------------------
_INSTANCES: Dict[str, TokenDenylist] = {}


def get_token_denylist(db_path: Optional[str] = None) -> TokenDenylist:
    """Get/create the shared denylist. Default path is the db_manifest axis
    (``<data_home>/token_denylist.db``). ``TOKEN_DENYLIST_PATH`` overrides it —
    the seam the test suite uses to keep revocations out of the real data home.
    """
    if db_path is None:
        db_path = os.getenv("TOKEN_DENYLIST_PATH") or None
    if db_path is None:
        db_path = str(sidecar_db_path("token_denylist.db"))
    inst = _INSTANCES.get(db_path)
    if inst is None:
        inst = TokenDenylist(db_path)
        _INSTANCES[db_path] = inst
    return inst


def jti_is_revoked(claims: Optional[Dict[str, Any]]) -> bool:
    """The ONE revocation predicate every auth gate calls (webview + api).

    Returns True for a revoked jti or an unavailable store. Session decoders
    enforce expiry and a nonempty jti before calling this predicate.
    """
    if not claims:
        return False
    try:
        return get_token_denylist().is_revoked(claims.get("jti"))
    except Exception:
        logger.error("token_denylist lookup failed", exc_info=True)
        return True


def revoke_cookie_token(token: Optional[str]) -> None:
    """Decode an ``auth_token`` JWT (ignoring ``exp``) and revoke its ``jti``.

    Invalid credentials are a no-op because session decoders already refuse
    them. Valid credentials must be durably revoked or RevocationUnavailable
    propagates to the logout handler.
    """
    if not token:
        return
    try:
        secret = os.environ.get("JWT_SECRET_KEY")
        if not secret:
            return
        from core.security.session_tokens import decode_session_token
        claims = decode_session_token(token, secret, verify_exp=False)
    except Exception:
        return
    jti = claims.get("jti")
    if jti:
        try:
            exp = float(claims.get("exp") or 0.0)
        except (TypeError, ValueError):
            exp = 0.0
        get_token_denylist().revoke(str(jti), expires_at=exp)


__all__ = [
    "OWNER_COOKIE_TTL_SECONDS",
    "RevocationUnavailable",
    "TokenDenylist",
    "get_token_denylist",
    "jti_is_revoked",
    "revoke_cookie_token",
]
