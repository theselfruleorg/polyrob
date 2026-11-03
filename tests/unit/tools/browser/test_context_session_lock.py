"""098 TS-06: BrowserContext.get_session initialises once; contexts are never pooled."""
import asyncio
from types import SimpleNamespace

from tools.browser.context import BrowserContext


def test_concurrent_get_session_initialises_once():
    async def go():
        ctx = BrowserContext.__new__(BrowserContext)
        ctx.session = None
        ctx._session_lock = asyncio.Lock()
        ctx._session_dead = lambda: False
        calls = []

        async def init():
            calls.append(1)
            await asyncio.sleep(0.01)
            ctx.session = SimpleNamespace(id=len(calls))
            return ctx.session
        ctx._initialize_session = init
        got = await asyncio.gather(*[ctx.get_session() for _ in range(4)])
        assert len(calls) == 1 and all(g is got[0] for g in got)
    asyncio.run(go())


def test_release_never_pools():
    from tools.browser.browser_manager import BrowserManager
    assert not hasattr(BrowserContext, "reset_context")
    assert "_reset_context_for_session" not in vars(BrowserManager)
