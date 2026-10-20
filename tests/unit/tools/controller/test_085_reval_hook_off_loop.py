"""085 W2 revalidation: the money approval hook's exemption predicate (which
prices a declared inflow, a network read on a cache miss) runs OFF the event
loop, so a slow price source never stalls the loop the owner's chat runs on."""
import asyncio
import threading

import pytest

from tools.controller.approval import make_approval_hook
from tools.controller.self_transfer import spend_exemption_with_turn


class _Provider:
    async def request(self, action_name, params, context):
        return True


def test_the_spend_predicate_is_marked_off_loop():
    assert getattr(spend_exemption_with_turn, "off_loop", False) is True


@pytest.mark.asyncio
async def test_an_off_loop_predicate_runs_in_a_worker_thread():
    loop_thread = threading.get_ident()
    seen = {}

    def slow_price_predicate(action_name, params, context=None):
        seen["thread"] = threading.get_ident()
        return "exempt"
    slow_price_predicate.takes_context = True
    slow_price_predicate.off_loop = True

    hook = make_approval_hook(_Provider(), {"defi_trade_call"}, exempt_fn=slow_price_predicate)
    assert await hook("defi_trade_call", {}, None) is None
    assert seen["thread"] != loop_thread


@pytest.mark.asyncio
async def test_a_blocking_price_read_does_not_stall_the_loop(monkeypatch):
    from core.config_policy import spend_lane
    import time
    monkeypatch.setenv("DEFI_TIERED_SPEND_LANE", "true")
    monkeypatch.setattr(spend_lane, "autonomous_ceiling_usd", lambda: 300.0)

    def blocking_price(chain, addr):
        time.sleep(0.3)                        # a cache miss on a slow source
        return None
    monkeypatch.setattr(spend_lane, "_SPEND_PRICE", blocking_price)
    ticks = []

    async def ticker():
        for _ in range(5):
            ticks.append(time.monotonic())
            await asyncio.sleep(0.02)

    hook = make_approval_hook(_Provider(), {"defi_trade_call"},
                              exempt_fn=spend_exemption_with_turn)
    params = {"chain": "base", "value": 0.004, "dry_run": False, "max_spend_usd": 10.0,
              "receive_token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
              "receive_min_raw": 9_000_000}
    t = asyncio.create_task(ticker())
    assert await hook("defi_trade_call", params, None) is None   # unpriced -> tap (approved)
    await t
    assert len(ticks) == 5 and ticks[-1] - ticks[0] < 0.25
