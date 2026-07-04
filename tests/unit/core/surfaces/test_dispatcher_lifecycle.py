import os, pytest
from core.surfaces.outbound_queue import OutboundDeliveryQueue
from core.surfaces.outbound_dispatcher import OutboundDispatcher


@pytest.mark.asyncio
async def test_start_then_stop_is_clean(tmp_path):
    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    d = OutboundDispatcher(q, lambda sid: None)
    d.start(); d.start()          # idempotent
    await d.stop()                 # no hang, no raise
