"""Thin X API v2 DM client over tweepy (OAuth 1.0a user context), off-loop.

DM endpoints require USER-context auth (app-only bearer is rejected), so this
reuses the OAuth1 creds POLYROB already stores (``BotConfig.get_twitter_config``:
api_key/api_secret/access_token/access_token_secret) — never a second credential
store. tweepy is already a dependency (tools/twitter_tool.py); every call runs
via ``asyncio.to_thread`` so the sync SDK never blocks the event loop.

Rate-limit reality (docs.x.com, 2026-07): GET /2/dm_events is 15 req/15 min per
user (shared across DM GET endpoints); POST dm_conversations/... messages is
15/15 min + 1,440/24 h. A 429 surfaces as :class:`XRateLimited` carrying the
``x-rate-limit-reset`` epoch so the poller can back off to the reset, not a
fixed sleep.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DM_EVENT_FIELDS = ["id", "event_type", "text", "sender_id",
                    "dm_conversation_id", "created_at", "participant_ids"]


class XRateLimited(Exception):
    """X returned 429; ``reset_at`` is the epoch when the window reopens."""

    def __init__(self, reset_at: Optional[float] = None) -> None:
        super().__init__(f"x rate limited until {reset_at}")
        self.reset_at = reset_at


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
        self._client = None  # lazy tweepy.Client

    @property
    def has_credentials(self) -> bool:
        return all((self._api_key, self._api_secret,
                    self._access_token, self._access_token_secret))

    def _tweepy(self):
        if self._client is None:
            import tweepy
            self._client = tweepy.Client(
                consumer_key=self._api_key,
                consumer_secret=self._api_secret,
                access_token=self._access_token,
                access_token_secret=self._access_token_secret,
                wait_on_rate_limit=False,  # the poller owns backoff
            )
        return self._client

    async def _call(self, fn, *args, **kwargs):
        import tweepy
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except tweepy.TooManyRequests as e:
            raise XRateLimited(reset_at=_reset_epoch_from(e)) from e

    async def get_me(self) -> str:
        """The authenticated bot account's user id (needed to skip own echoes)."""
        resp = await self._call(self._tweepy().get_me, user_auth=True)
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
            user_auth=True,
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
            participant_id=str(participant_id), text=text, user_auth=True,
        )
        return dict(resp.data or {})

    async def close(self) -> None:
        return None  # tweepy's sync Client holds no persistent connection
