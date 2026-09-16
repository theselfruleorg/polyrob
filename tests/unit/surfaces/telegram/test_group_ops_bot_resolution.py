"""`group_ops` can reach the live bot in PRODUCTION, not just in tests.

⚠️ Live incident, Playground Env, 2026-09-15 17:43:49:

    room action: member-status probe failed (no chat connection to read member
    status with) — treating the target as PROTECTED

A plain member tried to buy a ban on another plain member and was told "That
person cannot be targeted here". Nobody was protected: `_bot(task_agent)` read
``task_agent.bot`` / ``task_agent.surface._bot``, and NOTHING sets either.
`harness.py` sets ``self.bot`` on the HARNESS. So the probe raised on every
call, `_target_protection` reads a raised probe as PROTECTED (correctly — an
unreadable status must never let an administrator through), and the paid rail
was dead for every target in every room.

⚠️ `_rights_fn` shares the same resolver and returns an EMPTY set on the same
fault, so the rights gate was dead too — it would simply have refused one step
later. Two guards, one missing wire.

The 046 tests all INJECT `rights_fn`/`target_status_fn`, so none of them ever
exercised `_bot`. These do the opposite: they assert the resolver works with
exactly what production has — a task_agent with neither attribute, and a
container holding the registered `room_moderator`.
"""
import pytest

from surfaces.telegram.group_ops import _bot


class _Bot:
    pass


class _Surface:
    surface_id = "telegram"

    def __init__(self, bot=None):
        self._bot = bot


class _Container:
    def __init__(self, services=None):
        self._s = services or {}

    def get_service(self, name):
        return self._s.get(name)


class _Agent:
    """Production shape: no `.bot`, no `.surface` — just the container."""

    def __init__(self, container=None):
        self.container = container


# --- the production shape --------------------------------------------------

def test_the_bot_is_found_through_the_registered_room_moderator():
    """⚠️ This is prod: harness.start() registers the moderator with the
    surface, and the task_agent carries only the container."""
    from surfaces.telegram.room_moderator import RoomModerator
    bot = _Bot()
    agent = _Agent(_Container({"room_moderator": RoomModerator(_Surface(bot))}))
    assert _bot(agent) is bot


def test_the_bot_is_found_through_the_surface_registry():
    """The other seam the harness fills (`register_surface`)."""
    class _Registry:
        def __init__(self, surface):
            self._surface = surface

        def get(self, sid):
            return self._surface if sid == "telegram" else None

    bot = _Bot()
    agent = _Agent(_Container({"surface_registry": _Registry(_Surface(bot))}))
    assert _bot(agent) is bot


# --- the shapes that already worked ---------------------------------------

def test_a_direct_bot_attribute_still_wins():
    agent = _Agent()
    agent.bot = _Bot()
    assert _bot(agent) is agent.bot


def test_a_surface_attribute_still_works():
    bot = _Bot()
    agent = _Agent()
    agent.surface = _Surface(bot)
    assert _bot(agent) is bot


# --- honest absence --------------------------------------------------------

def test_no_bot_anywhere_is_still_None():
    """⚠️ Must stay None, not a stub. `_target_status_fn` RAISES on None and
    `_target_protection` reads that as PROTECTED — an unreadable status must
    never let a chat administrator be bought a ban on."""
    assert _bot(_Agent(_Container())) is None
    assert _bot(_Agent(None)) is None


def test_a_raising_container_does_not_propagate():
    class _Broken:
        def get_service(self, name):
            raise RuntimeError("container closed")

    assert _bot(_Agent(_Broken())) is None
