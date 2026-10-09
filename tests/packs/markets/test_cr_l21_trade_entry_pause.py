"""CR-L21: the scoped `/pause trading` (``trade_entry``) binds the venue order
verbs and the shared money-verb pause seam — exits stay exempt."""
import pytest

pytest.importorskip("polyrob_markets")

import pytest

import core.autonomy_control as ac


@pytest.fixture
def trading_paused(monkeypatch):
    st = ac.PauseState(paused=True, scopes=("trading",), since=None, until=None,
                       set_by="owner", via="test", reason="stop entries",
                       source="record", record_scopes=("trading",))

    def fake_allows(kind, data_dir=None):
        denied = kind == "trade_entry"
        return ac.Decision(not denied,
                           "paused (trading) by owner via test" if denied else "running",
                           st)

    monkeypatch.setattr(ac, "allows", fake_allows)
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner-1")


def test_spend_pause_refusal_blocks_entries_only(trading_paused):
    from core.wallet.authority import spend_pause_refusal
    assert spend_pause_refusal(entry=True)
    assert spend_pause_refusal() is None          # an exit / non-entry verb passes


def test_trade_turn_refusal_blocks_an_entry_not_an_exit(trading_paused):
    from polyrob_markets.trade_gate import trade_turn_refusal
    assert trade_turn_refusal(None, None)                      # a new order
    assert trade_turn_refusal(None, None, entry=False) is None  # a sell / reduce-only
    assert trade_turn_refusal(None, None, risk_reducing=True) is None  # a cancel


@pytest.mark.asyncio
async def test_hyperliquid_buy_is_refused_under_the_trading_pause(trading_paused):
    import types
    from polyrob_markets.hyperliquid.service import HyperliquidTool, PlaceMarketOrderParams

    tool = HyperliquidTool(config=types.SimpleNamespace(), container=None)

    async def _noop():
        return None

    tool.ensure_initialized = _noop
    res = await tool.place_market_order(PlaceMarketOrderParams(
        coin="ETH", is_buy=True, size=0.01, max_usd=10))
    assert res["success"] is False and "trading" in res["error"]
