"""The ONE verb -> Telegram call adapter for 046 paid room actions.

Both callers go through it, so they can never diverge:

* `core.surfaces.room_actions.apply` — the paid path, after settlement;
* `surfaces.telegram.group_ops._apply_free` — the owner/admin path, no invoice.

Before this existed, the free path called ``restrict_member`` directly and
rendered "muted" whatever verb was resolved, and the paid path could reach no
transport at all (the settlement watcher's ``_room_moderator`` seam had zero
production writers, so EVERY settled action was credited instead of applied).

⚠️ The bot is resolved LAZILY, per call. The surface outlives a reconnect and the
``Bot`` object does not, so a moderator that captured one at construction would
be holding a dead handle by the time a payment lands.

⚠️ Never raises. The caller owes the payer a credit on failure and can only write
one if it is TOLD about the failure instead of being unwound by it.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class RoomModerator:
    """Apply one catalog effect in one Telegram chat.

    Registered as the container service ``room_moderator``
    (`surfaces/telegram/harness.py::TelegramHarness.start`), which is what
    `room_actions.apply` resolves when no ``perform_fn`` is injected.
    """

    def __init__(self, surface: Any = None, bot: Any = None) -> None:
        self._surface = surface
        self._bot = bot

    def _resolve_bot(self) -> Optional[Any]:
        if self._bot is not None:
            return self._bot
        surface = self._surface
        return (getattr(surface, "_bot", None) or getattr(surface, "bot", None))

    async def member_status_async(self, surface: str, chat_id: str,
                                  user_id: str) -> str:
        """A TARGET's live status, awaited on the CALLER's loop.

        ⚠️ The `_sync` twin below bridges to `core.async_bridge`'s background
        loop, and the aiogram session is bound to whichever loop the harness or
        the autonomy runtime is running — so the bridged call raised "got
        Future attached to a different loop" and every target read as PROTECTED
        (Playground Env, 2026-09-15). Every async caller must use THIS one.
        """
        bot = self._resolve_bot()
        if bot is None:
            raise RuntimeError("no chat connection to read member status with")
        from surfaces.telegram.moderation import member_status
        return await member_status(bot, chat_id, user_id)

    def member_status_sync(self, surface: str, chat_id: str, user_id: str) -> str:
        """A TARGET's live status in the chat, synchronously (046 T5).

        ⚠️ ONLY for a caller with no running event loop. From async code use
        `member_status_async` — bridging an aiogram call to another loop is the
        2026-09-15 outage.

        Read by `room_actions._target_protection` at SETTLEMENT time, where the
        caller is async but the protection helper is not. Raises on any fault —
        the protection helper reads a raised probe as PROTECTED, so an
        unreadable status refuses rather than letting an administrator through.
        """
        bot = self._resolve_bot()
        if bot is None:
            raise RuntimeError("no chat connection to read member status with")
        from core.async_bridge import run_coroutine_sync
        from surfaces.telegram.moderation import member_status
        return run_coroutine_sync(member_status(bot, chat_id, user_id))

    async def __call__(self, offer: Any, effect: Any, until_ts: int):
        from surfaces.telegram.moderation import (
            ModResult, ban_member, restrict_member, unban_member,
            unrestrict_member,
        )
        bot = self._resolve_bot()
        if bot is None:
            return ModResult(False, "no chat connection was available")
        verb = getattr(effect, "verb", "") or getattr(offer, "verb", "")
        chat_id = getattr(offer, "chat_id", "")
        user_id = getattr(offer, "target_user_id", "")
        try:
            if verb == "mute":
                return await restrict_member(bot, chat_id, user_id,
                                             until_ts=until_ts)
            if verb == "unmute":
                return await unrestrict_member(bot, chat_id, user_id)
            if verb == "ban":
                return await ban_member(bot, chat_id, user_id, until_ts=until_ts)
            if verb == "unban":
                return await unban_member(bot, chat_id, user_id)
        except Exception as e:      # pragma: no cover - the primitives catch
            logger.warning("room moderator: %s raised (%s)", verb, e)
            return ModResult(False, str(e))
        # ⚠️ No default action. A verb this adapter does not know is a refusal,
        # never "the nearest thing" — the free path used to mute whatever it was
        # asked for.
        return ModResult(False, f"I do not know how to apply {verb!r}")


def install_room_moderator(container: Any, surface: Any) -> bool:
    """Register the moderator on *container*. Idempotent, fail-open.

    Returns True when the service is present afterwards.
    """
    try:
        if container is None:
            return False
        if container.get_service("room_moderator") is not None:
            return True
        container.register_service("room_moderator", RoomModerator(surface))
        logger.info("room actions: telegram moderator installed")
        return True
    except Exception as e:
        logger.warning("room actions: could not install the telegram "
                       "moderator (%s) — a paid action would be credited", e)
        return False


__all__ = ["RoomModerator", "install_room_moderator"]
