"""X (Twitter) OAuth 2.0 user-context token: encrypted store + auto-refresh.

The X Chat DM read (``/2/chat/...``) — where every inbound DM now lands — needs a
USER-context OAuth 2.0 token (scopes ``dm.read users.read tweet.read``). Until
2026-09-17 the tree read that token from ONE static env value,
``TWITTER_OAUTH2_ACCESS_TOKEN``, and nothing refreshed it. An X user access
token expires **2 hours** after mint, so a hand-minted token proved the rail
once and then died on the next tick: outbound worked, inbound went dark, and
the tool said "credentials missing" about a rail that had been working an hour
earlier.

This module is the ONE resolver every consumer reads through
(:func:`resolve_access_token`):

1. the encrypted store (``<data_home>/.x_session.json``, provider ``x_oauth2``,
   the same Fernet + ``FileTokenStore`` the X browser session uses) — refreshed
   in place when it is within :data:`REFRESH_SKEW_SEC` of expiry;
2. else the static ``TWITTER_OAUTH2_ACCESS_TOKEN`` env value, kept as a manual
   override (a fresh token pasted for a two-hour test still works). If
   ``TWITTER_OAUTH2_REFRESH_TOKEN`` is ALSO set, the pair is seeded into the
   store on first use and managed from then on — so a deploy can start from
   env and never touch env again.

A refresh needs the app's ``TWITTER_OAUTH2_CLIENT_ID`` (+ ``_CLIENT_SECRET``
for a confidential app). X ROTATES the refresh token on every refresh, so the
new one is persisted before the old one is forgotten; a refresh that fails is
logged and the (possibly still valid) old token is returned — the API's own
401 is the honest signal, never a silent swap.

⚠️ Nothing here logs a token value. Presence, expiry and scope only.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

PROVIDER = "x_oauth2"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
REVOKE_URL = "https://api.x.com/2/oauth2/revoke"
#: The scopes the DM read + send need, plus ``offline.access`` so a refresh
#: token is issued at all. ``tweet.read``/``users.read`` are required by X for
#: any user-context call.
DEFAULT_SCOPES = ("dm.read", "dm.write", "tweet.read", "users.read", "offline.access")
#: Refresh this many seconds BEFORE ``expires_at`` (X tokens live 7200 s).
REFRESH_SKEW_SEC = 300
#: Assumed lifetime for an imported token whose ``expires_in`` is unknown.
DEFAULT_LIFETIME_SEC = 7200

_lock = threading.Lock()


def _default_path() -> Path:
    from tools.x_browser.session_store import _default_path as _p
    return _p()


def _instance_key() -> str:
    """One X account per instance — key the record by the instance id."""
    from core.instance import resolve_instance_id
    return resolve_instance_id()


def client_id() -> str:
    return (os.environ.get("TWITTER_OAUTH2_CLIENT_ID") or "").strip()


def client_secret() -> str:
    return (os.environ.get("TWITTER_OAUTH2_CLIENT_SECRET") or "").strip()


class XOAuth2Store:
    """Fernet-encrypted ``(instance_id, "x_oauth2")`` record.

    Record shape: ``access_token``, ``refresh_token``, ``expires_at`` (epoch
    seconds), ``scope``, ``obtained_at``, ``source`` (``pkce``/``import``/
    ``env``/``refresh``).
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        from tools.oauth.file_store import FileTokenStore
        self._store = FileTokenStore(Path(path) if path is not None else _default_path())

    def _enc(self):
        from core.security.encryption import get_encryption
        return get_encryption()

    def load(self, key: Optional[str] = None) -> Optional[dict]:
        blob = self._store.get((key or _instance_key(), PROVIDER))
        if not blob:
            return None
        try:
            return self._enc().decrypt_dict(blob)
        except Exception as e:
            logger.warning("x oauth2 record undecryptable (%s) — treating as absent; "
                           "re-run `polyrob x-account oauth-login`", e)
            return None

    def save(self, record: Dict[str, Any], key: Optional[str] = None) -> None:
        rec = dict(record)
        rec.setdefault("obtained_at", datetime.now(timezone.utc).isoformat())
        self._store[(key or _instance_key(), PROVIDER)] = self._enc().encrypt_dict(rec)

    def delete(self, key: Optional[str] = None) -> None:
        try:
            del self._store[(key or _instance_key(), PROVIDER)]
        except KeyError:
            pass

    def exists(self, key: Optional[str] = None) -> bool:
        return (key or _instance_key(), PROVIDER) in self._store


# ---------------------------------------------------------------------------
# token exchange
# ---------------------------------------------------------------------------

def _basic_auth_header(cid: str, csecret: str) -> Dict[str, str]:
    raw = base64.b64encode(f"{cid}:{csecret}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def _post_token(form: Dict[str, str], *, transport: Any = None) -> dict:
    """POST to the token endpoint; returns the JSON body or raises ``RuntimeError``
    with X's ``error``/``error_description`` (never the tokens)."""
    import httpx
    cid, csecret = client_id(), client_secret()
    if not cid:
        raise RuntimeError("TWITTER_OAUTH2_CLIENT_ID is not set — the app's Client ID "
                           "(developer.x.com → your app → Keys and tokens → OAuth 2.0) "
                           "is required to mint or refresh a user token.")
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    form = dict(form, client_id=cid)
    if csecret:
        # Confidential app: X wants HTTP Basic with the client secret.
        headers.update(_basic_auth_header(cid, csecret))
    with httpx.Client(timeout=20.0, transport=transport) as http:
        resp = http.post(TOKEN_URL, data=form, headers=headers)
    try:
        body = resp.json()
    except Exception:
        body = {}
    if resp.status_code >= 400 or "access_token" not in body:
        err = body.get("error") or f"http {resp.status_code}"
        desc = body.get("error_description") or ""
        raise RuntimeError(f"x oauth2 token endpoint refused: {err} {desc}".strip())
    return body


def _record_from_response(body: dict, *, source: str, prior: Optional[dict] = None) -> dict:
    now = time.time()
    lifetime = body.get("expires_in")
    try:
        lifetime = int(lifetime) if lifetime is not None else DEFAULT_LIFETIME_SEC
    except (TypeError, ValueError):
        lifetime = DEFAULT_LIFETIME_SEC
    rec = {
        "access_token": str(body["access_token"]),
        # X rotates the refresh token; if the response omits one, keep the old.
        "refresh_token": str(body.get("refresh_token") or (prior or {}).get("refresh_token") or ""),
        "expires_at": now + lifetime,
        "scope": str(body.get("scope") or (prior or {}).get("scope") or ""),
        "token_type": str(body.get("token_type") or "bearer"),
        "source": source,
        "obtained_at": datetime.now(timezone.utc).isoformat(),
    }
    return rec


def refresh(store: Optional[XOAuth2Store] = None, *, transport: Any = None) -> dict:
    """Exchange the stored refresh token for a new pair; persist; return the record.

    Raises ``RuntimeError`` (with X's reason) on failure and leaves the stored
    record UNCHANGED, so a transient outage does not erase a still-valid pair.
    """
    store = store or XOAuth2Store()
    prior = store.load()
    if not prior or not prior.get("refresh_token"):
        raise RuntimeError("no refresh token stored — run `polyrob x-account oauth-login` "
                           "(or oauth-import with a refresh token).")
    body = _post_token({"grant_type": "refresh_token",
                        "refresh_token": prior["refresh_token"]}, transport=transport)
    rec = _record_from_response(body, source="refresh", prior=prior)
    store.save(rec)
    logger.info("x oauth2: token refreshed (expires in %ds, scope=%s)",
                int(rec["expires_at"] - time.time()), rec.get("scope") or "?")
    return rec


def import_pair(access_token: str, refresh_token: str = "", *,
                expires_in: Optional[int] = None, scope: str = "",
                store: Optional[XOAuth2Store] = None) -> dict:
    """Seed the store from a pair minted elsewhere (a manual PKCE run, a paste).

    ``expires_in`` unknown ⇒ the token is assumed FRESH for the full lifetime;
    if it is in fact older, the first API 401 triggers a refresh anyway.
    """
    store = store or XOAuth2Store()
    access_token = (access_token or "").strip()
    if not access_token:
        raise ValueError("access token is empty")
    rec = _record_from_response(
        {"access_token": access_token, "refresh_token": (refresh_token or "").strip(),
         "expires_in": expires_in if expires_in is not None else DEFAULT_LIFETIME_SEC,
         "scope": scope},
        source="import")
    store.save(rec)
    return rec


def _seed_from_env(store: XOAuth2Store) -> Optional[dict]:
    """First-use seed: TWITTER_OAUTH2_ACCESS_TOKEN (+ _REFRESH_TOKEN) → store."""
    access = (os.environ.get("TWITTER_OAUTH2_ACCESS_TOKEN") or "").strip()
    refresh_tok = (os.environ.get("TWITTER_OAUTH2_REFRESH_TOKEN") or "").strip()
    if not access:
        return None
    if not refresh_tok:
        # A static override with no refresh token is NOT stored — the env
        # value is the truth and the operator replaces it by hand.
        return None
    rec = _record_from_response(
        {"access_token": access, "refresh_token": refresh_tok,
         "expires_in": DEFAULT_LIFETIME_SEC}, source="env")
    store.save(rec)
    logger.info("x oauth2: seeded the token store from env (TWITTER_OAUTH2_*_TOKEN); "
                "refreshes are managed from the store now")
    return rec


def resolve_access_token(*, force_refresh: bool = False,
                         store: Optional[XOAuth2Store] = None,
                         transport: Any = None) -> Optional[str]:
    """The current user-context access token, refreshed if needed; None if none.

    Order: store (refresh when within :data:`REFRESH_SKEW_SEC` of expiry or
    ``force_refresh``) → env seed (access+refresh) → static env access token.
    A failed refresh returns the OLD token (if any) and logs the reason; the
    caller's 401 is the honest signal.
    """
    with _lock:
        store = store or XOAuth2Store()
        rec = store.load()
        if rec is None:
            rec = _seed_from_env(store)
        if rec is not None:
            expires_at = float(rec.get("expires_at") or 0)
            due = force_refresh or (expires_at - time.time()) < REFRESH_SKEW_SEC
            if due and rec.get("refresh_token"):
                try:
                    rec = refresh(store, transport=transport)
                except Exception as e:
                    logger.warning("x oauth2: refresh failed (%s) — using the stored token; "
                                   "expect a 401 if it has expired", e)
            elif due:
                logger.warning("x oauth2: stored token is expired/expiring and there is no "
                               "refresh token — run `polyrob x-account oauth-login`")
            tok = (rec.get("access_token") or "").strip()
            return tok or None
        static = (os.environ.get("TWITTER_OAUTH2_ACCESS_TOKEN") or "").strip()
        return static or None


def oauth2_configured() -> bool:
    """Presence check for gates/doctors: a stored record OR a static env token."""
    try:
        if XOAuth2Store().exists():
            return True
    except Exception:
        pass
    return bool((os.environ.get("TWITTER_OAUTH2_ACCESS_TOKEN") or "").strip())


def status(store: Optional[XOAuth2Store] = None) -> dict:
    """Secret-free status for `polyrob x-account oauth-status` and doctors."""
    store = store or XOAuth2Store()
    rec = store.load()
    static = bool((os.environ.get("TWITTER_OAUTH2_ACCESS_TOKEN") or "").strip())
    out = {"stored": rec is not None, "static_env": static,
           "client_id_set": bool(client_id()), "client_secret_set": bool(client_secret())}
    if rec:
        left = float(rec.get("expires_at") or 0) - time.time()
        out.update({
            "expires_in_sec": int(left),
            "expired": left <= 0,
            "has_refresh_token": bool(rec.get("refresh_token")),
            "scope": rec.get("scope") or "",
            "source": rec.get("source") or "",
            "obtained_at": rec.get("obtained_at") or "",
        })
    return out


# ---------------------------------------------------------------------------
# PKCE (the `oauth-login` ceremony)
# ---------------------------------------------------------------------------

def pkce_pair() -> tuple:
    """(code_verifier, code_challenge) per RFC 7636, S256."""
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def authorize_url(*, redirect_uri: str, state: str, code_challenge: str,
                  scopes: tuple = DEFAULT_SCOPES) -> str:
    cid = client_id()
    if not cid:
        raise RuntimeError("TWITTER_OAUTH2_CLIENT_ID is not set")
    q = {
        "response_type": "code", "client_id": cid, "redirect_uri": redirect_uri,
        "scope": " ".join(scopes), "state": state,
        "code_challenge": code_challenge, "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urlencode(q)}"


def exchange_code(code: str, *, redirect_uri: str, code_verifier: str,
                  store: Optional[XOAuth2Store] = None, transport: Any = None) -> dict:
    """Authorization-code → token pair; persisted; returns the record."""
    store = store or XOAuth2Store()
    body = _post_token({"grant_type": "authorization_code", "code": code,
                        "redirect_uri": redirect_uri, "code_verifier": code_verifier},
                       transport=transport)
    rec = _record_from_response(body, source="pkce")
    store.save(rec)
    return rec
