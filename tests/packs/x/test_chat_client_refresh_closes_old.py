"""A token refresh that replaces the XChatClient closes the old one's session."""
import asyncio

from polyrob_x import x_chat_client as tt


class _Old:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


def test_replaced_client_is_closed_on_a_running_loop():
    old = _Old()

    async def run():
        tt.close_replaced_chat_client(old)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(run())
    assert old.closed


def test_no_loop_and_no_client_are_noops():
    tt.close_replaced_chat_client(None)
    tt.close_replaced_chat_client(_Old())  # no running loop: nothing raises
