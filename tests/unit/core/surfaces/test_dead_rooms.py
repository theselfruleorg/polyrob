"""044 T21: a bot that was kicked out of a room stops retrying.

Every surface collapses its send exception to a plain string, so "Forbidden: bot
was kicked from the supergroup chat" looked exactly like a timeout: the router
retried it on every later room message, forever, and `/groups list` kept showing
the room as active.

Two records, one classification:

- ``GroupAllowlist.mark_left`` — the room's own row goes ``left``. ``is_allowed``
  is unchanged (``left`` is not ``active``), so ingress stops too, and the owner
  can SEE why in ``/groups list``.
- ``DeadTargetStore.mark`` — the outbound-side latch the router already keeps.

And the router consults ``is_dead`` FIRST — before the room hourly cap and before
the durable outbound queue — so a dead room neither burns the room's reply budget
nor piles up queue rows nothing can ever deliver.
"""
import asyncio

import pytest

from core.surfaces.dead_targets import DeadTargetStore, classify_dead_error
from core.surfaces.envelopes import MessageKind, OutboundMessage, SendResult
from core.surfaces.group_allowlist import GroupAllowlist
from core.surfaces.message_router import MessageRouter
from core.surfaces.room_caps import RoomCaps

ROOM_KEY = "agent:main:telegram:supergroup:-1001"
KICKED = "Telegram server says - Forbidden: bot was kicked from the supergroup chat"


# --- the allowlist half ------------------------------------------------------

def test_mark_left_stops_the_room_without_erasing_it(tmp_path):
    al = GroupAllowlist(str(tmp_path / "g.db"))
    al.allow("telegram", "-1")
    al.mark_left("telegram", "-1")
    assert not al.is_allowed("telegram", "-1")
    assert [r["status"] for r in al.list_all()] == ["left"]


def test_mark_left_is_idempotent_and_never_resurrects_a_revoked_room(tmp_path):
    al = GroupAllowlist(str(tmp_path / "g.db"))
    al.allow("telegram", "-1")
    al.mark_left("telegram", "-1")
    al.mark_left("telegram", "-1")
    assert [r["status"] for r in al.list_all()] == ["left"]
    al.revoke("telegram", "-2")  # never allowed at all
    assert al.mark_left("telegram", "-2") is False


def test_the_owner_can_re_allow_a_room_after_a_rejoin(tmp_path):
    al = GroupAllowlist(str(tmp_path / "g.db"))
    al.allow("telegram", "-1")
    al.mark_left("telegram", "-1")
    al.allow("telegram", "-1")
    assert al.is_allowed("telegram", "-1")


# --- the classifier ----------------------------------------------------------

@pytest.mark.parametrize("text,reason", [
    (KICKED, "kicked"),
    ("Bad Request: chat not found", "chat_not_found"),
    ("Forbidden: CHAT_WRITE_FORBIDDEN", "write_forbidden"),
    ("Forbidden: bot is not a member of the supergroup chat", "not_a_member"),
])
def test_a_room_death_is_classified(text, reason):
    assert classify_dead_error("telegram", text) == reason


@pytest.mark.parametrize("text", [
    "Forbidden: bots can't send messages to bots",  # not a liveness signal
    "Request timeout",
    "Too Many Requests: retry after 12",
])
def test_a_non_liveness_failure_is_never_classified(text):
    assert classify_dead_error("telegram", text) is None


# --- the router --------------------------------------------------------------

class _Registry:
    def __init__(self, db_path):
        self.db_path = db_path

    def resolve(self, key):
        return {"surface_id": "telegram", "chat_id": "-1001"}


class _Surface:
    def __init__(self, result):
        self.result = result
        self.sends = 0

    async def send(self, msg):
        self.sends += 1
        return self.result


class _Queue:
    def __init__(self):
        self.rows = []

    def enqueue(self, **kw):
        self.rows.append(kw)


def _router(tmp_path, surface):
    al = GroupAllowlist(str(tmp_path / "group_allowlist.db"))
    al.allow("telegram", "-1001")
    router = MessageRouter(_Registry(str(tmp_path / "surfaces.db")))
    router.subscribe("telegram", surface)
    router.attach_dead_targets(DeadTargetStore(str(tmp_path / "dead_targets.db")))
    router.attach_room_caps(RoomCaps(str(tmp_path / "surfaces.db")))
    return router, al


def _msg(text="hi"):
    return OutboundMessage(session_key=ROOM_KEY, text=text,
                           kind=MessageKind.AGENT_TEXT, partial=False)


@pytest.fixture(autouse=True)
def _flags(monkeypatch, tmp_path):
    monkeypatch.setenv("DEAD_TARGET_REGISTRY", "true")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))


def test_a_kicked_room_is_marked_left_and_dead(tmp_path):
    surface = _Surface(SendResult(success=False, error=KICKED))
    router, al = _router(tmp_path, surface)
    asyncio.run(router.publish(_msg()))
    assert surface.sends == 1
    assert router._dt.is_dead("telegram", "-1001")
    assert not al.is_allowed("telegram", "-1001")
    assert [r["status"] for r in al.list_all()] == ["left"]


def test_the_next_reply_never_reaches_the_surface(tmp_path):
    surface = _Surface(SendResult(success=False, error=KICKED))
    router, _al = _router(tmp_path, surface)
    asyncio.run(router.publish(_msg("first")))
    asyncio.run(router.publish(_msg("second")))
    assert surface.sends == 1, "a dead room must not be retried"


def test_a_dead_room_never_burns_the_hourly_reply_cap(tmp_path):
    surface = _Surface(SendResult(success=False, error=KICKED))
    router, _al = _router(tmp_path, surface)
    asyncio.run(router.publish(_msg("first")))
    before = router._room_caps.replies_since("telegram", "-1001", 0)
    asyncio.run(router.publish(_msg("second")))
    assert router._room_caps.replies_since("telegram", "-1001", 0) == before == 0


def test_a_dead_room_is_never_enqueued(tmp_path, monkeypatch):
    """The `is_dead` gate must run BEFORE the durable queue, or a kicked room
    accumulates rows the dispatcher can only ever fail to deliver."""
    monkeypatch.setenv("OUTBOUND_QUEUE_ENABLED", "true")
    surface = _Surface(SendResult(success=False, error=KICKED))
    router, _al = _router(tmp_path, surface)
    router._dt.mark("telegram", "-1001", "kicked")
    queue = _Queue()
    router.attach_queue(queue)
    asyncio.run(router.publish(_msg()))
    assert queue.rows == []
    assert surface.sends == 0


def test_a_dm_death_marks_dead_but_never_touches_a_room_row(tmp_path):
    surface = _Surface(SendResult(success=False, error="Forbidden: bot was blocked by the user"))
    router, al = _router(tmp_path, surface)
    asyncio.run(router.publish(OutboundMessage(
        session_key="agent:main:telegram:dm:-1001:rob", text="hi",
        kind=MessageKind.AGENT_TEXT, partial=False)))
    assert router._dt.is_dead("telegram", "-1001")
    assert al.is_allowed("telegram", "-1001"), "a DM death is not a room departure"


def test_a_transient_failure_leaves_the_room_alone(tmp_path):
    surface = _Surface(SendResult(success=False, error="Request timeout"))
    router, al = _router(tmp_path, surface)
    asyncio.run(router.publish(_msg()))
    asyncio.run(router.publish(_msg("again")))
    assert surface.sends == 2
    assert al.is_allowed("telegram", "-1001")


# --- the owner can SEE it ----------------------------------------------------

def test_the_owner_seats_name_a_left_room(tmp_path):
    """A room that stopped answering must be EXPLAINED. Filtering the seats to
    `active` alone made a kicked room vanish, which reads as a room that never
    existed."""
    import types
    from core.status_snapshot import _groups_section
    from core.surfaces import group_admin

    al = GroupAllowlist(str(tmp_path / "group_allowlist.db"))
    al.allow("telegram", "-1001", note="The Den")
    al.mark_left("telegram", "-1001")
    container = types.SimpleNamespace(
        config=types.SimpleNamespace(data_dir=str(tmp_path)),
        get_service=lambda n: None)

    out = group_admin.list_rooms(container, "rob")
    assert "LEFT" in out and "telegram:-1001" in out

    sec = _groups_section("rob", str(tmp_path))
    assert sec.data["left_rooms"] == [{"surface": "telegram", "chat_id": "-1001"}]
    assert any("LEFT" in ln for ln in sec.lines)


def test_a_user_level_death_never_marks_a_room_left(tmp_path):
    """A room cannot block the bot or be deactivated. Marking a room `left` on a
    user-level signal would be inferring a departure from the wrong evidence."""
    surface = _Surface(SendResult(success=False,
                                  error="Forbidden: bot was blocked by the user"))
    router, al = _router(tmp_path, surface)
    asyncio.run(router.publish(_msg()))
    assert router._dt.is_dead("telegram", "-1001")
    assert al.is_allowed("telegram", "-1001")


def test_the_send_message_shim_also_marks_a_kicked_room_left(tmp_path):
    """The cron/`message`-tool path uses a synthetic `direct:` key with no
    chat_type, so the room test there is the ALLOWLIST. Without it, a room the
    bot was kicked from stayed `active` forever in `/groups list`."""
    surface = _Surface(SendResult(success=False, error=KICKED))
    router, al = _router(tmp_path, surface)
    assert asyncio.run(router.send_message("-1001", "report", "telegram")) is False
    assert router._dt.is_dead("telegram", "-1001")
    assert not al.is_allowed("telegram", "-1001")


def test_the_shim_never_marks_a_non_room_left(tmp_path):
    surface = _Surface(SendResult(success=False, error=KICKED))
    router, al = _router(tmp_path, surface)
    asyncio.run(router.send_message("-9999", "x", "telegram"))
    assert [r["status"] for r in al.list_all()] == ["active"]


def test_a_removed_bot_is_marked_left(tmp_path):
    """Fix round 1 (Minor 6): Telegram says "kicked" for a BAN and "is not a
    member of the" after an ordinary removal — the second is just as final."""
    surface = _Surface(SendResult(
        success=False,
        error="Forbidden: bot is not a member of the supergroup chat"))
    router, al = _router(tmp_path, surface)
    asyncio.run(router.publish(_msg()))
    assert router._dt.is_dead("telegram", "-1001")
    assert not al.is_allowed("telegram", "-1001")


# --- fix round 2: a room with no chat row --------------------------------------

def test_a_room_with_no_chat_row_is_delivered_by_key(tmp_path, caplog):
    """N1: `bind_chat_surface` is the ONLY writer of `session_chat_map`, so a room
    the owner allowlisted but that never ran a LIVE turn has no row. A SERVICE run
    there found work, PAID for the model call, and had every reply dropped at
    DEBUG — while its checkpoint advanced, so the lines were gone. The key IS the
    address; read it back rather than drop."""
    import logging as _logging
    from core.surfaces.session_chat_registry import SessionChatRegistry

    delivered = []

    class _Surface:
        async def send(self, msg):
            delivered.append(msg)
            return SendResult(success=True)

    registry = SessionChatRegistry(str(tmp_path / "surfaces.db"))
    assert registry.resolve(ROOM_KEY) is None
    router = MessageRouter(registry)
    router.subscribe("telegram", _Surface())
    with caplog.at_level(_logging.WARNING):
        assert asyncio.run(router.publish(_msg("yes, live since Tuesday"))) is True
    assert delivered and delivered[0].text == "yes, live since Tuesday"
    assert any("no chat row" in r.getMessage() for r in caplog.records)
    # ...and the live session still owns the row: we did not write one.
    assert registry.resolve(ROOM_KEY) is None


def test_a_key_that_is_not_chat_scoped_is_still_dropped(tmp_path):
    """The fallback reads an ADDRESS, never guesses one."""
    from core.surfaces.session_chat_registry import (
        SessionChatRegistry, row_from_session_key,
    )
    assert row_from_session_key("direct:telegram:-1001") is None
    assert row_from_session_key("agent:main:telegram") is None
    assert row_from_session_key("") is None

    router = MessageRouter(SessionChatRegistry(str(tmp_path / "surfaces.db")))
    router.subscribe("telegram", object())
    assert asyncio.run(router.publish(OutboundMessage(
        session_key="nonsense", text="hi", kind=MessageKind.AGENT_TEXT,
        partial=False))) is False
