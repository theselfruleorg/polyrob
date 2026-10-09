"""The session feed backfill: ``GET /api/session/{session_id}/feed/events``.

Moved out of ``webview/server.py`` (070 W0.10) and changed in three ways:

* **Newest N, returned oldest first.** The old route sorted by FILE NAME and
  kept the first ``limit`` — the OLDEST events, and since telemetry files start
  with digits (``000031_tool_result.json``) and ``add_to_feed`` files with a
  letter (``agent_message_1790….json``), a long chat reopened on its first turn
  with its replies cut off. Now the files are read newest first by mtime, the
  newest ``limit`` are kept, and they are returned in time order.
* **Both file schemes.** ``?event_type=`` matched only ``{type}_*.json``
  (scheme B), so ``?event_type=tool_started`` found nothing; it now also matches
  telemetry's ``{seq:06d}_{type}[_{step:04d}].json`` (scheme A).
* **The full type list** the transcript draws (``agent_message``,
  ``task_complete``, ``tool_started`` and the rest) is accepted.

Auth is unchanged: the auth middleware gates the path, and the data is read
under the session OWNER's id (``pm().get_session_user``), else the caller's.
"""
from __future__ import annotations

from webview.session_access import http_session_id

import json
import logging
import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()

#: Every event type a caller may filter by.
VALID_EVENT_TYPES = (
    "step", "planner", "evaluation", "multi_agent_relationship",
    "agent_registration", "session_start", "task_update", "llm_request",
    "service_actions", "available_actions", "status",
    "user_message", "queue_status",
    "tool_execution", "tool_result",
    # 070 W0.10: the types the transcript draws that were refused with a 400.
    "agent_message", "task_complete", "tool_started", "document_uploaded",
    "user_message_during_execution", "session_completion", "llm_started",
)

_NO_STORE = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}


def _type_matcher(event_type: str):
    """A file-name predicate for one type, over both schemes."""
    t = re.escape(event_type)
    scheme_a = re.compile(rf"^\d{{6}}_{t}(_\d{{4}})?\.json$")
    scheme_b = re.compile(rf"^{t}_\d+\.json$")
    return lambda name: bool(scheme_a.match(name) or scheme_b.match(name))


def _seconds(item: dict) -> float:
    """The event's time in seconds (a filename-derived ms stamp is divided)."""
    try:
        ts = float(item.get("timestamp") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return ts / 1000.0 if ts > 1e12 else ts


def _seq(item: dict) -> int:
    try:
        return int(item.get("_seq") or 0)
    except (TypeError, ValueError):
        return 0


#: How many 1 s polls a feed watcher waits for the agent to create the feed.
FEED_WAIT_POLLS = 600


async def wait_for_feed_dir(path_manager, clean_id: str, polls: Optional[int] = None):
    """The session's feed dir once the AGENT has made it, or None (WEB-9).

    Read-only (no mkdir, no ``_anonymous_`` fallback) and off the event loop;
    an id that never appears gives up, and a later join restarts the watcher.
    """
    import asyncio
    for _ in range(FEED_WAIT_POLLS if polls is None else polls):
        feed_dir = await asyncio.to_thread(path_manager.find_feed_dir, clean_id)
        if feed_dir is not None:
            return feed_dir
        await asyncio.sleep(1.0)
    return None


def read_feed(feed_dir, *, event_type: Optional[str], limit: int,
              after_seq: Optional[int]) -> dict:
    """The newest ``limit`` events of *feed_dir*, oldest first, plus the
    delta-sync metadata (``last_seq``, ``total``, ``has_more``)."""
    from webview.server import _enrich_llm_event_with_cost

    match = _type_matcher(event_type) if event_type else None
    files = [p for p in feed_dir.glob("*.json") if not match or match(p.name)]

    def _mtime(path):
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    files.sort(key=_mtime, reverse=True)
    items = []
    read = 0
    for path in files:
        if len(items) >= limit:
            break
        read += 1
        try:
            with path.open("r") as fh:
                item = json.load(fh)
        except Exception as exc:
            logger.debug("feed: could not read %s: %s", path, exc)
            continue
        if not isinstance(item, dict):
            continue
        item_seq = item.get("_seq")
        if after_seq is not None and item_seq is not None and item_seq <= after_seq:
            continue
        if "event_type" in item and "type" not in item:
            item["type"] = item["event_type"]
        if "timestamp" not in item:
            parts = path.stem.split("_")
            try:
                item["timestamp"] = int(parts[-1]) if len(parts) >= 2 else _mtime(path)
            except ValueError:
                item["timestamp"] = _mtime(path)
        item["_source_file"] = path.name
        _enrich_llm_event_with_cost(item)
        items.append(item)
    items.sort(key=lambda it: (_seconds(it), _seq(it)))
    seqs = [s for s in (it.get("_seq") for it in items) if isinstance(s, int)]
    return {
        "events": items,
        "last_seq": max(seqs) if seqs else 0,
        "total": len(items),
        "has_more": read < len(files),
    }


@router.get("/api/session/{session_id}/feed/events", response_class=JSONResponse)
async def api_feed_events(request: Request, session_id: str,
                          event_type: Optional[str] = None,
                          limit: Optional[int] = None,
                          after_seq: Optional[int] = None):
    """The session's feed events, newest ``limit`` (default
    ``WEBVIEW_FEED_DEFAULT_LIMIT``, at most ``WEBVIEW_FEED_MAX_LIMIT``),
    returned in time order. ``after_seq`` keeps only events with a higher
    ``_seq`` (delta sync)."""
    from agents.task.path import pm
    from utils.auth_utils import get_authenticated_user_id
    from webview.server import FEED_DEFAULT_LIMIT, FEED_MAX_LIMIT

    limit = FEED_DEFAULT_LIMIT if limit is None else limit
    if limit < 1 or limit > FEED_MAX_LIMIT:
        raise HTTPException(400, {"error": "invalid_limit", "min": 1, "max": FEED_MAX_LIMIT})
    if event_type and event_type not in VALID_EVENT_TYPES:
        raise HTTPException(400, {"error": "invalid_event_type",
                                  "allowed": list(VALID_EVENT_TYPES)})

    # The session OWNER's id, so a shared session is readable (unchanged).
    owner = pm().get_session_user(session_id)
    user_id = owner if owner else get_authenticated_user_id(request)
    clean_id = http_session_id(session_id, pm())
    logger.debug("feed events: session=%s user=%s type=%s", clean_id, user_id, event_type)
    try:
        # WEB-9: read-only lookup (no mkdir, no sleep), off the event loop.
        import asyncio
        feed_dir = await asyncio.to_thread(pm().find_feed_dir, clean_id, user_id)
    except Exception as exc:
        logger.error("feed events: no feed dir for %s: %s", clean_id, exc)
        raise HTTPException(500, {"error": "internal_error"})
    if feed_dir is None:
        logger.debug("feed events: feed dir missing for %s", clean_id)
        raise HTTPException(404, {"error": "session_not_found"})
    body = read_feed(feed_dir, event_type=event_type, limit=limit, after_seq=after_seq)
    return JSONResponse(body, headers=_NO_STORE)


from webview.contributions import register_console_router  # noqa: E402

register_console_router(router, destination="new", source="webview.feed_routes")
