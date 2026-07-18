"""X DM polling: pure dm_event → InboundMessage + the since-id cursor loop.

X's pay-per-use tier has no DM webhook (Account Activity is enterprise), so
inbound is polling ``GET /2/dm_events``. The endpoint has NO ``since_id``
param — it returns events newest-first with ``pagination_token`` — so the
cursor is "newest event id already processed": each poll walks pages until it
meets the cursor (or ``max_pages``), then hands the new events to the handler
oldest-first. The cursor advances over EVERY new event id (own echoes and
skipped shapes included) so nothing is re-fetched forever; parse + dedup are
the correctness backstop.

First run (no cursor) initializes the cursor to the newest existing event
WITHOUT replaying — up to 30 days of DM history would otherwise re-enter the
agent at startup.

``parse_dm_event`` is pure (unit-tested without a socket). Group DM
conversations are skipped in v1: a 1:1 conversation id is ``"<id>-<id>"``; a
bare snowflake means a multi-party conversation, and replying via
``/with/:participant_id`` would leak the reply into a private 1:1 thread.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable, Optional

from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from surfaces.x.client import XRateLimited

logger = logging.getLogger(__name__)


def parse_dm_event(event: dict, bot_user_id: str,
                   user_directory: Any = None) -> Optional[InboundMessage]:
    """dm_event dict → InboundMessage, or None to ignore.

    Ignores: non-MessageCreate events, own messages (outbound echoes come back
    through dm_events), empty text, and group DM conversations (v1).
    """
    if not isinstance(event, dict):
        return None
    if str(event.get("event_type") or "") != "MessageCreate":
        return None
    sender = str(event.get("sender_id") or "")
    if not sender or sender == "None" or sender == str(bot_user_id):
        return None
    text = str(event.get("text") or "").strip()
    if not text or text == "None":
        return None
    conversation = event.get("dm_conversation_id")
    if conversation is not None and "-" not in str(conversation):
        return None  # multi-party conversation → out of scope in v1

    source = SessionSource(surface_id="x", chat_id=sender, chat_type="dm")

    user_id = None
    try:
        from core.instance import owner_surface_alias
        user_id = owner_surface_alias(sender, "x")
    except Exception:
        user_id = None
    if not user_id and user_directory is not None:
        try:
            user_id = user_directory.resolve_internal(sender, "x")
        except Exception:
            user_id = None
    if not user_id:
        user_id = f"u_x_{sender}"

    return InboundMessage(
        text=text,
        identity=Identity(user_id=user_id, source=source, raw_user_id=sender),
        idempotency_key=str(event.get("id") or "") or None,
        raw=event,
        mentions_bot=None,
    )


class XCursorStore:
    """Durable newest-processed-event-id, atomic JSON under data_dir.

    A restart resumes from the cursor instead of re-fetching (and re-paying
    rate limit for) the whole 30-day window; dedup is the backstop, the cursor
    is the budget saver.
    """

    def __init__(self, path: str) -> None:
        self.path = path

    def get(self) -> Optional[str]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                value = json.load(f).get("since_id")
            return str(value) if value else None
        except FileNotFoundError:
            return None
        except Exception as e:
            logger.warning("XCursorStore read failed (%s) — treating as empty", e)
            return None

    def set(self, event_id: str) -> None:
        tmp = f"{self.path}.tmp"
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"since_id": str(event_id)}, f)
            os.replace(tmp, self.path)
        except Exception as e:  # fail-open: dedup still protects correctness
            logger.warning("XCursorStore write failed: %s", e)


class XDMPoller:
    """Poll → hand new events (oldest first) to ``handler`` → advance cursor.

    ``poll_sec`` defaults to 90: GET /2/dm_events allows 15 req/15 min per
    user, so 60s polling sits exactly at the cap with zero headroom for
    pagination; 90s (10/15 min) leaves room. A 429 backs off to the
    ``x-rate-limit-reset`` epoch (via :class:`XRateLimited`), never a fixed
    sleep.
    """

    def __init__(self, client: Any,
                 handler: Callable[[dict], Awaitable[None]],
                 cursor: XCursorStore, *,
                 poll_sec: float = 90.0, max_pages: int = 3) -> None:
        self._client = client
        self._handler = handler
        self._cursor = cursor
        self.poll_sec = float(poll_sec)
        self.max_pages = int(max_pages)
        self._sleep_until = 0.0
        self._stopped = asyncio.Event()

    async def stop(self) -> None:
        self._stopped.set()

    def next_delay(self) -> float:
        remaining = self._sleep_until - time.time()
        if remaining > 0:
            return max(remaining, self.poll_sec)
        return self.poll_sec

    async def _fetch_new(self, since: Optional[str]) -> list:
        new_events, token, pages = [], None, 0
        while pages < self.max_pages:
            resp = await self._client.get_dm_events(pagination_token=token)
            page = resp.get("events") or []
            pages += 1
            if since is None:
                return page  # first run: caller only needs the newest id
            hit_cursor = False
            for ev in page:  # newest-first within a page
                try:
                    eid = int(str(ev.get("id")))
                except (TypeError, ValueError):
                    continue
                if eid <= int(since):
                    hit_cursor = True
                    break
                new_events.append(ev)
            token = resp.get("next_token")
            if hit_cursor or not token:
                break
        return new_events

    async def poll_once(self) -> int:
        """One poll cycle. Returns the number of events handed to the handler.
        Never raises; a failed poll leaves the cursor untouched."""
        self._sleep_until = 0.0
        since = self._cursor.get()
        try:
            new_events = await self._fetch_new(since)
        except XRateLimited as e:
            self._sleep_until = e.reset_at or (time.time() + self.poll_sec)
            logger.warning("x dm poll rate-limited; backing off until %s",
                           self._sleep_until)
            return 0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("x dm poll failed: %s", e)
            return 0

        if not new_events:
            return 0
        ids = []
        for ev in new_events:
            try:
                ids.append(int(str(ev.get("id"))))
            except (TypeError, ValueError):
                continue
        if since is None:
            # First run: mark current history as seen, replay nothing.
            if ids:
                self._cursor.set(str(max(ids)))
            return 0

        handled = 0
        for ev in sorted(new_events,
                         key=lambda e: int(str(e.get("id") or 0))):
            try:
                await self._handler(ev)
                handled += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("x dm handler failed for event %s",
                               ev.get("id"), exc_info=True)
        if ids:
            self._cursor.set(str(max(ids)))
        return handled

    async def run(self) -> None:
        while not self._stopped.is_set():
            await self.poll_once()
            try:
                await asyncio.wait_for(self._stopped.wait(),
                                       timeout=self.next_delay())
            except asyncio.TimeoutError:
                continue
