"""P4 — centralized sync->async bridge (persistent loop)."""
import asyncio

import pytest

from core.async_bridge import run_coroutine_sync


async def _double(x):
    await asyncio.sleep(0)
    return x * 2


def test_no_running_loop_uses_asyncio_run():
    # Called from a plain sync context (no event loop on this thread).
    assert run_coroutine_sync(_double(21)) == 42


@pytest.mark.asyncio
async def test_from_running_loop_uses_persistent_bridge():
    # Called synchronously from within a running loop (the Agent.__init__ case).
    assert run_coroutine_sync(_double(21)) == 42


@pytest.mark.asyncio
async def test_bridge_reuses_one_thread_across_calls():
    import threading
    before = {t.name for t in threading.enumerate()}
    for _ in range(5):
        assert run_coroutine_sync(_double(1)) == 2
    after = {t.name for t in threading.enumerate()}
    # at most one new persistent bridge thread, regardless of call count
    new_threads = [n for n in (after - before) if "async-bridge" in n]
    assert len(new_threads) <= 1


def test_timeout_raises():
    async def _slow():
        await asyncio.sleep(5)

    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        run_coroutine_sync(_slow(), timeout=0.1)


@pytest.mark.asyncio
async def test_exception_propagates_from_bridge():
    async def _boom():
        raise ValueError("kaboom")

    with pytest.raises(ValueError, match="kaboom"):
        run_coroutine_sync(_boom())
