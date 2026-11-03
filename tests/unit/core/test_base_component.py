"""098 CO-06: BaseComponent initialize/cleanup are double-checked under one lock."""
import asyncio

from core.base_component import BaseComponent
from core.config import BotConfig


class _C(BaseComponent):
    def __init__(self):
        super().__init__(name="c", config=BotConfig())
        self._container = None
        self.inits = 0
        self.cleans = 0
        self.gate = None

    @property
    def container(self):
        return None

    async def _initialize(self):
        self.inits += 1
        await asyncio.sleep(0)
        if self.gate is not None:
            await self.gate.wait()

    async def _cleanup(self):
        self.cleans += 1


def test_concurrent_initialize_runs_once():
    async def go():
        c = _C()
        await asyncio.gather(c.initialize(), c.initialize(), c.initialize())
        assert c.inits == 1 and c._initialized
    asyncio.run(go())


def test_cleanup_waits_for_initialize():
    async def go():
        c = _C()
        c.gate = asyncio.Event()
        init = asyncio.create_task(c.initialize())
        await asyncio.sleep(0.01)
        c._initialized = True  # an earlier init finished; this one is in flight
        cl = asyncio.create_task(c.cleanup())
        await asyncio.sleep(0.01)
        assert c.cleans == 0  # waits for the lock
        c.gate.set()
        await init
        await cl
        assert c.cleans == 1 and not c._initialized
        await c.cleanup()  # a second cleanup is a no-op
        assert c.cleans == 1
    asyncio.run(go())
