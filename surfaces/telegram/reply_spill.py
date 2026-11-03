"""096 W5/W8: how a command reply the owner asked for reaches them whole.

* A reply above three messages (D2) arrives as a one-message gist plus the
  whole text as an attached ``.txt`` (D5) — :func:`plan_owner_reply`,
  :func:`send_spill_or_full`.
* A reply that stopped part-way holds only what did NOT arrive for
  ``/missed`` and tells the owner which message is missing —
  :func:`hold_unsent_reply`.

Extracted from ``harness.py`` (god-file ratchet). Every send goes through the
``send`` callable the harness passes in (``harness._send_telegram_text``), so
this module never imports the harness back. Fail-open throughout.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Awaitable, Callable, Optional, Tuple

logger = logging.getLogger(__name__)

Send = Callable[..., Awaitable[Any]]


def record_undelivered_reply(user_id: str, cmd: str, text: str, exc: BaseException) -> None:
    """TG12: a command reply that could not be SENT — after a money verb that
    already executed, the result the owner never saw — becomes an ``owner_notice``
    row `/missed` reads (marker: ``user_delivery.NOTICE_MARKERS[2]``), never
    only a log line. 096 W8: the row is the owner's only copy — kept whole
    (marked if cut). Fail-open."""
    logger.error("telegram: %s reply undelivered (%s) — recorded for /missed",
                 cmd, exc, exc_info=True)
    from core import event_log
    from core.event_kinds import OWNER_NOTICE
    from core.surfaces.user_delivery import NOTICE_MARKERS, notice_text
    # `emit` is the fail-open record (core.event_log) — it never raises.
    event_log.emit(OWNER_NOTICE, source="telegram", user_id=str(user_id or ""),
                   attrs={"text": notice_text(f"{NOTICE_MARKERS[2]}; source=telegram {cmd}] "
                                              f"{str(text or '')}")})


def partial_reply_note(sent: Any, total: Any) -> Tuple[str, str]:
    """What the owner is told when a long reply stopped part-way."""
    try:
        k, n = int(sent) + 1, int(total)
        return (f"Message {k} of {n} of this reply did not arrive",
                f"(the part from message {k} of {n} on)")
    except (TypeError, ValueError):
        return ("The end of this reply did not arrive",
                "(the part that did not arrive)")


def plan_owner_reply(inbound_text: str, cmd: Any, full_text: str, *,
                     in_room: bool) -> Tuple[str, Optional[dict]]:
    """``(text to send, spill entry or None)``. Not in a room: the outbox is the
    owner's. Fail-open to the whole text."""
    if in_room:
        return full_text, None
    try:
        from core.surfaces.spill import maybe_spill_owner_reply
        from core.surfaces.user_delivery import _home_dir_for_container
        words = (inbound_text or "").split()
        src = (words[0].lstrip("/") if words else "") or str(cmd or "reply")
        spilled = maybe_spill_owner_reply(
            full_text, home_dir=_home_dir_for_container(None), source=src.lstrip("/"))
        if spilled is not None:
            return spilled
    except Exception:
        logger.debug("telegram: reply spill skipped (fail-open)", exc_info=True)
    return full_text, None


async def hold_unsent_reply(send: Send, bot: Any, chat_id: Any, user_id: str, cmd: Any,
                            exc: BaseException, *, sent_text: str, full_text: str,
                            spilled: bool) -> None:
    """TG12 + 096 W8: hold what did NOT arrive for ``/missed`` — only that.

    A reply that stopped after message 2 of 3 used to store the WHOLE reply, so
    a resend repeated what the owner already had. The surface reports its
    progress (``remaining_text``, the OB7 rule the outbound queue resumes with —
    ``outbound_dispatcher.unsent_text``); a gist whose send failed holds the
    whole text (its attachment never went)."""
    from core.surfaces.outbound_dispatcher import unsent_text
    label = str(cmd or "command")
    rest = full_text if spilled else unsent_text(sent_text, getattr(
        exc, "remaining_text", None))
    partial = (not spilled) and bool(rest) and rest != sent_text
    where = ""
    if partial:
        where, held = partial_reply_note(getattr(exc, "sent_chunks", None),
                                         getattr(exc, "total_chunks", None))
        rest = f"{held}\n{rest}"
    try:
        record_undelivered_reply(user_id, label, rest or full_text, exc)
    except Exception:
        logger.warning("telegram: %s reply could not be held for /missed",
                       label, exc_info=True)
    if partial:
        try:
            await send(bot, chat_id, f"⚠ {where}. The rest is held for you: /missed")
        except Exception:
            logger.warning("telegram: partial-reply notice not sent", exc_info=True)


async def send_spill_or_full(send: Send, bot: Any, send_media: Callable[..., Awaitable[Any]],
                             chat_id: Any, user_id: str, cmd: Any, entry: dict,
                             full_text: str) -> None:
    """Attach the whole text of a spilled reply. If the file cannot go, the whole
    text goes as messages instead — the gist alone is never the end of it."""
    try:
        path = entry.get("path")
        if not path or not os.path.isfile(path):
            raise FileNotFoundError(str(path))
        await send_media(chat_id, entry)
        return
    except Exception as e:
        logger.warning("telegram: reply attachment failed (%s) — sending the "
                       "whole text instead", e)
    try:
        await send(bot, chat_id, full_text)
    except Exception as e:
        await hold_unsent_reply(send, bot, chat_id, user_id, cmd, e, sent_text=full_text,
                                full_text=full_text, spilled=False)
