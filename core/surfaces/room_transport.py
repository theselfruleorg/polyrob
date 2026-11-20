"""The room transport seam: what a room verb needs from the chat platform (proposal 125 S14).

The decision body of the room verbs (``/groups``, ``/mute``, ``/unmute``, ``/ban``,
``/unban``, ``/paid`` — ``cron/owner_rooms.py``) runs below the seats. It decides who
may act, which room, free or paid, and what the answer says. It never imports a chat
platform. What only the platform can do — read the bot's rights in a chat, read a
member's status, restrict or ban a member, list the platform's own admins, read which
message a line replied to — it asks of the transport the room's surface registered here.

A surface registers ONE factory per surface id at load
(``register_room_transport("telegram", factory)``); the factory takes the object the
seat runs on (the task agent) and returns a :class:`RoomTransport`. The body asks
:func:`room_transport` for the surface of the room it targets.

⚠️ No transport registered (the terminal, a surface with no rooms) is the
:class:`NullRoomTransport`, and every one of its answers is the fail-CLOSED one: no
rights (a sale refuses), an unreadable member status RAISES (the target reads as
protected), an effect is not applied (the caller credits the payer), no reply target
(the member form of a verb refuses). It never pretends a platform call worked.
"""
import logging
import threading
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple, runtime_checkable

logger = logging.getLogger(__name__)


class EffectResult:
    """A typed outcome of one room effect: ``ok`` and, on a failure, the reason.

    A transport returns any object with ``ok`` and ``reason`` (Telegram's
    ``ModResult``); this one is the seam's own, for the null transport."""

    __slots__ = ("ok", "reason")

    def __init__(self, ok: bool, reason: str = "") -> None:
        self.ok = ok
        self.reason = reason


@runtime_checkable
class RoomTransport(Protocol):
    """What a chat platform does in a room, for the room verbs."""

    def connected(self) -> bool:
        """Is there a live platform connection to act with?"""

    def is_admin_owner(self, user_id: str) -> bool:
        """Is this principal the owner? A network surface never honours the local bypass."""

    def reply_target(self, raw: Any) -> Optional[Tuple[str, str, bool]]:
        """``(raw_user_id, display_name, is_bot)`` of the message this raw update
        replied to, or None. A reply is a FACT the platform event carries."""

    def room_title(self, raw: Any) -> str:
        """The room's title from the raw update, or ``""``. Never raises."""

    async def rights(self, chat_id: str) -> set:
        """The rights the agent holds in the room. EMPTY on any fault (fail-closed)."""

    async def member_status(self, chat_id: str, user_id: str) -> str:
        """One member's status in the room. RAISES on any fault (the caller reads
        a raised probe as PROTECTED)."""

    async def apply(self, row: Any, effect: Any, until_ts: int) -> Any:
        """Apply one catalog effect (mute → restrict, unmute → unrestrict, ban, unban)
        to ``row.target_user_id`` in ``row.chat_id``. Never raises; returns ``ok``/``reason``."""

    async def suggest_admins(self, chat_id: str) -> Optional[List[dict]]:
        """The platform's own admin list as ``[{"id", "name"}]`` suggestions, or
        None when this surface cannot list them."""


def _core_is_owner(user_id: str) -> bool:
    from core.instance import is_owner_uid
    return is_owner_uid(user_id, allow_local=False)


class NullRoomTransport:
    """The transport of a surface that registered none. Every answer fails closed."""

    def __init__(self, surface: str = "") -> None:
        self.surface = surface

    def connected(self) -> bool:
        return False

    def is_admin_owner(self, user_id: str) -> bool:
        return _core_is_owner(user_id)

    def reply_target(self, raw: Any) -> Optional[Tuple[str, str, bool]]:
        return None

    def room_title(self, raw: Any) -> str:
        return ""

    async def rights(self, chat_id: str) -> set:
        return set()

    async def member_status(self, chat_id: str, user_id: str) -> str:
        raise RuntimeError("no chat connection to read member status with")

    async def apply(self, row: Any, effect: Any, until_ts: int) -> Any:
        return EffectResult(False, "no chat connection was available")

    async def suggest_admins(self, chat_id: str) -> Optional[List[dict]]:
        return None


_FACTORIES: Dict[str, Callable[[Any], RoomTransport]] = {}
_LOCK = threading.Lock()


def register_room_transport(surface: str, factory: Callable[[Any], RoomTransport]) -> None:
    """Register the factory of *surface*'s room transport. Idempotent: the last wins."""
    key = str(surface or "").strip().lower()
    if not key:
        raise ValueError("a room transport needs a surface id")
    with _LOCK:
        _FACTORIES[key] = factory


def room_transport(surface: Optional[str], task_agent: Any = None) -> RoomTransport:
    """The transport for *surface*, built over *task_agent*; the null one when none
    is registered or the factory fails (logged, never raised)."""
    key = str(surface or "").strip().lower()
    factory = _FACTORIES.get(key)
    if factory is None:
        return NullRoomTransport(key)
    try:
        return factory(task_agent)
    except Exception as e:
        logger.warning("room transport for %r failed to build (%s) — no connection", key, e)
        return NullRoomTransport(key)


#: The verbs whose body (``cron/owner_rooms.py``) acts through a room transport. A
#: chat app that registered none answers each with "no chat connection", so its
#: `/help` does not list them (:func:`seat_has_rooms`).
ROOM_VERBS = frozenset({"/groups", "/mute", "/unmute", "/ban", "/unban", "/paid"})


def seat_has_rooms(seat: Optional[str]) -> bool:
    """Can *seat* run a room verb? A chat surface only when it registered a room
    transport; an owner control seat that is not a chat surface (the terminal,
    the console) runs it through the surface that has one."""
    key = str(seat or "").strip().lower()
    if key in _FACTORIES:
        return True
    try:
        from core.surfaces.catalog import get
        return get(key) is None
    except Exception:
        return False


def registered_surfaces() -> List[str]:
    """The surface ids with a registered room transport (for a status read)."""
    return sorted(_FACTORIES)


__all__ = ["EffectResult", "NullRoomTransport", "ROOM_VERBS", "RoomTransport",
           "register_room_transport", "registered_surfaces", "room_transport", "seat_has_rooms"]
