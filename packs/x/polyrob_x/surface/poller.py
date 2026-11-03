"""X DM polling: pure dm_event → InboundMessage + the since-id cursor loop.

OBSOLETE (2026-10-04): ``/2/dm_events`` no longer receives new DMs (X moved them
to encrypted X Chat). See ``polyrob_x.x_chat_client``.

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
from polyrob_x.surface.client import XRateLimited

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


#: A handler failure on one event holds the cursor below it; after this many
#: failures the event is skipped with an owner-visible ``x_dm_poison`` event.
_MAX_EVENT_RETRIES = 5


def _emit(kind: str, **attrs: Any) -> None:
    try:
        from core.event_log import emit
        emit(kind, source="x.dm_poller", attrs={k: str(v)[:200] for k, v in attrs.items()})
    except Exception:
        pass


def _eid(ev: Any) -> Optional[int]:
    try:
        return int(str(ev.get("id")))
    except (TypeError, ValueError, AttributeError):
        return None


class XCursorStore:
    """Durable DM cursor, atomic JSON under data_dir (098 CL-12).

    v2 shape: ``{"v": 2, "since_id": str, "drain": {"token", "upper"} | null,
    "retries": {event_id: n}}``. ``since_id`` means "every event id at or below
    this was handled" — not "the newest id fetched". A ``drain`` is a page walk
    that hit ``max_pages`` before it met ``since_id``: the next poll continues
    from its token, and ``since_id`` moves to ``upper`` only when the walk meets
    the cursor. A v1 file ``{"since_id": …}`` reads as idle at that id.

    A corrupt file is NOT "first run" (that marked every waiting DM as seen
    without delivery): it is moved aside and the ``.bak`` of the last good
    write is read; with no backup the first-run branch runs and the owner gets
    an ``x_dm_cursor_lost`` event.
    """

    def __init__(self, path: str) -> None:
        self.path = path

    def load(self) -> Optional[dict]:
        """The cursor state, or None when there is none (first run)."""
        state = self._read(self.path)
        if state is None:
            # No main file: a first run has no backup either; a cursor moved
            # aside (or lost) earlier still has its last good write here.
            backup = self._read(f"{self.path}.bak")
            return backup if isinstance(backup, dict) else None
        if state is not _CORRUPT:
            return state
        try:
            os.replace(self.path, f"{self.path}.corrupt-{int(time.time())}")
        except OSError:
            pass
        backup = self._read(f"{self.path}.bak")
        if isinstance(backup, dict):
            logger.warning("XCursorStore: corrupt cursor moved aside; using the backup")
            return backup
        logger.warning("XCursorStore: corrupt cursor and no backup — older DMs are not replayed")
        _emit("x_dm_cursor_lost", path=self.path)
        return None

    @staticmethod
    def _read(path: str):
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return None
        except Exception as e:
            logger.warning("XCursorStore read failed (%s)", e)
            return _CORRUPT
        if not isinstance(raw, dict) or not raw.get("since_id"):
            return _CORRUPT if raw else None
        return {"v": 2, "since_id": str(raw["since_id"]),
                "drain": raw.get("drain") if isinstance(raw.get("drain"), dict) else None,
                "retries": dict(raw.get("retries") or {})}

    def get(self) -> Optional[str]:
        state = self.load()
        return state["since_id"] if state else None

    def save(self, state: dict) -> None:
        from polyrob.sdk import atomic_write_json
        try:
            atomic_write_json(self.path, state, indent=None)
            atomic_write_json(f"{self.path}.bak", state, indent=None)
        except Exception as e:  # fail-open: dedup still protects correctness
            logger.warning("XCursorStore write failed: %s", e)

    def set(self, event_id: str) -> None:
        self.save({"v": 2, "since_id": str(event_id), "drain": None, "retries": {}})


_CORRUPT = object()


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

    async def _fetch_new(self, since: Optional[str], token: Optional[str] = None):
        """``(events, next_token)``. ``next_token`` is set only when the page cap
        ended the walk before it met ``since`` (older pages remain)."""
        new_events, pages = [], 0
        while pages < self.max_pages:
            resp = await self._client.get_dm_events(pagination_token=token)
            page = resp.get("events") or []
            pages += 1
            if since is None:
                return page, None  # first run: caller only needs the newest id
            hit_cursor = False
            for ev in page:  # newest-first within a page
                eid = _eid(ev)
                if eid is None:
                    continue
                if eid <= int(since):
                    hit_cursor = True
                    break
                new_events.append(ev)
            token = resp.get("next_token")
            if hit_cursor or not token:
                return new_events, None
        return new_events, token

    async def poll_once(self) -> int:
        """One poll cycle. Returns the number of events handed to the handler.
        Never raises; a failed poll leaves the cursor untouched.

        CL-12: a handler failure on event E holds ``since_id`` below E (the
        next poll retries it; the surface dedup absorbs the events after E that
        already ran); a page-cap walk is resumed next poll instead of jumping
        the cursor over the unfetched pages."""
        self._sleep_until = 0.0
        state = self._cursor.load()
        since = state["since_id"] if state else None
        drain = state.get("drain") if state else None
        retries = dict(state.get("retries") or {}) if state else {}
        try:
            new_events, more = await self._fetch_new(
                since, drain.get("token") if drain else None)
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

        events = sorted((ev for ev in new_events if _eid(ev) is not None), key=_eid)
        ids = [_eid(ev) for ev in events]
        if since is None:
            # First run: mark current history as seen, replay nothing.
            if ids:
                self._cursor.set(str(max(ids)))
            return 0
        if not events and not drain and not more:
            return 0

        handled, failed_at = 0, None
        last_ok = None
        for ev in events:
            eid = _eid(ev)
            try:
                await self._handler(ev)
                handled += 1
                last_ok = eid
                retries.pop(str(eid), None)
            except asyncio.CancelledError:
                raise
            except Exception:
                n = int(retries.get(str(eid), 0)) + 1
                if n >= _MAX_EVENT_RETRIES:
                    logger.error("x dm event %s failed %d times — skipped", eid, n,
                                 exc_info=True)
                    _emit("x_dm_poison", event_id=eid, attempts=n)
                    retries.pop(str(eid), None)
                    last_ok = eid
                    continue
                logger.warning("x dm handler failed for event %s (attempt %d/%d); "
                               "the cursor holds below it", eid, n, _MAX_EVENT_RETRIES,
                               exc_info=True)
                retries[str(eid)] = n
                failed_at = eid
                break

        new_state = {"v": 2, "since_id": since, "drain": drain, "retries": retries}
        if failed_at is not None:
            if not drain and not more and last_ok is not None:
                new_state["since_id"] = str(last_ok)
            # A drain (or a cap-hit walk) keeps its position: the same pages
            # are walked again next poll.
        elif more:
            upper = drain.get("upper") if drain else (str(max(ids)) if ids else None)
            new_state["drain"] = {"token": more, "upper": upper} if upper else None
        else:
            top = drain.get("upper") if drain else None
            if ids:
                top = str(max(int(top or 0), max(ids)))
            if top:
                new_state["since_id"] = top
            new_state["drain"] = None
        self._cursor.save(new_state)
        return handled

    async def run(self) -> None:
        while not self._stopped.is_set():
            await self.poll_once()
            try:
                await asyncio.wait_for(self._stopped.wait(),
                                       timeout=self.next_delay())
            except asyncio.TimeoutError:
                continue
