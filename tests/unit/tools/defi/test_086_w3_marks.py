"""086 W3: ``defi_data.position_mark`` + ``defi_data.discovery_count`` — the mandate
jobs' $0 precheck reads. The barrier numbers come from the mandate's preset."""
import types

import pytest

from core import open_positions as op
from core import trading_mandate as tm
from core.open_positions import PositionDelta, apply_delta
from core.trading_presets import MAJORS_EXIT, MEMECOIN_EXIT
from tools.defi.data_tool import DefiDataTool
from tools.defi.marks import (CROSSED, NEAR, NONE, UNKNOWN, DiscoveryCountParams,
                              PositionMarkParams, barrier_for)
from tools.defi.providers import geckoterminal as gt

MEME = "0x" + "a1" * 20
MEME2 = "0x" + "b2" * 20


# -- the pure barrier ---------------------------------------------------------

def _b(price, *, grade="high", per=1.0, hw=None, hours=1.0, taken=0, preset=MEMECOIN_EXIT):
    return barrier_for(preset, price=price, grade=grade, basis_per_token=per,
                       high_water=hw, hours_open=hours, tiers_taken=taken)


@pytest.mark.parametrize("price,want", [
    (1.0, (NONE, "")),
    (0.55, (NEAR, "stop_loss")),          # -45 %: within near_pct 10 of -50 %
    (0.50, (CROSSED, "stop_loss")),
    (1.95, (NEAR, "take_profit")),        # +95 %
    (2.0, (CROSSED, "take_profit")),
])
def test_stop_and_take_profit(price, want):
    assert _b(price) == want


def test_a_spent_tier_does_not_refire_and_the_trail_takes_over():
    assert _b(2.0, taken=1, hw=2.0) == (NONE, "")
    # peak +200 % (armed), now 40 % below the high = the trail.
    assert _b(1.8, taken=1, hw=3.0) == (CROSSED, "trailing")
    assert _b(1.9, taken=1, hw=3.0)[0] == NEAR


def test_the_time_stop_needs_no_price_and_spares_a_winner():
    assert _b(None, hours=49.0, hw=1.1) == (CROSSED, "time_stop")
    assert _b(1.0, hours=49.0, hw=1.3) == (NONE, "")     # once +30 %: kept
    assert _b(1.0, hours=47.0) == (NONE, "")


def test_unknown_price_basis_or_weak_grade_never_fire():
    assert _b(None) == (UNKNOWN, "price_unreadable")
    assert _b(0.5, per=None) == (UNKNOWN, "basis_unknown")
    assert _b(0.5, grade="low") == (UNKNOWN, "stop_loss_unverified")
    assert _b(0.55, grade="low") == (NEAR, "stop_loss")   # near still informs
    assert _b(1.0, grade="low") == (NONE, "")


def test_majors_numbers_come_from_the_majors_preset():
    assert _b(0.92, preset=MAJORS_EXIT) == (CROSSED, "stop_loss")
    assert _b(0.935, preset=MAJORS_EXIT) == (NEAR, "stop_loss")
    assert _b(0.95, preset=MAJORS_EXIT) == (NONE, "")
    assert barrier_for(None, price=0.1, grade="high", basis_per_token=1.0, high_water=None,
                       hours_open=1.0) == (NONE, "no_exit_preset")


# -- the verb -----------------------------------------------------------------

def _ctx(uid="owner-1"):
    return types.SimpleNamespace(user_id=uid, session_id="s-1")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    import core.money.authority as authority
    monkeypatch.setattr(authority, "owner_refusal", lambda uid: None)
    return tmp_path


def _buy(qty=100.0, cost=3.0, token=MEME):
    apply_delta("owner-1", PositionDelta("base", token, "MEME", qty, cost),
                db_path=op.open_positions_db_path())


def _tool(price=0.03, conf="high", discover=None):
    return DefiDataTool(
        holder="0xHOLDER", discover_fn=discover,
        price_fn=lambda c, a: (None if price is None
                               else types.SimpleNamespace(price_usd=price, confidence=conf)))


def _mandate():
    return tm.create("owner-1", actor=tm.OWNER_SEAT, budget_usd=30.0, chains=["base"],
                     slots=2)


async def _mark(tool, mandate=None, address=MEME):
    return await tool.position_mark(PositionMarkParams(chain="base", account="",
                                                       address=address, mandate=mandate),
                                    execution_context=_ctx())


@pytest.mark.asyncio
async def test_mark_of_an_open_row_with_the_mandate_preset(home):
    _buy()
    m = _mandate()
    res = await _mark(_tool(price=0.015), m.id)            # -50 %
    md = res.metadata
    assert res.error is None, res.error
    assert md["closed"] is False and md["dust"] is False and md["price_grade"] == "high"
    assert md["qty"] == 100.0 and md["cost_usd"] == 3.0
    assert md["value_usd"] == pytest.approx(1.5)
    assert md["unrealized_pct"] == pytest.approx(-50.0)
    assert md["barrier"] == CROSSED and md["barrier_reason"] == "stop_loss"
    assert md["exit_preset"] == "memecoin_default" and md["hours_open"] is not None


@pytest.mark.asyncio
async def test_mark_raises_the_high_water_on_a_spend_grade_price_only(home):
    _buy()
    await _mark(_tool(price=0.05, conf="low"))
    assert op.get_position("owner-1", "base", MEME,
                           db_path=op.open_positions_db_path()).high_water_usd is None
    res = await _mark(_tool(price=0.05))
    assert res.metadata["high_water_usd"] == 0.05


@pytest.mark.asyncio
async def test_unreadable_price_is_unknown_and_wakes(home):
    _buy()
    res = await _mark(_tool(price=None), _mandate().id)
    assert res.metadata["price_grade"] == "unknown"
    assert res.metadata["barrier"] == UNKNOWN and res.metadata["closed"] is False


@pytest.mark.asyncio
async def test_closed_and_dust_rows(home):
    res = await _mark(_tool())
    assert res.metadata["closed"] is True and res.metadata["status"] == "none"
    _buy()
    op.set_status("owner-1", "base", MEME, op.STATUS_DUST, db_path=op.open_positions_db_path())
    res = await _mark(_tool())
    assert res.metadata["dust"] is True and res.metadata["closed"] is False
    op.set_status("owner-1", "base", MEME, op.STATUS_WRITTEN_OFF,
                  db_path=op.open_positions_db_path())
    assert (await _mark(_tool())).metadata["closed"] is True


@pytest.mark.asyncio
async def test_an_unreadable_book_is_an_error_never_closed(home):
    path = op.open_positions_db_path()
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"garbage" * 200)
    res = await _mark(_tool())
    assert res.error and "NOT a closed position" in res.error


@pytest.mark.asyncio
async def test_a_partial_sell_spends_the_take_profit_tier(home):
    _buy()
    apply_delta("owner-1", PositionDelta("base", MEME, "MEME", -50.0, proceeds_usd=3.0),
                db_path=op.open_positions_db_path())
    res = await _mark(_tool(price=0.06), _mandate().id)   # +100 % on the remainder
    assert res.metadata["tiers_taken"] == 1
    assert res.metadata["barrier"] != CROSSED or res.metadata["barrier_reason"] != "take_profit"


@pytest.mark.asyncio
async def test_a_mandate_of_another_tenant_is_unknown(home):
    _buy()
    other = tm.create("owner-2", actor=tm.OWNER_SEAT, budget_usd=30.0, chains=["base"])
    res = await _mark(_tool(), other.id)
    assert res.metadata["barrier"] == UNKNOWN
    assert res.metadata["barrier_reason"] == "mandate_unreadable"


@pytest.mark.asyncio
async def test_a_non_owner_turn_is_refused(home, monkeypatch):
    import core.money.authority as authority
    monkeypatch.setattr(authority, "owner_refusal", lambda uid: "not the owner")
    res = await _mark(_tool())
    assert res.error and "refused" in res.error


# -- discovery_count ----------------------------------------------------------

def _ago(hours):
    import datetime as _dt
    t = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hours)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _pool(token, pool="0xpool"):
    row = {"attributes": {"address": pool, "name": "X / WETH", "pool_created_at": _ago(24),
                          "reserve_in_usd": "50000",
                          "volume_usd": {"h24": "20000", "h1": "500"},
                          "price_change_percentage": {"h24": "5.0"},
                          "transactions": {"h24": {"buys": 200, "sells": 180, "buyers": 90,
                                                   "sellers": 80},
                                           "h1": {"buys": 10, "sells": 9, "buyers": 8,
                                                  "sellers": 7}}},
           "relationships": {"base_token": {"data": {"id": f"base_{token}"}},
                             "dex": {"data": {"id": "uniswap-v4-base"}}}}
    return gt.parse_pools({"data": [row]}, "base", "base")


def _discover(tokens):
    def fn(chain, kind):
        out = []
        for i, t in enumerate(tokens):
            out.extend(_pool(t, pool=f"0xpool{i}{kind}"))
        return out
    return fn


async def _count(tool, mandate=None, chains=("base",)):
    return await tool.discovery_count(DiscoveryCountParams(chains=list(chains),
                                                           mandate=mandate),
                                      execution_context=_ctx())


@pytest.mark.asyncio
async def test_count_without_a_mandate(home):
    res = await _count(_tool(discover=_discover([MEME, MEME2])))
    md = res.metadata
    assert md["count"] == 2 and md["actionable"] == 2 and md["mandate_ready"] is True


@pytest.mark.asyncio
async def test_count_skips_a_held_token_and_full_slots_read_nothing(home):
    m = _mandate()
    _buy(token=MEME)
    res = await _count(_tool(discover=_discover([MEME, MEME2])), m.id)
    assert res.metadata["count"] == 1 and res.metadata["actionable"] == 1
    _buy(token=MEME2)

    def boom(chain, kind):
        raise AssertionError("no read when the slots are full")
    res = await _count(_tool(discover=boom), m.id)
    assert res.metadata["mandate_ready"] is False and res.metadata["actionable"] == 0


@pytest.mark.asyncio
async def test_a_paused_mandate_is_a_quiet_tick(home):
    m = _mandate()
    tm.set_status(m.id, tm.PAUSED, actor=tm.OWNER_SEAT)
    res = await _count(_tool(discover=_discover([MEME])), m.id)
    assert res.metadata["actionable"] == 0 and "PAUSED" in res.metadata["mandate_reason"]


@pytest.mark.asyncio
async def test_every_read_failed_is_unknown_and_wakes(home):
    def down(chain, kind):
        raise gt.DiscoveryUnavailable("HTTP 429")
    res = await _count(_tool(discover=down))
    assert res.metadata["actionable"] is None and res.metadata["unavailable_chains"] == ["base"]
