"""109: X public threads for the code storefront (``core.commerce.thread.ThreadChannel``).

Registered for the ``x`` surface through the ``commerce.thread_channel`` pack hook.
Core's storefront calls it only while a live offer lists ``native:x`` AND has an
announcement post, so with no such offer nothing here reads or writes X.

* **poll** — ``GET /2/users/:id/mentions`` after a ``since_id``, with the conversation,
  the replied-to post and the edit history; at most one read per
  :data:`POLL_EVERY_S` (reads cost API credit). A recorded 402 or a refused read is a
  :class:`ThreadUnavailable` whose text the owner reads in ``/status``.
* **reply** — the ``twitter_reply`` ACTION, never a raw client call, so the pause,
  cooldown and write-budget gates apply. The owner's go-live card is the approval of
  these replies (the cron-delivery precedent: ``_OWNER_APPROVED_ACTION`` for the
  owner's tenant only, never when the operator denies approvals). Each reply is
  recorded as an ``external_write`` with the post as its ``subject``, so the model's
  one-answer-per-post gate refuses a second answer to a storefront line.
* **share** — code writes only inside :data:`write_budget.COMMERCE_SHARE` of the
  hour's X writes; the rest stays for the owner's and the model's posts.
* A 403 "not part of this conversation" is final for that post: no retry burns a
  write on it again.
"""
from __future__ import annotations

import logging
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Seconds between two mention reads.
POLL_EVERY_S = 120.0
MAX_RESULTS = 50
#: Pages per read (50 posts each) before the rest waits for the next read.
MAX_PAGES = 4
TWEET_FIELDS = ["created_at", "author_id", "conversation_id", "referenced_tweets",
                "edit_history_tweet_ids"]
#: X's texts for "you may not reply in this conversation". A bare "403 Forbidden" is NOT one:
#: a token or app-permission 403 is no fact about the post, so it never kills the post.
_REFUSED_403 = ("not been mentioned", "not part of the conversation")
#: Seconds a refused post stays refused (X may let the agent in later: a new mention).
DEAD_TTL_S = 24 * 3600


def _ts(value: Any) -> float:
    try:
        return float(value.timestamp())
    except Exception:
        return 0.0


def to_post(t: Any) -> Any:
    """A tweepy ``Tweet`` -> ``PublicPost`` (pure; the mapping the tests pin)."""
    from polyrob.sdk import PublicPost
    pid = str(getattr(t, "id", "") or "")
    reply_to = ""
    for ref in getattr(t, "referenced_tweets", None) or ():
        if str(getattr(ref, "type", "")) == "replied_to":
            reply_to = str(getattr(ref, "id", "") or "")
    history = [str(x) for x in (getattr(t, "edit_history_tweet_ids", None) or ())]
    return PublicPost(surface="x", post_id=pid, author_id=str(getattr(t, "author_id", "") or ""),
                      text=str(getattr(t, "text", "") or ""),
                      root_id=str(getattr(t, "conversation_id", "") or pid),
                      reply_to_id=reply_to, created_at=_ts(getattr(t, "created_at", None)),
                      edited=bool(history) and history[0] != pid)


class XThreadChannel:
    surface = "x"

    def __init__(self, tool_factory=None, *, time_fn=time.time) -> None:
        self._factory = tool_factory
        self._tool: Any = None
        self._me = ""
        self._last_poll = 0.0
        self._time = time_fn
        self._dead: Dict[str, float] = {}
        #: since_id -> the oldest post id already read above an unread gap. Set when more
        #: than MAX_PAGES pages wait after the cursor; the next reads page the gap
        #: (``until_id``) so the oldest posts come first and the cursor never passes them.
        self._gap_until: Dict[str, str] = {}

    async def _ready_tool(self) -> Any:
        if self._tool is None:
            if self._factory is not None:
                self._tool = self._factory()
            else:
                from core.config import BotConfig
                from polyrob_x.cron_delivery import build_ready_twitter_tool
                self._tool = await build_ready_twitter_tool(BotConfig(), None)
        tool = self._tool
        if getattr(tool, "client", None) is None and hasattr(tool, "initialize"):
            await tool.initialize()
        return tool

    async def _me_id(self, tool: Any) -> str:
        if not self._me:
            self._me = str(await tool.get_authenticated_user_id() or "")
        return self._me

    def agent_id(self) -> str:
        return self._me

    async def poll(self, since_id: Optional[str]) -> Tuple[List[Any], Optional[str]]:
        from polyrob.sdk import ThreadUnavailable
        now = self._time()
        if now - self._last_poll < POLL_EVERY_S:
            return [], since_id
        self._last_poll = now
        tool = await self._ready_tool()
        if not getattr(tool, "_enabled", True) or getattr(tool, "client", None) is None:
            raise ThreadUnavailable("x: the API is not configured here")
        me = await self._me_id(tool)
        if not me:
            raise ThreadUnavailable("x api: the agent's own account could not be read (401?)")
        kw = dict(id=me, max_results=MAX_RESULTS, tweet_fields=TWEET_FIELDS)
        gap = self._gap_until.get(str(since_id)) if since_id else None
        if since_id:
            kw["since_id"] = str(since_id)
        if gap:
            kw["until_id"] = gap
        posts, newest, token = [], None, None
        for _page in range(MAX_PAGES):
            try:
                resp = await tool._make_request(func=tool.client.get_users_mentions,
                                                endpoint_type="tweets",
                                                **(dict(kw, pagination_token=token)
                                                   if token else kw))
            except Exception as exc:
                raise ThreadUnavailable(
                    f"x api: mentions read refused ({str(exc)[:160]})") from None
            posts += [to_post(t) for t in (getattr(resp, "data", None) or ())]
            meta = getattr(resp, "meta", None) or {}
            meta = meta if isinstance(meta, dict) else {}
            newest = newest or meta.get("newest_id")
            token = meta.get("next_token")
            # With no cursor (the first read) one page is enough: older posts predate it.
            if not token or not since_id:
                break
        else:
            # More than MAX_PAGES pages since the cursor (X lists newest first, so the
            # oldest are the ones not read). Return nothing and keep the cursor: the next
            # read pages the older part (``until_id``) until the oldest window fits, and
            # that window goes out first. Nothing after the cursor is ever skipped.
            posts.sort(key=lambda p: int(p.post_id) if p.post_id.isdigit() else 0)
            if self._gap_until.get(str(since_id)) == "":
                # The gap below these pages was read and is empty (deleted posts): these
                # are the oldest after the cursor, so they go out now.
                self._gap_until.pop(str(since_id), None)
                return posts, newest or (posts[-1].post_id if posts else since_id)
            if posts:
                self._gap_until[str(since_id)] = posts[0].post_id
            logger.warning("x thread: over %d mentions since %s; reading the oldest first",
                           MAX_PAGES * MAX_RESULTS, since_id)
            return [], since_id
        posts.sort(key=lambda p: int(p.post_id) if p.post_id.isdigit() else 0)
        if gap:
            # The oldest window is read whole: its newest post is the new cursor (the
            # posts above the gap are read again after it, from that cursor).
            if not posts:
                self._gap_until[str(since_id)] = ""  # empty gap: the window above goes next
                return [], since_id
            self._gap_until.pop(str(since_id), None)
            return posts, posts[-1].post_id
        return posts, newest or (posts[-1].post_id if posts else since_id)

    async def lookup(self, post_id: str) -> Optional[Any]:
        try:
            tool = await self._ready_tool()
            await self._me_id(tool)
            resp = await tool._make_request(func=tool.client.get_tweet, endpoint_type="tweets",
                                            id=str(post_id), tweet_fields=TWEET_FIELDS)
            data = getattr(resp, "data", None)
            return to_post(data) if data is not None else None
        except Exception:
            logger.info("x thread: post %s could not be read", post_id, exc_info=True)
            return None

    def can_reply(self) -> bool:
        from polyrob_x.write_budget import share_available
        return share_available()

    async def reply(self, post_id: str, text: str, *, subject: str = "",
                    user_id: str = "") -> bool:
        dead_at = self._dead.get(str(post_id))
        if dead_at is not None:
            if self._time() - dead_at < DEAD_TTL_S:
                return False
            self._dead.pop(str(post_id), None)
        from polyrob.sdk import delivery_context, is_owner_tenant
        from polyrob_x.twitter_tool import _OWNER_APPROVED_ACTION, TwitterReplyAction
        from tools.controller.approval import operator_denies_approvals
        tool = await self._ready_tool()
        ctx = delivery_context(SimpleNamespace(session_id="commerce:x", user_id=user_id))
        token = (_OWNER_APPROVED_ACTION.set("twitter_reply")
                 if user_id and is_owner_tenant(ctx) and not operator_denies_approvals()
                 else None)
        try:
            res = await tool.twitter_reply(TwitterReplyAction(tweet_id=str(post_id),
                                                              text=text[:1200]),
                                           execution_context=ctx)
        except Exception as exc:
            res = SimpleNamespace(error=str(exc))
        finally:
            if token is not None:
                _OWNER_APPROVED_ACTION.reset(token)
        err = str(getattr(res, "error", "") or "")
        ok = not err
        if not ok and any(k in err.lower() for k in _REFUSED_403):
            self._dead[str(post_id)] = self._time()
            logger.warning("x thread: X refuses replies under %s (403); it is not retried",
                           post_id)
        elif not ok:
            logger.info("x thread: reply under %s refused: %s", post_id, err[:200])
        try:
            from polyrob.sdk import record_external_write
            record_external_write(effect="social", tool="twitter", action="twitter_reply",
                                  target="open", surface="x", autonomous=True,
                                  outcome="ok" if ok else "error", user_id=user_id,
                                  session_id="commerce:x", fingerprint=text,
                                  subject=subject or str(post_id))
        except Exception:
            logger.debug("x thread: the reply record failed", exc_info=True)
        return ok


_CHANNEL: Optional[XThreadChannel] = None


def channel() -> XThreadChannel:
    """The factory the ``commerce.thread_channel`` hook calls (one per process)."""
    global _CHANNEL
    if _CHANNEL is None:
        _CHANNEL = XThreadChannel()
    return _CHANNEL


__all__ = ["POLL_EVERY_S", "TWEET_FIELDS", "XThreadChannel", "channel", "to_post"]
