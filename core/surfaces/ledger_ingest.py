"""044 T13: the ONE place an inbound becomes a ledger row. Runs before every
gate except the allowlist, so a line that triggers nothing is still context."""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from core.surfaces.group_ledger import LedgerRow

logger = logging.getLogger(__name__)


def _allowed(container: Any, surface: str, chat_id: str) -> bool:
    try:
        from core.surfaces.group_allowlist import GroupAllowlist
        from core.runtime_paths import data_dir_or_home
        cfg = getattr(container, "config", None)
        data_dir = data_dir_or_home(getattr(cfg, "data_dir", None))
        return GroupAllowlist(os.path.join(data_dir, "group_allowlist.db")).is_allowed(surface, chat_id)
    except Exception as e:
        logger.debug("ledger ingest allowlist probe failed (skip): %s", e)
        return False


#: Telegram service (event) messages, and what a ledger line should say happened.
#: 044 M-a: these arrive with NO text, so they were appended as `kind="text"` with
#: an empty body — a blank attributed line in the room context ("@name|id
#: (member): "), which reads to the model as a member who said nothing, and in a
#: `listen`-mode room is indistinguishable from a message that failed to parse.
_SERVICE_EVENTS = (
    ("new_chat_members", "joined the room"),
    ("left_chat_member", "left the room"),
    ("new_chat_title", "changed the room title"),
    ("new_chat_photo", "changed the room photo"),
    ("delete_chat_photo", "removed the room photo"),
    ("pinned_message", "pinned a message"),
    ("group_chat_created", "created the group"),
    ("supergroup_chat_created", "created the supergroup"),
    ("channel_chat_created", "created the channel"),
    ("message_auto_delete_timer_changed", "changed the auto-delete timer"),
    ("migrate_to_chat_id", "migrated the chat"),
    ("migrate_from_chat_id", "migrated the chat"),
)

#: Surfaces already warned that their rooms produce no ledger rows (044 I12).
_NO_MESSAGE_ID_WARNED: set = set()


def _service_event(msg: dict) -> str:
    """The event this service message announces, or "" for an ordinary message."""
    for field, label in _SERVICE_EVENTS:
        if msg.get(field) is not None:
            return label
    return ""


def _warn_surface_has_no_message_id(surface_id: str) -> None:
    """044 I12: ONE warning per surface, naming it.

    The ingest reads a Telegram-shaped `raw` (`message.message_id`). A Discord,
    Slack or Signal room therefore produced NO ledger rows at all — every line
    returned False at DEBUG — so the room context block was permanently empty and
    the service job permanently found "no change". Silent on those surfaces is
    exactly the failure mode 044 T9 forbids."""
    try:
        if surface_id in _NO_MESSAGE_ID_WARNED:
            return
        _NO_MESSAGE_ID_WARNED.add(surface_id)
        logger.warning(
            "room ledger: no message_id in a %s inbound — the ledger ingest reads a "
            "TELEGRAM-shaped raw update, so %s rooms record NO lines: the room context "
            "block stays empty and the service job always reports no-change. Rooms are "
            "Telegram-only until a %s-shaped id is threaded through.",
            surface_id, surface_id, surface_id)
    except Exception:  # pragma: no cover - a logging fault must not drop the line
        pass


def record_inbound_to_ledger(container: Any, inbound: Any, *, role: str, is_owner: bool) -> bool:
    src = getattr(getattr(inbound, "identity", None), "source", None)
    if src is None or (src.chat_type or "dm") == "dm":
        return False
    ledger = container.get_service("group_ledger") if container else None
    if ledger is None or not _allowed(container, src.surface_id, str(src.chat_id)):
        return False
    raw = inbound.raw or {}
    msg = (raw.get("message") or raw.get("edited_message") or raw.get("channel_post")
           or raw.get("edited_channel_post") or {})
    frm = msg.get("from") or {}
    uname = frm.get("username")
    name = f"@{uname}" if uname else (frm.get("first_name") or inbound.identity.display_name or "")
    mid = msg.get("message_id")
    if mid is None:
        _warn_surface_has_no_message_id(str(src.surface_id or "?"))
        return False
    media = getattr(inbound, "media", None) or []
    text = inbound.text or ""
    event = _service_event(msg)
    if event:
        # 044 M-a: a join/leave/title change is recorded as what it IS, with the
        # event as its text, so the room context reads "@name|id (member): joined
        # the room" instead of a blank attributed line.
        kind, text = "service", event
    elif raw.get("edited_message") or raw.get("edited_channel_post"):
        kind = "edit"
    elif media:
        kind = "media"
    elif not text.strip():
        # Neither text, nor media, nor a service event we can name. There is
        # nothing to attribute to this sender — never write a blank line.
        logger.debug("room ledger: skipping an empty non-service message %s", mid)
        return False
    else:
        kind = "text"
    # 044 §4.1: a media row stores the caption (already in `inbound.text`) and the
    # workspace path the inbound rail wrote — never bytes. Ingest runs BEFORE the
    # attachment rail absorbs (only an owner-tier turn absorbs at all), so `path`
    # is usually still None here: the line is then named by kind alone, never
    # dropped.
    media_path = next((p for p in (getattr(m, "path", None) for m in media) if p), None)
    row = LedgerRow(
        surface=src.surface_id, chat_id=str(src.chat_id), thread_id=src.thread_id,
        message_id=str(mid), ts=float(msg.get("date") or time.time()),
        sender_id=str(inbound.identity.raw_user_id or inbound.identity.user_id),
        sender_name=name, sender_is_bot=bool(frm.get("is_bot")),
        role_at_write="owner" if is_owner else role, kind=kind, text=text,
        reply_to_message_id=inbound.reply_to, mentions_bot=bool(inbound.mentions_bot),
        media_path=media_path)
    try:
        ledger.append(row)
        return True
    except Exception as e:
        logger.warning("ledger append failed for %s:%s: %s", src.surface_id, src.chat_id, e)
        return False


def record_outbound_to_ledger(ledger: Any, *, surface: str, chat_id: str,
                              thread_id: Any, text: str, ts: float,
                              reply_to: Any = None, media: bool = False) -> bool:
    """Record what the AGENT said in a room — the other half of the room log.

    ⚠️ Until 2026-09-16 :func:`record_inbound_to_ledger` was the ledger's ONLY
    writer, so ``group_ledger`` held every human line and not one of the agent's
    own. The consequences were not cosmetic:

    * a room session evicted or restarted came back unable to see a single thing
      it had said, because its replies existed nowhere but that session's own
      in-memory history;
    * ``/groups tail`` and the status count showed the owner a half-conversation;
    * a service goal reading from its checkpoint could re-answer a line it had
      already answered, having no record of the answer.

    The row is deliberately shaped like an inbound one (``sender_is_bot=1``,
    ``role_at_write="agent"``) so every existing reader — the context renderer,
    the owner tail, the service goal — picks it up with no change.

    ``message_id`` is SYNTHETIC (``out:<ts>:<hash>``): the durable-queue path
    returns before any surface has minted a real id, so waiting for one would
    record only the direct-send half. The hash makes the PK idempotent, so a
    retried send updates rather than duplicates.

    Fail-open and boolean: the room log is bookkeeping. A fault here must cost
    context, never the message that was already delivered.
    """
    if ledger is None or not surface or not str(chat_id or ""):
        return False
    body = (text or "").strip()
    if not body and not media:
        return False
    try:
        stamp = float(ts)
        mid = f"out:{stamp:.3f}:{abs(hash(body)) & 0xffffffff:08x}"
        ledger.append(LedgerRow(
            surface=str(surface), chat_id=str(chat_id),
            thread_id=(str(thread_id) if thread_id else None),
            message_id=mid, ts=stamp,
            sender_id="agent", sender_name="you", sender_is_bot=True,
            role_at_write="agent", kind=("media" if media else "text"),
            text=body,
            reply_to_message_id=(str(reply_to) if reply_to else None),
            mentions_bot=False))
        return True
    except Exception as e:
        logger.warning("room ledger: failed to record the agent's own reply for "
                       "%s:%s: %s — this room's log now shows only one side",
                       surface, chat_id, e)
        return False
