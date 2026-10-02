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

A refresh the token endpoint refuses because the refresh token ITSELF is dead
(``invalid_grant``, or ``invalid_request`` "Value passed for the token was
invalid" — what X answers after the client secret is regenerated, 2026-09-25)
raises :class:`ReloginNeeded` and records the ``x_oauth2`` credential verdict
(code ``relogin_needed``, an OPEN_REMEDY kind: only the owner's re-login closes
it). While that verdict is open the dead refresh token is NOT re-POSTed on every
call; a store record newer than the verdict (the owner re-logged in from a
process that could not clear it) re-arms the refresh. A successful
login/import/refresh clears the verdict.

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
from contextlib import contextmanager
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
#: Longest wait for another PROCESS's refresh before proceeding unlocked (the
#: token POST itself times out at 20 s; a holder past this is wedged).
LOCK_WAIT_SEC = 45.0

#: The credential-verdict kind/key/code a dead refresh token records
#: (``core/credential_verdicts.py``; rendered by ``/status`` + ``polyrob doctor``).
VERDICT_KIND = "x_oauth2"
VERDICT_KEY = ""
RELOGIN_CODE = "relogin_needed"
#: The owner's fix, named on the verdict, the log line and every 401 result.
RELOGIN_REMEDY = ("re-login the agent's X account: `/x login` (Telegram, REPL or "
                  "console) or `polyrob x-account oauth-login` on the box")


class ReloginNeeded(RuntimeError):
    """The token endpoint refused the refresh token itself — no retry fixes it.

    Raised by :func:`refresh` on ``invalid_grant`` or an ``invalid_request``
    whose description says the token was invalid. Only the owner's re-login
    (``/x login`` or ``polyrob x-account oauth-login``) mints a working pair.
    """


#: Per-process once: the "refresh held" WARNING names the remedy one time per
#: verdict episode, not on every resolve.
_REFUSAL_LOGGED: set = set()


def _is_relogin_error(err: str, desc: str) -> bool:
    err = (err or "").strip().lower()
    d = (desc or "").lower()
    if err == "invalid_grant":
        return True
    return err == "invalid_request" and "token" in d and "invalid" in d


def _record_relogin(reason: str) -> None:
    try:
        from core.credential_verdicts import record_rejection
        record_rejection(VERDICT_KIND, VERDICT_KEY, code=RELOGIN_CODE,
                         remedy=RELOGIN_REMEDY)
    except Exception as e:  # fail-open: the verdict is an optimisation
        logger.debug("x oauth2: could not record the relogin verdict: %s", e)


def _clear_relogin() -> None:
    try:
        from core.credential_verdicts import clear_rejection
        clear_rejection(VERDICT_KIND, VERDICT_KEY)
    except Exception as e:
        logger.debug("x oauth2: could not clear the relogin verdict: %s", e)
    _REFUSAL_LOGGED.clear()


def _open_verdict():
    try:
        from core.credential_verdicts import verdict
        v = verdict(VERDICT_KIND, VERDICT_KEY)
    except Exception:
        return None
    if v is None or (v.code or RELOGIN_CODE) != RELOGIN_CODE:
        return None
    return v


def _obtained_epoch(rec: Optional[dict]) -> float:
    raw = (rec or {}).get("obtained_at") or ""
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def relogin_verdict(rec: Optional[dict] = None, *, store: "Optional[XOAuth2Store]" = None):
    """The open ``relogin_needed`` verdict, or None.

    A store record obtained AFTER the verdict's ``first_seen`` means the owner
    re-logged in (from a process that could not clear the verdict) — the
    verdict is then stale and this answers None so the next resolve tries."""
    v = _open_verdict()
    if v is None:
        return None
    if rec is None:
        try:
            rec = (store or XOAuth2Store()).load()
        except Exception:
            rec = None
    if rec is not None and _obtained_epoch(rec) > float(v.first_seen):
        return None
    return v


def relogin_needed() -> bool:
    """True while the OAuth2 login is known dead (the owner must re-login)."""
    return relogin_verdict() is not None


def _default_path() -> Path:
    from polyrob_x.x_browser.session_store import _default_path as _p
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

    @property
    def path(self) -> Path:
        return Path(self._store.path)

    def reload(self) -> None:
        """Re-read the file: another PROCESS may have rotated the pair since this
        instance's snapshot (``FileTokenStore`` reads once at construction)."""
        from tools.oauth.file_store import FileTokenStore
        self._store = FileTokenStore(self.path)

    def has_key(self, key: Optional[str] = None) -> bool:
        """True when the file holds an ``x_oauth2`` row for this key — even an
        undecryptable one — or when the file exists but cannot be read here.
        Either way the store is NOT empty and must never be re-seeded."""
        if (key or _instance_key(), PROVIDER) in self._store:
            return True
        p = self.path
        return p.exists() and not os.access(str(p), os.R_OK)

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
# cross-process refresh lock
# ---------------------------------------------------------------------------

_LOCK_WARNED: set = set()


@contextmanager
def _refresh_lock(store: "XOAuth2Store"):
    """Serialise load → refresh → save across THREADS and PROCESSES.

    ``polyrob`` and ``polyrob-email`` both load the X pack and refreshed the same
    ROTATING token in the same second (prod 09-23 04:47:48, 09-26 04:01:08): the
    loser replayed a spent refresh token. An ``fcntl.flock`` on a sibling
    ``<store>.lock`` file makes the second process wait, then RE-LOAD and find the
    pair already fresh. Fail-open: no ``fcntl`` (Windows), an unwritable lock
    file or a wedged holder degrade to the in-process lock, logged once.
    """
    with _lock:
        fd = None
        lock_path = str(store.path) + ".lock"
        try:
            import fcntl
            store.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o660)
            try:
                if os.stat(str(store.path.parent)).st_mode & 0o020:
                    os.fchmod(fd, 0o660)  # the shared-data convention (polyrob-data)
            except OSError:
                pass
            deadline = time.monotonic() + LOCK_WAIT_SEC
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"{lock_path} held for > {LOCK_WAIT_SEC:.0f}s")
                    time.sleep(0.05)
        except Exception as e:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                fd = None
            if lock_path not in _LOCK_WARNED:
                _LOCK_WARNED.add(lock_path)
                logger.warning("x oauth2: no cross-process refresh lock (%s) — "
                               "refreshing under the in-process lock only", e)
        try:
            yield
        finally:
            if fd is not None:
                try:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except Exception:
                    pass
                try:
                    os.close(fd)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# token exchange
# ---------------------------------------------------------------------------

def _basic_auth_header(cid: str, csecret: str) -> Dict[str, str]:
    raw = base64.b64encode(f"{cid}:{csecret}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def _post_token(form: Dict[str, str], *, transport: Any = None) -> dict:
    """POST to the token endpoint; returns the JSON body or raises ``RuntimeError``
    with X's ``error``/``error_description`` (never the tokens).

    A ``refresh_token`` grant refused because the refresh token is dead raises
    :class:`ReloginNeeded` (a ``RuntimeError``) instead."""
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
        msg = f"x oauth2 token endpoint refused: {err} {desc}".strip()
        if form.get("grant_type") == "refresh_token" and _is_relogin_error(
                str(body.get("error") or ""), str(desc)):
            raise ReloginNeeded(f"{msg} — {RELOGIN_REMEDY}")
        raise RuntimeError(msg)
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
    A dead refresh token raises :class:`ReloginNeeded` and records the
    ``x_oauth2`` verdict; a success clears it.
    """
    store = store or XOAuth2Store()
    with _refresh_lock(store):
        store.reload()
        return _refresh_locked(store, transport=transport)


def _refresh_locked(store: "XOAuth2Store", *, transport: Any = None) -> dict:
    """:func:`refresh` body; the caller holds :func:`_refresh_lock`."""
    prior = store.load()
    if not prior or not prior.get("refresh_token"):
        raise RuntimeError("no refresh token stored — run `polyrob x-account oauth-login` "
                           "(or oauth-import with a refresh token).")
    try:
        body = _post_token({"grant_type": "refresh_token",
                            "refresh_token": prior["refresh_token"]}, transport=transport)
    except ReloginNeeded as e:
        _record_relogin(str(e))
        raise
    rec = _record_from_response(body, source="refresh", prior=prior)
    store.save(rec)
    _clear_relogin()
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
    _clear_relogin()
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
    store = store or XOAuth2Store()
    with _refresh_lock(store):
        # Another process may have refreshed while this one waited on the lock.
        store.reload()
        rec = store.load()
        if rec is None and not store.has_key():
            # Seed ONLY an empty store: an unreadable or undecryptable record
            # (EACCES, another key, another instance id) is not "absent", and
            # overwriting it destroyed the pair the owning process still used.
            rec = _seed_from_env(store)
        if rec is not None:
            expires_at = float(rec.get("expires_at") or 0)
            due = force_refresh or (expires_at - time.time()) < REFRESH_SKEW_SEC
            held = relogin_verdict(rec) if due else None
            if due and held is not None:
                # The refresh token is known dead: re-POSTing it cannot help and
                # buried the one line that mattered (P1-2, 2026-09-26).
                _log_refusal_once(held)
                if expires_at <= time.time():
                    # Known-dead access token too: answer "none" so a caller with
                    # another rail (OAuth 1.0a) takes it instead of a certain 401.
                    static = (os.environ.get("TWITTER_OAUTH2_ACCESS_TOKEN") or "").strip()
                    return static or None
            elif due and rec.get("refresh_token"):
                try:
                    rec = _refresh_locked(store, transport=transport)
                except ReloginNeeded as e:
                    held = relogin_verdict(rec)
                    if held is not None:
                        _log_refusal_once(held)
                    else:
                        logger.warning("x oauth2: %s", e)
                    if expires_at <= time.time():
                        static = (os.environ.get("TWITTER_OAUTH2_ACCESS_TOKEN") or "").strip()
                        return static or None
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


def _log_refusal_once(v) -> None:
    token = round(float(getattr(v, "first_seen", 0) or 0), 3)
    if token in _REFUSAL_LOGGED:
        return
    _REFUSAL_LOGGED.add(token)
    try:
        from core.credential_verdicts import since_text
        since = since_text(v)
    except Exception:
        since = "?"
    logger.warning("x oauth2: the refresh token was refused (relogin_needed since %s) — "
                   "not retrying it; X DMs stay down until the owner acts: %s",
                   since, RELOGIN_REMEDY)


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
    held = relogin_verdict(rec)
    out["relogin_needed"] = held is not None
    if held is not None:
        try:
            from core.credential_verdicts import since_text
            out["relogin_needed_since"] = since_text(held)
        except Exception:
            out["relogin_needed_since"] = ""
        out["relogin_remedy"] = held.remedy or RELOGIN_REMEDY
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
    _clear_relogin()
    return rec
