"""Where `send_message` goes in an AUTONOMOUS run — and telling the agent so.

Prod 2026-10-09 19:01: the hourly den-engagement cron read The Public Den,
decided to answer ``@EmilyWhale``, and called ``send_message`` instead of
``message(surface=…, target=<den>, …)``. In a goal/cron run ``send_message`` has
exactly one destination — the owner's private chat (§3.1) — so the room reply
landed in the owner's DM. The action result said only "Message sent to user",
so the run recorded "replied in the room", and a later run told the owner
"Nothing went to your inbox" — false.

Two rules live here (``action_registration.py`` is under a shrink-only size
ratchet, so the behaviour is a module of its own):

* :func:`third_party_address_refusal` — an autonomous run's ``send_message``
  whose text OPENS by addressing someone (``@handle …``) is not a report to the
  owner; it is refused, NOT sent, with the remedy (``message``) named.
* :func:`route_status` — when the autonomous rail delivered, the receipt says
  WHERE: the owner's private chat, never a room.

⚠️ Registry-closure landmine: NO ``from __future__ import annotations`` here
(nothing here is an action closure, but the module is imported by one).
"""
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: A text that OPENS by addressing a handle: "@EmilyWhale — …", "@bob, …".
_LEADING_HANDLE = re.compile(r"^\s*@([A-Za-z0-9_]{2,32})\b")

OWNER_PRIVATE_CHAT_RECEIPT = (
    "delivered to your OWNER's private chat only — with no live chat attached "
    "(a scheduled/goal run, or a resumed session) send_message never posts in a "
    "room or to anyone else")


def _autonomous_private_run(controller: Any) -> bool:
    """True for a goal/cron/planner session that is NOT room-bound — the case
    whose send_message falls back to the owner's private chat."""
    try:
        orch = getattr(controller, "orchestrator", None)
        from core.surfaces.room_policy import is_public_session
        if orch is not None and is_public_session(orch):
            return False
        from core.autonomy_marker import is_autonomous
        return is_autonomous(getattr(controller, "session_id", None))
    except Exception:
        logger.debug("send_message route probe failed (fail-open)", exc_info=True)
        return False


def third_party_address_refusal(controller: Any, text: str):
    """A refusal ActionResult, or None when the send may proceed.

    Narrow on purpose: only an autonomous, non-room run, and only a text whose
    FIRST token is an ``@handle``. A report to the owner that merely mentions a
    handle later in the text is untouched.
    """
    m = _LEADING_HANDLE.match(text or "")
    if not m or not _autonomous_private_run(controller):
        return None
    handle = m.group(1)
    from agents.task.loop_types.controller_types import ActionResult
    return ActionResult(
        extracted_content=(
            f"send_message: NOT SENT. This text addresses @{handle}, but in a "
            "scheduled/goal run send_message goes ONLY to your owner's private "
            "chat — it would have landed in the owner's DM, not where you meant. "
            "To answer in a room or to another person use "
            "message(surface=..., target=<room id or address>, text=...). If this "
            "line really is for your owner, send it again without the leading "
            f"@{handle}."),
        include_in_memory=True)


async def route_autonomous_send(controller: Any, text: str) -> Optional[str]:
    """§3.1: deliver an autonomous run's send_message to its own principal.

    Moved verbatim out of ``action_registration.py``. Fail-open — never fails
    the action.
    """
    try:
        from core.surfaces.user_delivery import maybe_deliver_autonomous_send
        outcome = await maybe_deliver_autonomous_send(
            controller.orchestrator, controller.session_id, text)
        if outcome is not None:
            controller.logger.info(f"💬 Autonomous send_message routed to user: {outcome}")
        return outcome
    except Exception as e:
        controller.logger.debug(f"Autonomous send routing failed (fail-open): {e}")
        return None


def route_status(route_outcome: Optional[str]) -> Optional[str]:
    """The honest suffix for send_message's result.

    ``"sent"`` here can only come from the owner rail (an autonomous run, or a
    session with no live mirror; a live mirror returns None), so it names the
    destination; every other outcome keeps
    :func:`tools.controller.emit.describe_route_outcome`'s wording.
    """
    if route_outcome == "sent":
        return OWNER_PRIVATE_CHAT_RECEIPT
    from tools.controller.emit import describe_route_outcome
    return describe_route_outcome(route_outcome)
