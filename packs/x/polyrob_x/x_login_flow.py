"""One-tap X OAuth 2.0 re-login from an owner seat (option A of the X evaluation).

The owner types ``/x login`` on Telegram (or the REPL, or the console). This
module mints a PKCE verifier and a random ``state``, and answers with X's
authorize URL. The owner taps it and approves on x.com; X redirects the browser
to the console's public callback (``GET /api/packs/x/oauth/callback``), which
calls :func:`complete_login`: the ``state`` is looked up, deleted (single use)
and checked for expiry, and the code is exchanged through
``polyrob_x.x_oauth2.exchange_code`` — which stores the pair in the ONE token
store. No SSH, no tunnel, no browser on the box, no cookie handled by us.

**Two processes, one record.** The verb runs in the agent process (``polyrob
telegram``); the callback lands in the console process (``webview/server.py``).
They share the pending record through the SAME encrypted file the token lives
in (``<data home>/.x_session.json``, ``FileTokenStore`` + Fernet via
``MCP_ENCRYPTION_KEY``; group ``polyrob-data`` 0660 on a deployed box), under
provider :data:`PENDING_PROVIDER`, keyed by a hash of the state (the state
itself is never stored in the clear as a key).

**The proof.** The ``state`` is 256 bits of randomness, single use, valid for
:data:`PENDING_TTL_SEC`, and bound to the owner who minted it (the notice goes
to that owner; a caller that knows who is asking passes ``expected_owner``).
The PKCE verifier never leaves the server, so a stolen ``code`` alone is
worthless. Nothing here logs or returns a token, a verifier or a state.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: The env flag naming the console callback X redirects to (docs/CONFIGURATION.md).
REDIRECT_FLAG = "X_OAUTH2_REDIRECT_URI"
#: The console path the callback route serves (``polyrob_x.console_routes``).
CALLBACK_PATH = "/api/packs/x/oauth/callback"
PENDING_PROVIDER = "x_oauth2_pending"
#: A login link lives this long.
PENDING_TTL_SEC = 600
#: At most this many open login links; the oldest is dropped first.
MAX_PENDING = 5


class LoginFlowError(RuntimeError):
    """``/x login`` cannot start; the message is the owner's remedy."""


@dataclass(frozen=True)
class LoginResult:
    """Secret-free outcome of :func:`complete_login`."""

    ok: bool
    reason: str = ""
    owner_user_id: str = ""
    scope: str = ""
    expires_in: int = 0


def _store_path(path: Optional[Path] = None) -> Path:
    if path is not None:
        return Path(path)
    from core.runtime_paths import resolve_data_home
    from polyrob_x.x_browser.session_store import SESSION_FILENAME
    return resolve_data_home() / SESSION_FILENAME


def _file_store(path: Optional[Path] = None):
    from tools.oauth.file_store import FileTokenStore
    return FileTokenStore(_store_path(path))


def _enc():
    from core.security.encryption import get_encryption
    return get_encryption()


def _key(state: str) -> tuple:
    digest = hashlib.sha256(state.encode("utf-8")).hexdigest()[:40]
    return (f"state-{digest}", PENDING_PROVIDER)


def redirect_uri_remedy() -> str:
    return (f"Set {REDIRECT_FLAG} to https://<your console host>{CALLBACK_PATH} in the "
            "instance env, register that exact URL as a callback URI of the X app "
            "(developer.x.com → your app → User authentication settings), and restart "
            "the agent and the console.")


def configured_redirect_uri() -> str:
    """The validated callback URL, or raise :class:`LoginFlowError` with the remedy.
    Nothing is derived: an unset flag is an answer, not a guess."""
    raw = (os.environ.get(REDIRECT_FLAG) or "").strip()
    if not raw:
        raise LoginFlowError(f"{REDIRECT_FLAG} is not set. " + redirect_uri_remedy())
    parts = urlsplit(raw)
    local = parts.hostname in ("127.0.0.1", "localhost", "::1")
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise LoginFlowError(f"{REDIRECT_FLAG} must be an https URL (http only for "
                             "127.0.0.1/localhost). " + redirect_uri_remedy())
    if parts.path.rstrip("/") != CALLBACK_PATH or parts.query or parts.fragment:
        raise LoginFlowError(f"{REDIRECT_FLAG} must end in {CALLBACK_PATH} (no query): that "
                             "is the console route that completes the login. "
                             + redirect_uri_remedy())
    return raw.rstrip("/")


def _pending_rows(store) -> list:
    """``[(key, record_or_None)]`` for every pending row (None = undecryptable)."""
    out = []
    for key in list(store.keys()):
        if key[1] != PENDING_PROVIDER:
            continue
        try:
            out.append((key, _enc().decrypt_dict(store[key])))
        except Exception:
            out.append((key, None))
    return out


def prune_pending(*, now: Optional[float] = None, path: Optional[Path] = None,
                  keep: int = MAX_PENDING) -> int:
    """Delete expired and undecryptable pending rows, then the oldest beyond
    ``keep``. Returns how many rows it removed."""
    now = time.time() if now is None else now
    store = _file_store(path)
    rows = _pending_rows(store)
    doomed = [k for k, rec in rows
              if rec is None or float(rec.get("expires_at") or 0) <= now]
    live = sorted(((float(rec.get("created_at") or 0), k) for k, rec in rows
                   if rec is not None and k not in doomed), reverse=True)
    doomed += [k for _ts, k in live[max(0, keep):]]
    for key in doomed:
        try:
            del store[key]
        except KeyError:
            pass
    return len(doomed)


def begin_login(owner_user_id: str, redirect_uri: str, *, now: Optional[float] = None,
                path: Optional[Path] = None) -> str:
    """Mint a single-use login for ``owner_user_id``; return X's authorize URL.

    Raises :class:`LoginFlowError` (the remedy) when the app's client id is not
    configured or the owner is unnamed."""
    from polyrob_x import x_oauth2
    owner = str(owner_user_id or "").strip()
    if not owner:
        raise LoginFlowError("an X login must be bound to the owner who asked for it")
    now = time.time() if now is None else now
    verifier, challenge = x_oauth2.pkce_pair()
    state = secrets.token_urlsafe(32)
    try:
        url = x_oauth2.authorize_url(redirect_uri=redirect_uri, state=state,
                                     code_challenge=challenge)
    except RuntimeError as exc:
        raise LoginFlowError(f"{exc}. Set TWITTER_OAUTH2_CLIENT_ID (and "
                             "TWITTER_OAUTH2_CLIENT_SECRET for a confidential app) in the "
                             "instance env.") from exc
    prune_pending(now=now, path=path, keep=MAX_PENDING - 1)
    record = {"owner_user_id": owner, "verifier": verifier, "redirect_uri": redirect_uri,
              "created_at": now, "expires_at": now + PENDING_TTL_SEC}
    _file_store(path)[_key(state)] = _enc().encrypt_dict(record)
    logger.info("x login: a login link was minted for the owner (valid %ds)",
                PENDING_TTL_SEC)
    return url


def _take(state: str, path: Optional[Path]) -> Optional[dict]:
    """Look up AND delete the pending row (single use). None = no such row."""
    store = _file_store(path)
    key = _key(state)
    blob = store.get(key)
    if blob is None:
        return None
    try:
        del store[key]
    except KeyError:
        return None                    # another request took it first
    try:
        return _enc().decrypt_dict(blob)
    except Exception:
        return {}


def discard(state: str, *, path: Optional[Path] = None) -> str:
    """Burn a pending login (X answered with an error: the owner declined).
    Returns the owner it was minted for ("" when unknown). Never raises."""
    state = str(state or "").strip()
    if not state:
        return ""
    try:
        record = _take(state, path)
    except Exception:  # noqa: BLE001
        return ""
    return str((record or {}).get("owner_user_id") or "")


def complete_login(state: str, code: str, *, expected_owner: Optional[str] = None,
                   now: Optional[float] = None, path: Optional[Path] = None,
                   transport: Any = None) -> LoginResult:
    """Finish a login the owner started: single use, expiry, owner binding, then
    ``x_oauth2.exchange_code``. Never raises; never returns a secret."""
    state = str(state or "").strip()
    code = str(code or "").strip()
    if not state:
        return LoginResult(False, "the link carries no state")
    now = time.time() if now is None else now
    try:
        record = _take(state, path)
    except Exception as exc:  # noqa: BLE001 — an unreadable store is not an empty one
        logger.warning("x login: the pending store could not be read: %s", type(exc).__name__)
        return LoginResult(False, "the X login store could not be read on this server")
    try:
        prune_pending(now=now, path=path)
    except Exception:  # noqa: BLE001 — cleanup is best effort
        logger.debug("x login: pending prune skipped", exc_info=True)
    if record is None:
        return LoginResult(False, "this login link is unknown or was already used — "
                                  "send /x login again")
    if not record:
        return LoginResult(False, "this login link could not be decrypted on this server "
                                  "(MCP_ENCRYPTION_KEY differs between the processes?)")
    owner = str(record.get("owner_user_id") or "")
    if float(record.get("expires_at") or 0) <= now:
        return LoginResult(False, "this login link expired — send /x login again",
                           owner_user_id=owner)
    if expected_owner is not None and str(expected_owner) != owner:
        logger.warning("x login: refused — the link was minted for another owner")
        return LoginResult(False, "this login link belongs to another owner")
    if not code:
        return LoginResult(False, "X sent no authorization code", owner_user_id=owner)
    from polyrob_x import x_oauth2
    try:
        store = x_oauth2.XOAuth2Store(path) if path is not None else None
        rec = x_oauth2.exchange_code(code, redirect_uri=str(record.get("redirect_uri") or ""),
                                     code_verifier=str(record.get("verifier") or ""),
                                     store=store, transport=transport)
    except Exception as exc:  # noqa: BLE001 — the token endpoint's reason, never a secret
        logger.warning("x login: the code exchange failed: %s", exc)
        return LoginResult(False, f"X refused the code exchange: {exc}", owner_user_id=owner)
    left = int(float(rec.get("expires_at") or 0) - time.time())
    logger.info("x login: renewed (scope=%s, expires in %ds)", rec.get("scope") or "?", left)
    return LoginResult(True, owner_user_id=owner, scope=str(rec.get("scope") or ""),
                       expires_in=max(0, left))


__all__ = ["CALLBACK_PATH", "LoginFlowError", "LoginResult", "MAX_PENDING",
           "PENDING_PROVIDER", "PENDING_TTL_SEC", "REDIRECT_FLAG", "begin_login",
           "complete_login", "configured_redirect_uri", "discard", "prune_pending",
           "redirect_uri_remedy"]
