"""098 review O-10: waiting for the lease keeper never swallows a cancel of
the sending task."""
import asyncio

import pytest

from core.surfaces.outbound_dispatcher import OutboundDispatcher


def test_a_cancel_while_the_keeper_winds_down_still_propagates():
    async def go():
        d = object.__new__(OutboundDispatcher)
        stop = asyncio.Event()

        async def slow_keeper(row):
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                await stop.wait()          # the keeper takes a while to end
                raise
        d._hold_lease = slow_keeper

        class _Surface:
            async def send(self, message):
                await asyncio.sleep(0.005)     # the keeper is running by now
                return True
        t = asyncio.create_task(d._send_holding_lease(_Surface(), {}, "m"))
        await asyncio.sleep(0.03)          # send done; now waiting for the keeper
        assert not t.done()
        t.cancel()
        await asyncio.sleep(0.01)
        stop.set()
        with pytest.raises(asyncio.CancelledError):
            await t
    asyncio.run(go())
