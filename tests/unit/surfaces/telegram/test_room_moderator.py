"""046 T1: the ONE verb -> Telegram adapter, and the wiring that reaches it.

⚠️ These test the REAL wiring path. Every 046 phase-1 test injected
`perform_fn`, so all 157 passed while production had NO writer for the
settlement watcher's `_room_container`/`_room_moderator` seams and no adapter at
all — which meant every settled paid action was CREDITED instead of applied.
"""
import time

import pytest

from surfaces.telegram.room_moderator import RoomModerator, install_room_moderator


class _Bot:
    def __init__(self):
        self.calls = []

    async def restrict_chat_member(self, **kw):
        self.calls.append(("restrict", kw))

    async def ban_chat_member(self, **kw):
        self.calls.append(("ban", kw))

    async def unban_chat_member(self, **kw):
        self.calls.append(("unban", kw))


class _Row:
    surface = "telegram"
    chat_id = "-100123"
    verb = "mute"
    target_user_id = "9911"
    target_name = "S"
    duration_sec = 3600


def _eff(verb):
    from core.surfaces.room_actions import effect
    return effect(verb)


@pytest.mark.asyncio
@pytest.mark.parametrize("verb,expected", [
    ("mute", "restrict"), ("unmute", "restrict"),
    ("ban", "ban"), ("unban", "unban")])
async def test_each_catalog_verb_reaches_its_own_api_call(verb, expected):
    bot = _Bot()
    row = _Row()
    row.verb = verb
    res = await RoomModerator(bot=bot)(row, _eff(verb), int(time.time()) + 3600)
    assert res.ok, res.reason
    assert [c[0] for c in bot.calls] == [expected]


@pytest.mark.asyncio
async def test_an_unknown_verb_refuses_rather_than_doing_the_nearest_thing():
    """⚠️ The free path used to call `restrict_member` whatever verb it had, and
    render "muted" either way — so adding `/ban` to it would silently have
    muted."""
    bot = _Bot()
    eff = type("E", (), {"verb": "delete_everything"})()
    res = await RoomModerator(bot=bot)(_Row(), eff, 0)
    assert not res.ok and "delete_everything" in res.reason
    assert bot.calls == []


@pytest.mark.asyncio
async def test_no_bot_is_a_typed_refusal_never_a_raise():
    res = await RoomModerator(surface=None, bot=None)(_Row(), _eff("mute"), 0)
    assert not res.ok and "no chat connection" in res.reason


@pytest.mark.asyncio
async def test_the_bot_is_resolved_LAZILY_from_the_surface():
    """⚠️ The surface outlives a reconnect and the Bot object does not, so an
    adapter that captured one at construction would hold a dead handle by the
    time a payment lands."""
    surface = type("S", (), {"_bot": None})()
    moderator = RoomModerator(surface)
    surface._bot = _Bot()       # the reconnect
    res = await moderator(_Row(), _eff("mute"), int(time.time()) + 3600)
    assert res.ok
    assert surface._bot.calls


class _Container:
    def __init__(self):
        self._services = {}

    def get_service(self, name):
        return self._services.get(name)

    def register_service(self, name, value):
        self._services[name] = value


def test_install_registers_the_service_and_is_idempotent():
    c = _Container()
    assert install_room_moderator(c, object())
    first = c.get_service("room_moderator")
    assert isinstance(first, RoomModerator)
    assert install_room_moderator(c, object())
    assert c.get_service("room_moderator") is first


def test_install_is_fail_open_on_a_broken_container():
    class _Bad:
        def get_service(self, name):
            raise RuntimeError("container down")
    assert install_room_moderator(_Bad(), object()) is False


def test_the_telegram_harness_installs_it_at_start():
    """A service nothing registers is a service `apply` can never resolve."""
    import inspect

    from surfaces.telegram.harness import TelegramHarness
    src = inspect.getsource(TelegramHarness.start)
    assert "install_room_moderator" in src


def test_the_settlement_watcher_builder_attaches_the_container():
    """⚠️ `_room_container` declared itself 'assigned after construction by
    whoever wires the watcher' and NOBODY did."""
    import inspect

    from core import autonomy_runtime
    src = inspect.getsource(autonomy_runtime._build_settlement_watcher)
    assert "_room_container" in src
