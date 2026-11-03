"""098 TS-17: a stop that comes before run_polling's first step is honoured."""
import asyncio

from surfaces.telegram.harness import TelegramHarness


class _Bot:
    def __init__(self):
        self.calls = 0

    async def get_updates(self, **kw):
        self.calls += 1
        return []


def test_stop_before_run_polling_is_honoured():
    h = TelegramHarness.__new__(TelegramHarness)
    h.bot = _Bot()
    h.poll_timeout = 0
    h._running, h._stop_requested = False, True     # what on_signal / stop() set
    asyncio.run(asyncio.wait_for(h.run_polling(), timeout=2))
    assert h.bot.calls == 0


def test_on_signal_sets_the_sticky_stop():
    import inspect
    from surfaces.telegram import launch
    assert "_stop_requested = False, True" in inspect.getsource(launch.launch)
