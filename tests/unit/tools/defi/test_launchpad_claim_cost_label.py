"""A claim's dollar line is its FEE, and must say so.

Live 2026-09-29 (session 0e87054b): `launchpad_claim(dry_run=true)` on 0.3087
claimable ETH printed `value:    $0.0200`. The guard's ``amount_usd`` for a
claim is the worst-case gas fee (a claim has no outflow), but the header called
it "value", so the agent reported to the owner that the tool had priced
0.3087 ETH (~$840) at two cents.
"""
import pytest

from core.wallet.tx_guard import Decision
from tests.unit.tools.defi import test_trade_t4 as t4


@pytest.fixture(autouse=True)
def _reset():
    yield
    t4._Rail.last = None


async def _dry_run(*, is_claim: bool):
    from tools.launchpad.execute import guarded_send
    tool, _gate = t4._tool()
    tool._rail_factory = t4._Rail
    tool._notify_tx = lambda *a, **k: None
    tool._guard_fn = lambda *a, **k: Decision(
        True, "ok", lane="owner_direct", amount_usd=0.02)
    res = await guarded_send(
        tool, execution_context=None, verb="claim" if is_claim else "buy",
        chain="base", to="0x" + "4" * 40, calldata="0x4e71d92d",
        value_wei=0 if is_claim else 10 ** 12, max_spend_usd=1.0, dry_run=True,
        header="", is_claim=is_claim,
        min_native_inflow_wei=308_734_466_399_179_249 if is_claim else None)
    assert res.error is None, res.error
    return res.extracted_content


@pytest.mark.asyncio
async def test_a_claims_dollar_line_names_the_fee_not_a_value():
    out = await _dry_run(is_claim=True)
    assert "value:" not in out
    assert "$0.0200" in out
    assert "fee" in out.lower()
    assert "not the value" in out.lower()


@pytest.mark.asyncio
async def test_a_buy_still_labels_its_spend():
    out = await _dry_run(is_claim=False)
    assert "$0.0200" in out
