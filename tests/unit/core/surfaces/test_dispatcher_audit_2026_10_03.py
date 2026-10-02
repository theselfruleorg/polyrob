"""Interface audit 2026-10-03 — command classification in the dispatcher.

TG6: a ``/verb@botname`` suffix was stripped without asking whose bot it
names, so ``/cancel@OtherBot`` in a shared room cancelled OUR task.

TG8: the ``/h`` and ``/?`` aliases in ``core/verbs.py`` were unknown here;
``/?`` (not command-shaped) became a paid agent turn.
"""
import pytest

from core.surfaces.dispatcher import RouteKind, route_inbound
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource


class _C:
    def get_service(self, name):
        return None


def _inbound(text, *, mentions_bot=None, chat_type="dm"):
    src = SessionSource(surface_id="telegram", chat_id="555", chat_type=chat_type)
    return InboundMessage(text=text, identity=Identity(user_id="u_abc", source=src),
                          mentions_bot=mentions_bot)


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ["/h", "/?", "/H"])
async def test_help_aliases_route_as_help(alias):
    d = await route_inbound(_C(), _inbound(alias))
    assert d.kind == RouteKind.COMMAND
    assert d.command == "/help"


@pytest.mark.asyncio
async def test_a_command_for_another_bot_is_not_ours():
    d = await route_inbound(_C(), _inbound("/cancel@OtherBot", mentions_bot=False))
    assert d.kind == RouteKind.DENIED
    assert getattr(d, "silent", False) is True


@pytest.mark.asyncio
async def test_a_command_for_this_bot_still_routes():
    d = await route_inbound(_C(), _inbound("/cancel@OurBot", mentions_bot=True))
    assert d.kind == RouteKind.COMMAND and d.command == "/cancel"


@pytest.mark.asyncio
async def test_unknown_identity_keeps_the_suffix_strip():
    """No bot identity (mentions_bot None) cannot say whose bot it names."""
    d = await route_inbound(_C(), _inbound("/help@MyRobBot"))
    assert d.kind == RouteKind.COMMAND and d.command == "/help"


@pytest.mark.asyncio
async def test_a_late_loaded_forgeable_surface_is_refused_the_obey_path(monkeypatch):
    """AC6: the dispatcher kept an import-time snapshot of the forgeable set, so
    a pack surface with `forgeable=true` loaded later fell through to the
    legacy obey-path. It now reads `access.is_forgeable_surface` live."""
    from core.surfaces import catalog
    monkeypatch.setattr(catalog, "forgeable_ids", lambda: frozenset({"latemail"}))
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "false")
    src = SessionSource(surface_id="latemail", chat_id="a@b.c", chat_type="dm")
    msg = InboundMessage(text="hello", identity=Identity(user_id="u_x", source=src))
    d = await route_inbound(_C(), msg)
    assert d.kind == RouteKind.DENIED
    assert d.reason == "forgeable_sender"


def test_no_snapshot_of_the_forgeable_set_remains():
    from core.surfaces import dispatcher
    assert not hasattr(dispatcher, "_FORGEABLE_NETWORK_SURFACES")
