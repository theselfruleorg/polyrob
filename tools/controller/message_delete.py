"""0008: `message(action="delete" | "posts")` — name what the agent posted, and
delete it on the owner's instruction.

The post ledger (``core/surfaces/sent_posts.py``, written by the router when a
send LANDS) is the ownership proof: a message id that is not on a ledger row for
that chat is refused as "not one of my own posts", even where the bot is an
admin that Telegram would let delete anybody's message.

Gates, in order, each NAMED on refusal: an owner turn — the owner TENANT, not a
sub-agent/leaf, not a forged, room or autonomous turn; a missing context refuses
(both verbs, since ``posts`` shows previews of every chat incl. the owner's DM); the ledger (row exists, same chat, not already
deleted); the surface's own window (Telegram: 48 h) — checked before the call so
the refusal is ours and exact; then the surface itself, whose refusal (e.g. no
admin right in a channel) is returned verbatim and leaves the row intact.
"""
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

#: Bot API ``deleteMessage``: "a message can only be deleted if it was sent less
#: than 48 hours ago". A surface absent here has no window we know of.
DELETE_WINDOW_SEC = {"telegram": 48 * 3600}
#: ``last=N`` is a bulk delete; bounded so one call cannot wipe a chat's history.
MAX_LAST = 10


def _fail(surface, target, error: str, **extra) -> dict:
    return {"success": False, "tier": None, "surface": surface, "target": target,
            "error": error, **extra}


def _owner_turn_refusal(execution_context, controller, *, verb: str) -> Optional[str]:
    """None only for a genuine owner turn. Fail-CLOSED.

    Two existing statements, both required: ``owner_turn_refusal`` (owner
    tenant, not sub-agent/leaf, not a self-wake re-entry, context present) and
    ``_is_forged_or_autonomous_turn`` (adds the room turn and the autonomous
    goal/cron session)."""
    try:
        from core.security.owner_turn import owner_turn_refusal
        why = owner_turn_refusal(execution_context, verb=verb,
                                 does="touches the agent's posts",
                                 public="list or delete the agent's posts")
        if why:
            return f"{why} — it needs the owner's instruction in this turn"
        from tools.controller.turn_origin import _is_forged_or_autonomous_turn
        if not _is_forged_or_autonomous_turn(execution_context, controller):
            return None
    except Exception:
        logger.debug("post owner-turn probe failed (fail-closed)", exc_info=True)
    return (f"{verb} needs the owner's instruction in this turn — an autonomous, "
            "delegated or room turn cannot. Tell the owner which post and let them ask.")


def _is_gone(reason: str) -> bool:
    """Telegram's "already gone" — removed by hand, so the post IS deleted."""
    return "message to delete not found" in (reason or "").lower()


def _age_refusal(post, now: float) -> Optional[str]:
    window = DELETE_WINDOW_SEC.get(post.surface)
    if window is None or now - post.ts <= window:
        return None
    hours = int((now - post.ts) // 3600)
    return (f"post #{post.row} is {hours} h old — {post.surface} only lets a bot delete "
            f"its own message within {window // 3600} h. It has to be removed by hand "
            "by a chat admin.")


def _ledger(router):
    return getattr(router, "sent_posts", None) if router is not None else None


def _chat_key(router, surface, target, owner_targets):
    from tools.controller.message_send import resolve_owner_alias
    from core.surfaces.outbound_target import normalize_surface_target
    raw = resolve_owner_alias(router, surface, target, owner_targets)
    return normalize_surface_target(surface, raw) if raw else raw


async def perform_message_delete(*, router, surface, target=None, owner_targets=None,
                                 message_id=None, post=None, last=None,
                                 execution_context=None, controller=None,
                                 now: Optional[float] = None) -> dict:
    refusal = _owner_turn_refusal(execution_context, controller, verb="deleting a post")
    if refusal:
        return _fail(surface, target, refusal, deleted_posts=[])
    store = _ledger(router)
    if store is None:
        return _fail(surface, target, "no post ledger in this process — I cannot "
                     "tell which messages are mine, so I delete none", deleted_posts=[])
    res = await _delete(router, store, surface, target, owner_targets,
                        message_id, post, last,
                        time.time() if now is None else now)
    res.setdefault("deleted_posts", [])
    return res


async def _delete(router, store, surface, target, owner_targets, message_id, post,
                  last, now: float) -> dict:
    if post is not None:
        try:
            p = store.get(int(post))
        except (TypeError, ValueError):
            p = None
        if p is None:
            return _fail(surface, target, f"no post #{post} in my post ledger (rows are "
                         "kept 14 days; `message(action='posts')` lists them)")
        # A row deletes in ITS chat; a target that names another chat is a
        # mismatch, never "delete over there instead" (review 0008 #1).
        if target and p.chat_id != _chat_key(router, surface, target, owner_targets):
            return _fail(surface, target, f"post #{post} is in {p.surface}:{p.chat_id}, "
                         f"not {surface}:{target}")
        posts = [p]
    elif message_id is not None:
        if not target:
            return _fail(surface, target, "deleting by message_id needs the chat it is "
                         "in (target=…); or pass post=<ledger row>")
        chat = _chat_key(router, surface, target, owner_targets)
        p = store.find(surface, chat, message_id)
        if p is None:
            return _fail(surface, target, f"message {message_id} in {surface}:{target} is "
                         "not one of my own posts in the post ledger — I only delete what "
                         "I sent")
        posts = [p]
    elif last is not None:
        if not target:
            return _fail(surface, target, "last=N needs the chat (target=…)")
        try:
            n = int(last)
        except (TypeError, ValueError):
            n = 0
        if not 1 <= n <= MAX_LAST:
            return _fail(surface, target, f"last must be a number 1..{MAX_LAST}")
        chat = _chat_key(router, surface, target, owner_targets)
        posts = store.recent(surface, chat, n)
        if not posts:
            return _fail(surface, target, f"I have no undeleted posts to {surface}:{target} "
                         "in the post ledger")
    else:
        return _fail(surface, target, "say which post: post=<ledger row>, message_id=<id> "
                     "with target, or last=N with target")

    deleted, problems = [], []
    for p in posts:
        if p.deleted_ts is not None:
            problems.append(f"post #{p.row} was already deleted")
            continue
        if p.surface != surface:
            problems.append(f"post #{p.row} is on {p.surface}, not {surface}")
            continue
        why = _age_refusal(p, now)
        if why:
            problems.append(why)
            continue
        # A message id names the POST it belongs to: every chunk/media item of
        # that post goes, so the row's deleted stamp is never half-true.
        failed = []
        for mid in p.message_ids:
            ok, reason = await router.delete_message(surface, p.chat_id, mid)
            if not ok and not _is_gone(reason):
                failed.append(f"{mid}: {reason or 'refused'}")
        if failed:
            problems.append(f"post #{p.row}: {surface} refused — " + "; ".join(failed))
            continue
        try:
            store.mark_deleted(p.row, now=now)
        except Exception:
            logger.debug("post ledger mark_deleted failed", exc_info=True)
        deleted.append(p.row)

    res = {"success": bool(deleted) and not problems, "tier": None, "surface": surface,
           "target": target, "deleted_posts": deleted,
           "error": "; ".join(problems) if problems else None}
    if deleted and problems:
        res["note"] = f"deleted {len(deleted)} of {len(posts)} posts"
    return res


async def perform_message_posts(*, router, surface, target=None, owner_targets=None,
                                last=None, execution_context=None, controller=None,
                                now: Optional[float] = None) -> dict:
    """What I posted, newest first — each with its ledger row, chat, age, ids and
    whether it can still be deleted. Read-only, but OWNER turns only: the
    previews span every chat, the owner's DM included."""
    refusal = _owner_turn_refusal(execution_context, controller, verb="listing my posts")
    if refusal:
        return _fail(surface, target, refusal)
    store = _ledger(router)
    if store is None:
        return _fail(surface, target, "no post ledger in this process")
    now = time.time() if now is None else now
    try:
        n = max(1, min(int(last or MAX_LAST), 50))
    except (TypeError, ValueError):
        n = MAX_LAST
    chat = _chat_key(router, surface, target, owner_targets) if target else None
    rows = []
    for p in store.recent(surface, chat, n):
        window = DELETE_WINDOW_SEC.get(p.surface)
        rows.append({"post": p.row, "chat": p.chat_id, "message_ids": p.message_ids,
                     "age_h": round((now - p.ts) / 3600, 1), "preview": p.preview,
                     "deletable": window is None or now - p.ts <= window})
    return {"success": True, "tier": None, "surface": surface, "target": target,
            "posts": rows, "error": None}


__all__ = ["DELETE_WINDOW_SEC", "MAX_LAST", "perform_message_delete",
           "perform_message_posts"]
