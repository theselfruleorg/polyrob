"""A thread notice that sends clears every OLDER held notice: a held "Payment received"
is never posted after the "Order delivered" that followed it. A notice held while
the send ran (newer than it) stays held."""
import time

import pytest

from core.commerce import orders as ords
from modules.commerce import service
from tests.unit.modules.commerce.test_order_service_e2e import _env  # noqa: F401  (autouse)
from tests.unit.modules.commerce.test_order_state_audit_fixes import (  # noqa: F401
    _Chan, _thread_order, chan_x)


@pytest.mark.asyncio
async def test_a_fresh_notice_clears_an_older_held_one(chan_x):
    o = _thread_order(1000.0)
    ords.set_notice_pending(o.id, "Payment received", now=1000.0)   # the thread was down
    ch = chan_x(_Chan())
    assert await service.tell_buyer(None, ords.get(o.id), "Order delivered")
    assert ords.get(o.id).notice_pending is None
    await service.tick(now=1000.0 + service.NOTICE_RETRY_S * 3)
    assert ch.sent == ["Order delivered"]          # the old notice is never posted after it


@pytest.mark.asyncio
async def test_a_notice_held_during_the_send_stays_held(chan_x):
    o = _thread_order(1000.0)
    later = time.time() + 100
    chan_x(_Chan(on_reply=lambda: ords.set_notice_pending(o.id, "Order delivered", now=later)))
    assert await service.tell_buyer(None, ords.get(o.id), "Payment received")
    assert ords.get(o.id).notice_pending == "Order delivered"
