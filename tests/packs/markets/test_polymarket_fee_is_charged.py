"""A Polymarket order's fee leaves the wallet like the order: the spend caps are charged
notional + the market's worst fee, and a documented default when the fee is unreadable."""
import types

import pytest

pytest.importorskip("polyrob_markets")

from core.wallet.policy import PolicyGate
from polyrob_markets.polymarket import fees
from polyrob_markets.polymarket.service import PlaceLimitOrderParams

from .test_pm_policy_gate_wiring import _async, _enable_live_trading, _tool  # noqa: F401

INFO = {"t": [{"t": "t1", "o": "Yes"}], "mts": 0.01}


def test_platform_fee_formula_at_the_order_price():
    fee, src = fees.worst_fee_usd({**INFO, "fd": {"r": 0.25, "e": 2}}, shares=100,
                                  price=0.5, reference=0.5, notional_usd=50)
    assert src == "market" and fee == pytest.approx(100 * 0.25 * 0.0625)


def test_base_fee_bps_formula_and_the_dearer_one_wins():
    fee, _ = fees.worst_fee_usd({**INFO, "tbf": 1000, "fd": {"r": 0.0, "e": 1}}, shares=100,
                                price=0.2, reference=0.2, notional_usd=20)
    assert fee == pytest.approx(100 * 0.1 * 0.2)


def test_worst_fill_price_inside_the_band_is_the_point_nearest_half():
    fee, _ = fees.worst_fee_usd({**INFO, "fd": {"r": 0.1, "e": 1}}, shares=10,
                                price=0.45, reference=0.55, notional_usd=5.5)
    assert fee == pytest.approx(10 * 0.1 * 0.25)


def test_a_fee_free_market_charges_nothing():
    assert fees.worst_fee_usd(INFO, shares=10, price=0.5, reference=0.5,
                              notional_usd=5) == (0.0, "market")


@pytest.mark.parametrize("info", [None, {}, {"error": "x"}, {**INFO, "fd": "bad"},
                                  {**INFO, "fd": {"r": "nan", "e": 1}}, {**INFO, "tbf": -5}])
def test_unreadable_fee_charges_the_documented_default_and_never_refuses(info):
    fee, src = fees.worst_fee_usd(info, shares=10, price=0.5, reference=0.5, notional_usd=5)
    assert src == "default" and fee == pytest.approx(5 * fees.DEFAULT_FEE_RATE)
    assert fees.DEFAULT_FEE_RATE >= 0.03     # above the dearest published schedule


@pytest.mark.asyncio
async def test_check_submit_and_record_all_carry_the_fee(monkeypatch):
    gate = PolicyGate(max_per_tx_usd=10_000.0)
    tool, client = _tool(monkeypatch, gate)
    monkeypatch.setattr(tool, "_market_fee_info",
                        lambda token_id: _async({**INFO, "fd": {"r": 0.2, "e": 1}}))
    checked, journaled = [], []
    real_check = gate.check
    monkeypatch.setattr(gate, "check", lambda **kw: checked.append(kw["amount_usd"]) or real_check(**kw))
    import core.wallet.submission_journal as journal
    real_prepare = journal.prepare_attempt
    monkeypatch.setattr(journal, "prepare_attempt",
                        lambda venue, holder, amount: journaled.append(amount) or real_prepare(venue, holder, amount))
    res = await tool.place_limit_order(PlaceLimitOrderParams(
        market_id="m1", token_id="t1", side="buy", price=0.5, size_usd=5.0))
    assert res["success"] is True
    expected = 5.0 + 10 * 0.2 * 0.25        # 10 shares at 0.5
    assert res["order_details"]["fee_charged_usd"] == pytest.approx(expected - 5.0)
    assert res["order_details"]["fee_source"] == "market"
    assert checked == [pytest.approx(expected)]
    assert journaled == [pytest.approx(expected)]
    assert gate.audit_log[0]["amount_usd"] == pytest.approx(expected)


@pytest.mark.asyncio
async def test_unreadable_fee_endpoint_still_places_the_order(monkeypatch):
    gate = PolicyGate(max_per_tx_usd=10_000.0)
    tool, client = _tool(monkeypatch, gate)

    async def down(url, **kw):
        raise OSError("clob down")
    tool._http_client = types.SimpleNamespace(get=down)
    res = await tool.place_limit_order(PlaceLimitOrderParams(
        market_id="m1", token_id="t1", side="buy", price=0.5, size_usd=5.0))
    assert res["success"] is True and res["order_details"]["fee_source"] == "default"
    assert gate.audit_log[0]["amount_usd"] == pytest.approx(5.0 * (1 + fees.DEFAULT_FEE_RATE))


@pytest.mark.asyncio
async def test_market_fee_info_reads_the_clob_market(monkeypatch):
    gate = PolicyGate(max_per_tx_usd=10_000.0)
    tool, _ = _tool(monkeypatch, gate)
    seen = []

    async def get(url, **kw):
        seen.append(url)
        body = {"condition_id": "0xc"} if "markets-by-token" in url else {**INFO, "tbf": 30}
        return types.SimpleNamespace(status_code=200, json=lambda: body)
    tool._http_client = types.SimpleNamespace(get=get)
    assert (await tool._market_fee_info("t1"))["tbf"] == 30
    assert seen[0].endswith("/markets-by-token/t1") and seen[1].endswith("/clob-markets/0xc")
