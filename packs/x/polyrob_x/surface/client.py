"""Thin X API v2 DM client over Tweepy user context, off-loop.

DM endpoints require USER-context auth (app-only bearer is rejected). Prefer
the OAuth 2.0 user token from the managed store (``polyrob_x/x_oauth2.py``); the
OAuth 1.0a user-context credential set remains supported and is the FALLBACK
when the OAuth2 login is refused (401) — ``/2/dm_events`` and the DM send accept
both. The ordinary ``TWITTER_BEARER_TOKEN`` is app-only and is NEVER used as a DM
user token.

A 401 the client cannot fix (no refresh, no OAuth1 fallback) raises
:class:`XUnauthorized` — an :class:`XRateLimited` whose ``reset_at`` is an
exponential back-off (5 min doubling to 1 h), so the poller waits instead of
re-sending a dead token every cycle, and the WARNING is logged once per episode
(2026-09-26 P1-2: every surface 401 re-POSTed the dead refresh token and logged).

Rate-limit reality (docs.x.com, 2026-07): GET /2/dm_events is 15 req/15 min per
user (shared across DM GET endpoints); POST dm_conversations/... messages is
15/15 min + 1,440/24 h. A 429 surfaces as :class:`XRateLimited` carrying the
``x-rate-limit-reset`` epoch so the poller can back off to the reset, not a
fixed sleep.

Important: successful user-context authentication does not guarantee that the
account's X access tier exposes every inbound event. Callers must treat an
empty or own-only API page as "API returned no inbound", never "no replies".
The ``x_browser`` tool is the UI-authoritative fallback for that case.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DM_EVENT_FIELDS = ["id", "event_type", "text", "sender_id",
                    "dm_conversation_id", "created_at", "participant_ids"]


class XRateLimited(Exception):
    """X returned 429; ``reset_at`` is the epoch when the window reopens."""

    def __init__(self, reset_at: Optional[float] = None) -> None:
        super().__init__(f"x rate limited until {reset_at}")
        self.reset_at = reset_at


#: First back-off after an unfixable 401; doubles per repeat, capped.
UNAUTH_BACKOFF_BASE_SEC = 300.0
UNAUTH_BACKOFF_CAP_SEC = 3600.0


class XUnauthorized(XRateLimited):
    """X refused the DM login (401) and neither a refresh nor the OAuth 1.0a
    keys fixed it. A :class:`XRateLimited` so the poller backs off to
    ``reset_at`` rather than retrying every cycle."""

    def __init__(self, reset_at: Optional[float] = None, remedy: str = "") -> None:
        Exception.__init__(self, f"x dm login refused (401); backing off until "
                                 f"{reset_at}. {remedy}".strip())
        self.reset_at = reset_at
        self.remedy = remedy


def _relogin_remedy() -> str:
    try:
        from polyrob_x.x_oauth2 import RELOGIN_REMEDY
        return RELOGIN_REMEDY
    except Exception:
        return "re-login the agent's X account (`/x login` or `polyrob x-account oauth-login`)"


def _reset_epoch_from(exc: Any) -> Optional[float]:
    try:
        headers = getattr(getattr(exc, "response", None), "headers", None) or {}
        raw = headers.get("x-rate-limit-reset")
        return float(raw) if raw else None
    except Exception:
        return None


class XDMClient:
    def __init__(self, creds: Optional[dict] = None) -> None:
        creds = creds or {}
        self._api_key = creds.get("api_key") or os.getenv("TWITTER_API_KEY", "")
        self._api_secret = (creds.get("api_secret")
                            or os.getenv("TWITTER_API_SECRET_KEY", ""))
        self._access_token = (creds.get("access_token")
                              or os.getenv("TWITTER_ACCESS_TOKEN", ""))
        self._access_token_secret = (creds.get("access_token_secret")
                                     or os.getenv("TWITTER_ACCESS_TOKEN_SECRET", ""))
        # An explicit creds value is a test/injection seam and is used as-is.
        # Otherwise the OAuth2 token comes from the managed resolver
        # (polyrob_x/x_oauth2.py): encrypted store → auto-refresh → env override.
        self._oauth2_static = creds.get("oauth2_access_token") or ""
        self._oauth2_access_token = self._oauth2_static or self._resolve_oauth2()
        self._client = None  # lazy tweepy.Client
        # 401 back-off state + the OAuth2 token parked after a 401 fell back to
        # OAuth1 (un-parked when the store yields a DIFFERENT token: a re-login).
        self._unauth_count = 0
        self._unauth_until = 0.0
        self._oauth2_parked = ""

    @staticmethod
    def _resolve_oauth2(force_refresh: bool = False) -> str:
        try:
            from polyrob_x.x_oauth2 import resolve_access_token
            return resolve_access_token(force_refresh=force_refresh) or ""
        except Exception as e:  # never let the resolver take the surface down
            logger.warning("x oauth2 resolver failed (%s); falling back to env", e)
            return os.getenv("TWITTER_OAUTH2_ACCESS_TOKEN", "")

    def _refresh_oauth2_client(self) -> bool:
        """Re-resolve the OAuth2 token (forcing a refresh) and rebuild tweepy.
        Returns True when the token CHANGED — the caller retries exactly once."""
        if self._oauth2_static or not self._oauth2_access_token:
            return False
        fresh = self._resolve_oauth2(force_refresh=True)
        if not fresh or fresh == self._oauth2_access_token:
            return False
        self._oauth2_access_token = fresh
        self._client = None
        return True

    @property
    def _oauth1_complete(self) -> bool:
        return all((self._api_key, self._api_secret,
                    self._access_token, self._access_token_secret))

    def _fallback_to_oauth1(self) -> bool:
        """Park the refused OAuth2 token and use the OAuth 1.0a keys. True when
        the switch happened (the caller retries once)."""
        if not self._oauth2_access_token or not self._oauth1_complete:
            return False
        self._oauth2_parked = self._oauth2_access_token
        self._oauth2_access_token = ""
        self._client = None
        logger.warning("x dm: the OAuth 2.0 login was refused (401) — using the "
                       "OAuth 1.0a keys for DMs until it is renewed; %s", _relogin_remedy())
        return True

    def _maybe_unpark_oauth2(self) -> None:
        """A re-login minted a new OAuth2 token: go back to it."""
        if not self._oauth2_parked or self._oauth2_static:
            return
        fresh = self._resolve_oauth2()
        if fresh and fresh != self._oauth2_parked:
            self._oauth2_parked = ""
            self._oauth2_access_token = fresh
            self._client = None
            self._unauth_count = 0
            self._unauth_until = 0.0
            logger.info("x dm: a renewed OAuth 2.0 login is available — using it again")

    def _unauthorized(self) -> XUnauthorized:
        self._unauth_count += 1
        backoff = min(UNAUTH_BACKOFF_BASE_SEC * (2 ** (self._unauth_count - 1)),
                      UNAUTH_BACKOFF_CAP_SEC)
        self._unauth_until = time.time() + backoff
        remedy = _relogin_remedy()
        if self._unauth_count == 1:
            logger.warning("x dm: X refused the login (401, auth=%s); backing off %ds "
                           "per repeat (cap %ds). Owner remedy: %s", self.auth_mode,
                           int(backoff), int(UNAUTH_BACKOFF_CAP_SEC), remedy)
        else:
            logger.debug("x dm: 401 again (#%d); backing off %ds",
                         self._unauth_count, int(backoff))
        return XUnauthorized(reset_at=self._unauth_until, remedy=remedy)

    @property
    def has_credentials(self) -> bool:
        return bool(self._oauth2_access_token) or all((
            self._api_key, self._api_secret,
            self._access_token, self._access_token_secret))

    @property
    def auth_mode(self) -> str:
        return "oauth2_user" if self._oauth2_access_token else "oauth1_user"

    @property
    def _user_auth(self) -> bool:
        # Tweepy uses user_auth=False for OAuth2 bearer-style requests. Because
        # this is the dedicated PKCE USER token, that is user context—not app-only.
        return not bool(self._oauth2_access_token)

    def _tweepy(self):
        if self._client is None and self._oauth2_access_token and not self._oauth2_static:
            # Pick up a proactively refreshed token (the resolver refreshes
            # within REFRESH_SKEW_SEC of expiry) before binding a client.
            fresh = self._resolve_oauth2()
            if fresh:
                self._oauth2_access_token = fresh
        if self._client is None:
            import tweepy
            if self._oauth2_access_token:
                self._client = tweepy.Client(
                    bearer_token=self._oauth2_access_token,
                    wait_on_rate_limit=False)
            else:
                self._client = tweepy.Client(
                    consumer_key=self._api_key,
                    consumer_secret=self._api_secret,
                    access_token=self._access_token,
                    access_token_secret=self._access_token_secret,
                    wait_on_rate_limit=False)
        return self._client

    async def _call(self, fn, *args, **kwargs):
        """Run a tweepy call off-loop. A 401 on the OAuth2 rail is treated as an
        expired access token: refresh once through the store and retry the SAME
        call with the rebuilt client (``fn`` is re-looked-up by name so the
        retry hits the new client, not the stale bound method). A 401 the
        refresh cannot fix falls back to the OAuth 1.0a keys when they are set;
        otherwise :class:`XUnauthorized` backs the caller off."""
        import tweepy
        self._maybe_unpark_oauth2()
        if self._unauth_until > time.time():
            raise XUnauthorized(reset_at=self._unauth_until, remedy=_relogin_remedy())
        name = getattr(fn, "__name__", "")

        async def _run(f, kw):
            try:
                out = await asyncio.to_thread(f, *args, **kw)
            except tweepy.TooManyRequests as e:
                raise XRateLimited(reset_at=_reset_epoch_from(e)) from e
            self._unauth_count = 0
            self._unauth_until = 0.0
            return out

        def _rebound(kw):
            client = self._tweepy()
            if "user_auth" in kw:
                kw = dict(kw, user_auth=self._user_auth)
            return (getattr(client, name, None) or fn), kw

        try:
            return await _run(fn, kwargs)
        except tweepy.Unauthorized:
            pass
        if self._refresh_oauth2_client():
            f, kw = _rebound(kwargs)
            try:
                return await _run(f, kw)
            except tweepy.Unauthorized:
                pass
        if self._fallback_to_oauth1():
            f, kw = _rebound(kwargs)
            try:
                return await _run(f, kw)
            except tweepy.Unauthorized:
                pass
        raise self._unauthorized()

    async def get_me(self) -> str:
        """The authenticated bot account's user id (needed to skip own echoes)."""
        resp = await self._call(
            self._tweepy().get_me, user_auth=self._user_auth)
        return str(resp.data.id)

    async def get_dm_events(self, pagination_token: Optional[str] = None,
                            max_results: int = 50) -> dict:
        """One page of DM events (newest first): ``{"events": [dict], "next_token"}``."""
        resp = await self._call(
            self._tweepy().get_direct_message_events,
            dm_event_fields=_DM_EVENT_FIELDS,
            event_types="MessageCreate",
            max_results=max_results,
            pagination_token=pagination_token,
            user_auth=self._user_auth,
        )
        events = []
        for e in (resp.data or []):
            data = dict(getattr(e, "data", None) or {})
            if "id" in data:
                data["id"] = str(data["id"])
            if "sender_id" in data and data["sender_id"] is not None:
                data["sender_id"] = str(data["sender_id"])
            events.append(data)
        meta = getattr(resp, "meta", None) or {}
        return {"events": events, "next_token": meta.get("next_token")}

    async def send_dm(self, participant_id: str, text: str) -> dict:
        """POST /2/dm_conversations/with/:participant_id/messages."""
        resp = await self._call(
            self._tweepy().create_direct_message,
            participant_id=str(participant_id), text=text,
            user_auth=self._user_auth,
        )
        return dict(resp.data or {})

    async def close(self) -> None:
        return None  # tweepy's sync Client holds no persistent connection
