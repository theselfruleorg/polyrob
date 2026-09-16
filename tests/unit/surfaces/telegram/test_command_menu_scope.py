"""The "/" command menu is owner-private — never a global Telegram menu.

`set_my_commands` with no `scope=` writes Telegram's DEFAULT scope, which is
served to EVERY user who opens the bot and which Telegram keeps serving until
something overwrites it. On an owner-locked deploy that showed a stranger the
whole admin verb list (/wallet, /trade, /deploy, /halt …), and kept showing a
retired verb long after the build that published it was gone.

These tests pin the two halves of the fix: every broad scope is CLEARED on each
start, and the verb list is published to the owner seats only.
"""
import asyncio

import pytest

from surfaces.telegram.harness import (TelegramHarness, _menu_chat_ids,
                                       help_commands)


class _FakeBot:
    def __init__(self):
        self.set_calls = []      # (commands, scope)
        self.deleted_scopes = []

    async def set_my_commands(self, commands, scope=None, **kw):
        self.set_calls.append((commands, scope))
        return True

    async def delete_my_commands(self, scope=None, **kw):
        self.deleted_scopes.append(scope)
        return True


def _harness(bot):
    h = TelegramHarness.__new__(TelegramHarness)
    h.bot = bot
    return h


def _publish(bot):
    asyncio.run(_harness(bot)._publish_command_menu())


@pytest.fixture
def owner_env(monkeypatch):
    monkeypatch.delenv("POLYROB_OWNER_TELEGRAM_ID", raising=False)
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "424242")
    return "424242"


# --- the menu is never global -------------------------------------------------

def test_broad_scopes_are_cleared_on_every_start(owner_env):
    bot = _FakeBot()
    _publish(bot)
    cleared = {getattr(s, "type", None) for s in bot.deleted_scopes}
    assert cleared == {"default", "all_private_chats", "all_group_chats"}, cleared


def test_no_publish_is_ever_scopeless(owner_env):
    """A scopeless set_my_commands IS the global menu — the bug itself."""
    bot = _FakeBot()
    _publish(bot)
    assert bot.set_calls, "the owner still needs a menu"
    for _commands, scope in bot.set_calls:
        assert scope is not None
        assert getattr(scope, "type", None) == "chat"


def test_published_to_the_owner_chat_with_the_help_ssot(owner_env):
    bot = _FakeBot()
    _publish(bot)
    assert len(bot.set_calls) == 1
    commands, scope = bot.set_calls[0]
    assert scope.chat_id == int(owner_env)
    assert [c.command for c in commands] == [n for n, _ in help_commands()]


def test_every_allowlisted_seat_gets_the_menu(monkeypatch):
    """The verbs are gated by the allowlist, so the menu's audience is it."""
    monkeypatch.delenv("POLYROB_OWNER_TELEGRAM_ID", raising=False)
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "111, 222")
    bot = _FakeBot()
    _publish(bot)
    assert [scope.chat_id for _c, scope in bot.set_calls] == [111, 222]


def test_no_owner_id_means_cleared_and_not_published(monkeypatch):
    """Bootstrap mode (no allowlist): the bot runs nothing, so nobody gets a menu —
    but the stale global list is still cleared."""
    monkeypatch.delenv("POLYROB_OWNER_TELEGRAM_ID", raising=False)
    monkeypatch.delenv("ALLOWED_TELEGRAM_USER_IDS", raising=False)
    bot = _FakeBot()
    _publish(bot)
    assert bot.set_calls == []
    assert len(bot.deleted_scopes) == 3


# --- fail-open ----------------------------------------------------------------

def test_a_bot_without_delete_my_commands_still_publishes(owner_env):
    class _Old(_FakeBot):
        delete_my_commands = None

    bot = _Old()
    _publish(bot)
    assert len(bot.set_calls) == 1


def test_a_failing_delete_does_not_block_the_publish(owner_env):
    class _Broken(_FakeBot):
        async def delete_my_commands(self, scope=None, **kw):
            raise RuntimeError("Bad Request")

    bot = _Broken()
    _publish(bot)
    assert len(bot.set_calls) == 1


def test_a_failing_publish_is_swallowed(owner_env):
    class _Broken(_FakeBot):
        async def set_my_commands(self, commands, scope=None, **kw):
            raise RuntimeError("Too Many Requests")

    _publish(_Broken())  # must not raise


# --- _menu_chat_ids -----------------------------------------------------------

def test_explicit_owner_id_leads_and_dedupes(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", "222")
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "111,222")
    assert _menu_chat_ids() == [222, 111]


def test_non_numeric_ids_are_dropped(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", "rob")
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "111,@someone, ,222")
    assert _menu_chat_ids() == [111, 222]


def test_chat_count_is_bounded(monkeypatch):
    monkeypatch.delenv("POLYROB_OWNER_TELEGRAM_ID", raising=False)
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS",
                       ",".join(str(i) for i in range(100)))
    assert len(_menu_chat_ids()) == 16
