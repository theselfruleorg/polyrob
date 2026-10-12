"""W4: spawn_retained without a registry holds the task in the module's own set."""
import asyncio

from core import async_bridge


def test_a_spawn_with_no_registry_is_held_until_done():
    async def main():
        gate = asyncio.Event()

        async def work():
            await gate.wait()
            return 7

        task = async_bridge.spawn_retained(work())
        assert task in async_bridge._RETAINED
        gate.set()
        assert await task == 7
        await asyncio.sleep(0)
        assert task not in async_bridge._RETAINED

    asyncio.run(main())
