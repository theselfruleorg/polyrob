"""071 W3 — `defi_data.positions` does the arithmetic; reconcile reads the rail store.

Pins:
 - positions: basis (average cost), value, unrealized USD/%, realized, high-water
   — all computed by the verb; an unknown basis is "basis unknown", never $0;
   an unknown price is no value; the realized figure outlives a full close;
 - the high-water mark only rises from a HIGH-confidence price;
 - the verb is an operator read (refused for a non-owner) and an unreadable
   store is an error, never an empty book;
 - reconcile: a closed ledger row is not unbacked; an unpriced token no book
   mentions is `unsolicited` (CLEAN is reachable); the rail store is diffed
   against the chain and each row names its source.
"""
import pytest

from core import open_positions
from core.open_positions import PositionDelta, apply_delta
from tools.defi import positions_view
from tools.defi.data_tool import DefiDataTool, PositionsParams, ReconcileParams
from tools.defi.reconcile import ChainHolding, RailPosition, diff, parse_open_positions, render

MEME = "0xb200000000000000000000ea8625786a776539fb"
MEME2 = "0xb200000000000000000000b344cb4a1e8bd51968"
DUST = "0xb2000000000000000000000ff4a547c891ab1b01"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def _px(price, conf="high"):
    return type("P", (), {"price_usd": price, "confidence": conf})()


def _ctx(uid="owner-1"):
    return type("Ctx", (), {"user_id": uid, "session_id": "s"})()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    # the operator read gate: only owner-1 is the bound owner
    import core.wallet.authority as auth
    monkeypatch.setattr(auth, "turn_refusal",
                        lambda ctx: None if getattr(ctx, "user_id", None) == "owner-1"
                        else "operator wallet access requires the bound owner identity")
    return tmp_path


def _db():
    return open_positions.open_positions_db_path()


# ------------------------------------------------------------------ positions_view (pure)

def test_figures_average_cost_and_pct():
    e = open_positions.PositionEntry(chain="base", address=MEME, symbol="MEME", qty=100.0,
                                     entry_usd=50.0, entry_ts=1.0, qty_source="receipt")
    f = positions_view.figures(e, _px(0.75))
    assert f.value_usd == pytest.approx(75.0)
    assert f.unrealized_usd == pytest.approx(25.0) and f.unrealized_pct == pytest.approx(50.0)
    assert f.basis_per_token_usd == pytest.approx(0.5)
    assert f.realized_usd == 0.0


def test_unknown_basis_is_not_zero_and_gives_no_pnl():
    e = open_positions.PositionEntry(chain="base", address=MEME, symbol="MEME", qty=100.0,
                                     entry_usd=None, entry_ts=1.0)
    f = positions_view.figures(e, _px(1.0))
    assert f.basis_usd is None and f.unrealized_usd is None and f.value_usd == 100.0
    text = positions_view.render([f], [], chain=None)
    assert "basis unknown" in text and "unrealized unknown (basis unknown)" in text
    assert "$0.00 (" not in text.split("realized")[0]


def test_no_price_gives_no_value():
    e = open_positions.PositionEntry(chain="base", address=MEME, symbol="MEME", qty=100.0,
                                     entry_usd=10.0, entry_ts=1.0)
    f = positions_view.figures(e, _px(None))
    assert f.value_usd is None and f.unrealized_usd is None
    text = positions_view.render([f], [], chain="base")
    assert "price unknown" in text and "unpriced, NOT included" in text


# ------------------------------------------------------------------ the verb

@pytest.mark.asyncio
async def test_positions_verb_does_the_arithmetic(home):
    apply_delta("owner-1", PositionDelta("base", MEME, "MEME", 200, 100.0, qty_source="receipt"),
                db_path=_db())
    apply_delta("owner-1", PositionDelta("base", MEME, "MEME", -100, proceeds_usd=80.0,
                                         qty_source="receipt"), db_path=_db())
    apply_delta("owner-1", PositionDelta("base", MEME2, "M2", 10, 5.0), db_path=_db())
    apply_delta("owner-1", PositionDelta("base", MEME2, "M2", -10, proceeds_usd=2.0),
                db_path=_db())
    prices = {MEME: _px(0.9)}
    tool = DefiDataTool(price_fn=lambda chain, addr: prices.get(addr, _px(None)))
    res = await tool.positions(PositionsParams(), execution_context=_ctx())
    assert res.error is None, res.error
    meta = res.metadata
    (p,) = meta["positions"]
    assert p["qty"] == 100 and p["basis_usd"] == pytest.approx(50.0)
    assert p["value_usd"] == pytest.approx(90.0)
    assert p["unrealized_usd"] == pytest.approx(40.0) and p["unrealized_pct"] == pytest.approx(80.0)
    assert p["realized_usd"] == pytest.approx(30.0)
    assert p["high_water_usd"] == pytest.approx(0.9)
    (c,) = meta["closed"]
    assert c["address"] == MEME2 and c["realized_usd"] == pytest.approx(-3.0)
    text = res.extracted_content
    assert "legacy Transfer events, unverified quantity" in text and "average cost" in text
    assert "never compute" in text


@pytest.mark.asyncio
async def test_high_water_ignores_a_low_confidence_price(home):
    apply_delta("owner-1", PositionDelta("base", MEME, "MEME", 1, 1.0), db_path=_db())
    tool = DefiDataTool(price_fn=lambda c, a: _px(50.0, "low"))
    res = await tool.positions(PositionsParams(chain="base"), execution_context=_ctx())
    assert res.metadata["positions"][0]["high_water_usd"] is None
    assert "low-confidence" in res.extracted_content
    assert open_positions.get_position("owner-1", "base", MEME).high_water_usd is None


@pytest.mark.asyncio
async def test_positions_is_an_operator_read(home):
    apply_delta("owner-1", PositionDelta("base", MEME, "MEME", 1, 1.0), db_path=_db())
    tool = DefiDataTool(price_fn=lambda c, a: _px(1.0))
    res = await tool.positions(PositionsParams(), execution_context=_ctx("stranger"))
    assert res.error and "refused" in res.error


@pytest.mark.asyncio
async def test_no_store_is_an_empty_book_but_an_unreadable_one_is_an_error(home):
    tool = DefiDataTool(price_fn=lambda c, a: _px(1.0))
    res = await tool.positions(PositionsParams(), execution_context=_ctx())
    assert res.error is None and "No open position" in res.extracted_content
    (home / "open_positions.db").write_bytes(b"garbage, not sqlite" * 20)
    res = await tool.positions(PositionsParams(), execution_context=_ctx())
    assert res.error and "NOT an empty book" in res.error


def test_positions_policy_matches_portfolio():
    """Own sizes and P&L are the reconnaissance before a drain, like portfolio."""
    from core.verb_policy_rows import CORE_VERB_ROWS
    row = CORE_VERB_ROWS["defi_data"]["defi_data_positions"]
    assert row == CORE_VERB_ROWS["defi_data"]["defi_data_portfolio"]
    from agents.task.agent.core.correspondent_gate import is_high_impact
    assert is_high_impact("defi_data_positions") is True


# ------------------------------------------------------------------ reconcile (pure)

def _held(addr, qty, value=None, known=True):
    return ChainHolding(address=addr, symbol="TOK", qty=qty, raw_units=1 if qty else 0,
                        value_usd=value, balance_known=known)


CLOSED_LEDGER = f"""## Open positions

| Token | Address | Size |
|---|---|---|
| MEME | {MEME} | 0 — FULL EXIT 2026-09-23 |
"""


def test_a_closed_ledger_row_is_not_unbacked():
    rows, _ = parse_open_positions(CLOSED_LEDGER)
    report = diff(rows, [], quote_addresses=[USDC])
    assert report.unbacked == [] and report.verdict == "CLEAN"
    assert "closed" in report.matched[0]


def test_a_closed_ledger_row_still_held_is_a_mismatch():
    rows, _ = parse_open_positions(CLOSED_LEDGER)
    report = diff(rows, [_held(MEME, 50.0, value=5.0)], quote_addresses=[USDC])
    assert report.mismatched and report.verdict == "DISAGREEMENT"


def test_a_dusted_wallet_reads_clean():
    holdings = [_held(f"0x{i:040x}", 1000.0, value=None) for i in range(1, 30)]
    report = diff([], holdings, quote_addresses=[USDC], rail=[])
    assert report.verdict == "CLEAN" and len(report.unsolicited) == 29
    text = render(report, chain="base", holder="0xH", ledger_path="l.md", coverage="c")
    assert "unsolicited" in text and "rail store (open_positions): 0 row(s) compared" in text


def test_rail_store_rows_are_diffed_and_name_their_source():
    rail = [RailPosition(address=MEME, symbol="MEME", qty=100.0),
            RailPosition(address=MEME2, symbol="M2", qty=10.0),
            RailPosition(address=DUST, symbol="LOOK", qty=5.0, status="quarantined")]
    holdings = [_held(MEME, 60.0, value=6.0), _held(DUST, 5.0, value=None)]
    report = diff([], holdings, quote_addresses=[USDC], rail=rail)
    assert any(MEME in m and "[rail store]" in m for m in report.mismatched)
    assert any(MEME2 in u and "[rail store]" in u for u in report.unbacked)
    assert any(DUST in m and "quarantined" in m for m in report.matched)
    assert report.unsolicited == []          # the quarantined row explains it
    assert report.rail_rows == 3


def test_ledger_and_rail_agreeing_adds_no_rail_line():
    led = f"## Open positions\n\n| T | A | S |\n|---|---|---|\n| MEME | {MEME} | 100 |\n"
    rows, _ = parse_open_positions(led)
    report = diff(rows, [_held(MEME, 100.0, value=10.0)], quote_addresses=[USDC],
                  rail=[RailPosition(address=MEME, symbol="MEME", qty=100.0)])
    assert report.verdict == "CLEAN" and len(report.matched) == 1
    assert "[ledger]" in report.matched[0]


@pytest.mark.asyncio
async def test_reconcile_action_reads_the_rail_store(home):
    apply_delta("owner-1", PositionDelta("base", MEME, "MEME", 100.0, 10.0), db_path=_db())
    (home / "ledger.md").write_text("## Open positions\n\n| T | A | S |\n|---|---|---|\n")
    # the index does NOT list MEME: the rail row must still be read -> chain zero
    tool = DefiDataTool(holder="0xHOLDER", index_fn=lambda h, chain: {},
                        identity_fn=lambda c, a: type("I", (), {"symbol": "MEME",
                                                               "decimals": 0})(),
                        price_fn=lambda c, a: _px(1.0))
    res = await tool.reconcile(ReconcileParams(chain="base",
                                               ledger_path=str(home / "ledger.md")),
                               execution_context=_ctx())
    assert res.error is None, res.error
    rep = res.metadata["report"]
    assert rep["unbacked"] and "[rail store]" in rep["unbacked"][0]
    assert "1 tracked row(s) on base compared" in res.extracted_content


@pytest.mark.parametrize('confidence', ['low', 'disputed', 'unknown', None])
def test_uncorroborated_prices_cannot_produce_barrier_figures(confidence):
    entry = open_positions.PositionEntry(chain='base', address=MEME, qty=100,
                                        entry_usd=100, high_water_usd=2, symbol='MEME', entry_ts=1)
    result = positions_view.figures(entry, _px(0.25, confidence))
    assert result.unrealized_pct is None
    assert result.unrealized_usd is None
    assert result.from_high_pct is None
    if confidence == 'low':
        assert result.value_usd == 25  # explicitly indicative, not a barrier
        assert 'independent executable exit quote' in positions_view.render([result], [], chain=None)
