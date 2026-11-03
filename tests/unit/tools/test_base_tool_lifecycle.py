"""098 CO-06b: BaseTool initialize/cleanup are double-checked under one lock."""
import asyncio

from core.config import BotConfig
from tools.base_tool import BaseTool, ToolStatus


class _Container:
    def get_service(self, name):
        return object()

    def has_service(self, name):
        return False


class _T(BaseTool):
    def __init__(self):
        super().__init__(name="t", config=BotConfig())
        self.container = _Container()
        self.inits = 0
        self.cleans = 0

    async def _initialize(self):
        self.inits += 1
        await asyncio.sleep(0.01)

    async def _cleanup(self):
        self.cleans += 1
        await asyncio.sleep(0.01)


def test_concurrent_initialize_runs_once():
    async def go():
        t = _T()
        await asyncio.gather(*[t.initialize() for _ in range(4)])
        assert t.inits == 1 and t._status == ToolStatus.HEALTHY
    asyncio.run(go())


def test_double_cleanup_runs_once():
    async def go():
        t = _T()
        await t.initialize()
        await asyncio.gather(t.cleanup(), t.cleanup())
        assert t.cleans == 1 and t._status == ToolStatus.CLOSED and not t._initialized
        await t.initialize()  # closed -> initialize again is allowed
        assert t.inits == 2 and t._status == ToolStatus.HEALTHY
    asyncio.run(go())
