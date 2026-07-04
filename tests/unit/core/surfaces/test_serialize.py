import asyncio, pytest
from core.surfaces.serialize import KeyedLock


@pytest.mark.asyncio
async def test_same_key_serializes_order():
    lock = KeyedLock()
    order = []

    async def work(tag, delay):
        async with lock.for_key("k"):
            await asyncio.sleep(delay)
            order.append(tag)

    await asyncio.gather(work("a", 0.02), work("b", 0.0))
    assert order == ["a", "b"]   # b waited for a even though b was faster


@pytest.mark.asyncio
async def test_different_keys_run_concurrently():
    lock = KeyedLock()
    order = []
    async def work(tag, key, delay):
        async with lock.for_key(key):
            await asyncio.sleep(delay); order.append(tag)
    await asyncio.gather(work("slow", "k1", 0.03), work("fast", "k2", 0.0))
    assert order == ["fast", "slow"]
