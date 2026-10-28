"""086 W0 "book truth": the classifier books the major<->stable leg and the
Solana mints, the book has one dust threshold, and a zero-balance row can be
retired with proof.

Pins:
 - USDC -> WETH opens a WETH position (cost = the swap value); WETH -> USDC
   closes it and books realized P&L — the leg the 076 majors lab trades;
 - native ETH in keys the same row as the wrapped native;
 - a major spent on a memecoin reduces a held major row (no-op when none);
 - a memecoin sold INTO a major opens no major row (an exit is not a buy);
 - a wrap (native -> its own wrapped token) tracks nothing;
 - Solana: USDC/wSOL are working capital; SOL -> MEME opens MEME, MEME -> SOL
   closes it with realized P&L; USDC -> wSOL opens a SOL row.
"""
import pytest

from core.open_positions import (
    PositionDelta, apply_delta, apply_deltas, classify_swap, get_position,
    realized_for,
)

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH = "0x4200000000000000000000000000000000000006"
MEME = "0xb200000000000000000000ea8625786a776539fb"

SOL_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"
SOL_MEME = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "open_positions.db")


def _swap(chain, tin, tout, in_qty, out_qty, usd, *, native=False):
    return classify_swap(chain=chain, token_in=tin, token_out=tout, in_native=native,
                         in_symbol="IN", out_symbol="OUT", in_qty=in_qty,
                         out_qty=out_qty, cost_usd=usd)


def test_stable_to_major_opens_the_major():
    legs = _swap("base", USDC, WETH, 100.0, 0.03, 100.0)
    assert len(legs) == 1
    assert legs[0].address == WETH.lower() and legs[0].qty == 0.03
    assert legs[0].cost_usd == 100.0


def test_major_to_stable_closes_it_with_realized_pnl(db):
    apply_deltas("u1", _swap("base", USDC, WETH, 100.0, 0.03, 100.0), db_path=db)
    legs = _swap("base", WETH, USDC, 0.03, 120.0, 120.0)
    assert [(d.address, d.qty, d.proceeds_usd) for d in legs] == [(WETH.lower(), -0.03, 120.0)]
    apply_deltas("u1", legs, db_path=db)
    assert get_position("u1", "base", WETH, db_path=db) is None
    (r,) = realized_for("u1", db_path=db)
    assert r.realized_usd == pytest.approx(20.0) and r.known_legs == 1


def test_native_in_keys_the_wrapped_native_row():
    legs = _swap("base", "native", USDC, 0.03, 120.0, 120.0, native=True)
    assert [(d.address, d.qty) for d in legs] == [(WETH.lower(), -0.03)]


def test_major_spent_on_a_memecoin_reduces_a_held_major(db):
    apply_deltas("u1", _swap("base", USDC, WETH, 100.0, 0.04, 100.0), db_path=db)
    legs = _swap("base", WETH, MEME, 0.01, 5000.0, 30.0)
    assert {d.address: d.qty for d in legs} == {MEME.lower(): 5000.0, WETH.lower(): -0.01}
    apply_deltas("u1", legs, db_path=db)
    assert get_position("u1", "base", WETH, db_path=db).qty == pytest.approx(0.03)


def test_a_memecoin_sold_into_a_major_opens_no_major_row():
    legs = _swap("base", MEME, WETH, 5000.0, 0.01, 30.0)
    assert [(d.address, d.qty) for d in legs] == [(MEME.lower(), -5000.0)]


def test_a_wrap_tracks_nothing():
    assert _swap("base", "native", WETH, 0.1, 0.1, 300.0, native=True) == []


def test_stable_to_stable_tracks_nothing():
    assert _swap("solana", SOL_USDC, SOL_USDC, 1.0, 1.0, 1.0) == []


def test_solana_sol_to_meme_opens_and_meme_to_sol_closes_with_pnl(db):
    buy = _swap("solana", WSOL, SOL_MEME, 0.5, 1_000_000.0, 75.0)
    # The SOL leg is a dispose: a no-op while no SOL row is held.
    assert {d.address: d.qty for d in buy} == {SOL_MEME: 1_000_000.0, WSOL: -0.5}
    apply_deltas("u1", buy, db_path=db)
    sell = _swap("solana", SOL_MEME, WSOL, 1_000_000.0, 0.4, 60.0)
    assert [(d.address, d.qty) for d in sell] == [(SOL_MEME, -1_000_000.0)]
    apply_deltas("u1", sell, db_path=db)
    assert get_position("u1", "solana", SOL_MEME, db_path=db) is None
    (r,) = realized_for("u1", chain="solana", db_path=db)
    assert r.realized_usd == pytest.approx(-15.0)


def test_solana_usdc_to_sol_opens_a_sol_row():
    legs = _swap("solana", SOL_USDC, WSOL, 150.0, 1.0, 150.0)
    assert [(d.address, d.qty, d.cost_usd) for d in legs] == [(WSOL, 1.0, 150.0)]


def test_a_base58_mint_keeps_its_case(db):
    apply_delta("u1", PositionDelta("solana", SOL_MEME, "M", 5.0, 1.0), db_path=db)
    assert get_position("u1", "solana", SOL_MEME, db_path=db).address == SOL_MEME


# --------------------------------------------------------------------------
# one dust threshold, the dust/retired statuses, the positions_for filter
# --------------------------------------------------------------------------

def test_one_dust_threshold():
    from core import open_positions
    from tools.defi import positions_view, reconcile
    assert open_positions.DUST_USD == 1.0
    assert positions_view.DUST_USD is open_positions.DUST_USD
    assert reconcile.DUST_VALUE_USD is open_positions.DUST_USD


def test_dust_and_retired_are_stored_statuses(db):
    from core import open_positions as op
    assert {op.STATUS_DUST, op.STATUS_RETIRED} <= set(op.STATUSES)
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 10.0, 5.0), db_path=db)
    assert op.set_status("u1", "base", MEME, op.STATUS_DUST, reason="$0.40", db_path=db)
    assert op.get_position("u1", "base", MEME, db_path=db).status == "dust"


def test_positions_for_excludes_inactive_rows_by_default(db):
    from core import open_positions as op
    for addr, status in ((MEME, None), (WETH, op.STATUS_WRITTEN_OFF),
                         (USDC, op.STATUS_RETIRED), (SOL_MEME, op.STATUS_DUST)):
        chain = "solana" if addr == SOL_MEME else "base"
        addr = addr if chain == "solana" else addr.lower()
        apply_delta("u1", PositionDelta(chain, addr, "X", 1.0, 1.0), db_path=db)
        if status:
            op.set_status("u1", chain, addr, status, db_path=db)
    assert [r.address for r in op.positions_for("u1", db_path=db)] == [MEME.lower()]
    every = op.positions_for("u1", db_path=db, statuses=op.STATUSES)
    assert len(every) == 4


def test_a_buy_after_retire_starts_a_fresh_row(db):
    from core import open_positions as op
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 10.0, 5.0), db_path=db)
    op.set_status("u1", "base", MEME, op.STATUS_RETIRED, db_path=db)
    # A sell of a retired row is a no-op (nothing is held on the book).
    apply_delta("u1", PositionDelta("base", MEME, "MEME", -3.0, proceeds_usd=1.0), db_path=db)
    assert realized_for("u1", db_path=db) == []
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 4.0, 2.0), db_path=db)
    pos = op.get_position("u1", "base", MEME, db_path=db)
    assert (pos.status, pos.qty, pos.entry_usd) == ("open", 4.0, 2.0)


def test_a_buy_on_a_dust_row_reopens_it(db):
    from core import open_positions as op
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 10.0, 5.0), db_path=db)
    op.set_status("u1", "base", MEME, op.STATUS_DUST, db_path=db)
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 4.0, 2.0), db_path=db)
    pos = op.get_position("u1", "base", MEME, db_path=db)
    assert (pos.status, pos.qty, pos.entry_usd) == ("open", 14.0, 7.0)
