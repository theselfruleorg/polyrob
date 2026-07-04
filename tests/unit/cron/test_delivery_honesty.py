"""Cron delivery honesty (AU-F6.1): MessageRouter.send_message must return the real
send outcome — False on a missing surface or a raising send, True only on a
completed send — so cron/delivery.py never mismarks a failed delivery as surfaced.
"""
import pytest

from core.surfaces.message_router import MessageRouter
from core.surfaces.session_chat_registry import SessionChatRegistry


@pytest.fixture
def router(tmp_path):
    reg = SessionChatRegistry(str(tmp_path / "chat.db"))
    return MessageRouter(reg)


@pytest.mark.asyncio
async def test_send_message_returns_false_when_no_surface(router):
    assert await router.send_message("123", "hi", surface_id="telegram") is False


@pytest.mark.asyncio
async def test_send_message_returns_false_when_send_raises(router):
    class Boom:
        async def send(self, msg):
            raise RuntimeError("net down")

    router._surfaces["telegram"] = Boom()
    assert await router.send_message("123", "hi", surface_id="telegram") is False


@pytest.mark.asyncio
async def test_send_message_returns_true_on_success(router):
    class Ok:
        async def send(self, msg):
            return None

    router._surfaces["telegram"] = Ok()
    assert await router.send_message("123", "hi", surface_id="telegram") is True
