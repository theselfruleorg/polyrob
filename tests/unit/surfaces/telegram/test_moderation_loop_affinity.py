"""Telegram reads happen on the BOT's event loop, never a bridged one.

⚠️ Live, Playground Env 2026-09-15 17:53:48 and 17:55:10:

    member-status probe failed (Task <... member_status() ...> got Future
    <Future pending> attached to a different loop) — treating the target as
    PROTECTED

`_handle_owner_admin` is `async def` and already `await`s `groups_reply`, but
called `mute_reply`/`ban_reply`/`paid_reply` SYNCHRONOUSLY. Those used
`core.async_bridge.run_coroutine_sync`, which hands the coroutine to a
persistent BACKGROUND loop — while the aiogram Bot's aiohttp session is bound
to the harness loop we were already standing on. Every Telegram read from these
seats therefore raised.

It broke both halves of the rail at once, which is why a member's paid ban AND
the owner's free ban failed the same evening:

* `_target_status_fn` raised -> `_target_protection` read that as PROTECTED
  (its correct fail-closed default) -> "That person cannot be targeted here".
* `_perform` raised -> the owner's FREE `/ban` did nothing.
* `_rights_fn` returns an empty set on fault -> the rights gate would have
  refused one step later anyway.

The fix is not a better bridge. When you are already ON the loop you await;
`core/` stays sync by receiving a value the async caller already resolved.
"""
import asyncio
import inspect

import pytest

from surfaces.telegram import group_ops


# --- the shape that makes the bug impossible -------------------------------

@pytest.mark.parametrize("name", [
    "mute_reply", "unmute_reply", "ban_reply", "unban_reply", "paid_reply",
])
def test_the_room_verb_handlers_are_awaitable(name):
    """⚠️ A sync handler cannot await a bot call, and bridging one is the bug.
    `groups_reply` was already async; these are its siblings."""
    fn = getattr(group_ops, name)
    assert inspect.iscoroutinefunction(fn), f"{name} is still sync"


def test_the_harness_awaits_every_one_of_them():
    """A handler made async but called without `await` returns a coroutine that
    is never run — the verb would silently do nothing at all."""
    import surfaces.telegram.harness as h
    src = inspect.getsource(h._handle_owner_admin)
    for name in ("mute_reply", "unmute_reply", "ban_reply", "unban_reply",
                 "paid_reply"):
        idx = src.find(name)
        assert idx != -1, f"{name} not dispatched"
    assert "handler(task_agent" not in src or "await handler(" in src, (
        "the /unmute|/ban|/unban handler is called without await")


def test_no_telegram_read_goes_through_the_sync_bridge():
    """⚠️ The regression guard. `run_coroutine_sync` in this module is what put
    a bot call on the wrong loop; awaiting is the only correct form here.

    Matches a real IMPORT or CALL, never the word in a docstring — the comments
    that explain this outage necessarily name the function.
    """
    import ast
    tree = ast.parse(inspect.getsource(group_ops))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "core.async_bridge":
            names = [a.name for a in node.names]
            assert "run_coroutine_sync" not in names, (
                "a Telegram read is being bridged to another loop again")
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            assert name != "run_coroutine_sync", (
                "a Telegram read is being bridged to another loop again")


# --- behaviour: the probes are resolved, not bridged -----------------------

class _Bot:
    def __init__(self):
        self.loop = None

    async def get_chat_member(self, chat_id, user_id):
        # Records which loop actually ran the call.
        self.loop = asyncio.get_running_loop()
        return type("M", (), {"status": "member", "user": type("U", (), {"id": user_id})})()


def test_a_bot_read_runs_on_the_caller_loop(monkeypatch):
    """The whole point: the read executes on the loop we are already on."""
    bot = _Bot()

    async def drive():
        from surfaces.telegram.moderation import member_status
        here = asyncio.get_running_loop()
        await member_status(bot, "-100", "777")
        return here

    here = asyncio.run(drive())
    assert bot.loop is here, "the bot call ran on a different loop"
