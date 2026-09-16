"""The rail-written open-position store (043 A35) — core/open_positions.py.

Pins:
 - a recorded BUY opens a row with a cost basis + entry ts;
 - a second buy INCREASES it (cost basis sums, entry ts unchanged);
 - a partial SELL reduces qty AND cost basis in the same proportion;
 - a full SELL closes it (row deleted);
 - a sell of an untracked position is a NO-OP (never opens a negative one);
 - tenant scoping: one owner never sees another's positions;
 - an anonymous tenant never writes; a missing store reads empty, never created;
 - the swap classifier splits a trade into the right acquire/dispose legs and
   treats the quote asset (USDC) + wrapped native as working capital.
"""
import os

import pytest

from core.open_positions import (
    PositionDelta, apply_delta, apply_deltas, classify_swap, entries_for,
    get_position, open_positions_db_path,
)

# base USDC + WETH are pinned working capital; MEME is a made-up position token.
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH = "0x4200000000000000000000000000000000000006"
MEME = "0xb200000000000000000000ea8625786a776539fb"
MEME2 = "0xb200000000000000000000b344cb4a1e8bd51968"


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "open_positions.db")


# --------------------------------------------------------------------------
# store: open / increase / reduce / close
# --------------------------------------------------------------------------

def test_buy_opens_a_position_with_cost_basis(db):
    apply_delta("u1", PositionDelta(chain="base", address=MEME, symbol="MEME",
                                    qty=5000.0, cost_usd=100.0),
                db_path=db, now=1000.0)
    pos = get_position("u1", "base", MEME, db_path=db)
    assert pos is not None
    assert pos.qty == 5000.0
    assert pos.entry_usd == 100.0
    assert pos.entry_ts == 1000.0


def test_second_buy_increases_and_keeps_first_entry_ts(db):
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 5000.0, 100.0),
                db_path=db, now=1000.0)
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 5000.0, 60.0),
                db_path=db, now=2000.0)
    pos = get_position("u1", "base", MEME, db_path=db)
    assert pos.qty == 10000.0
    assert pos.entry_usd == 160.0          # cost basis sums
    assert pos.entry_ts == 1000.0          # first-open ts unchanged


def test_partial_sell_reduces_qty_and_cost_basis_proportionally(db):
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 10000.0, 200.0),
                db_path=db, now=1000.0)
    apply_delta("u1", PositionDelta("base", MEME, "MEME", -2500.0, None),
                db_path=db, now=2000.0)
    pos = get_position("u1", "base", MEME, db_path=db)
    assert pos.qty == 7500.0
    # 75% of the size remains -> 75% of the cost basis remains.
    assert pos.entry_usd == pytest.approx(150.0)


def test_full_sell_closes_the_position(db):
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 5000.0, 100.0),
                db_path=db, now=1000.0)
    apply_delta("u1", PositionDelta("base", MEME, "MEME", -5000.0, None),
                db_path=db, now=2000.0)
    assert get_position("u1", "base", MEME, db_path=db) is None


def test_oversell_closes_never_goes_negative(db):
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 5000.0, 100.0),
                db_path=db, now=1000.0)
    apply_delta("u1", PositionDelta("base", MEME, "MEME", -9999.0, None),
                db_path=db, now=2000.0)
    assert get_position("u1", "base", MEME, db_path=db) is None


def test_sell_of_untracked_position_is_a_noop(db):
    apply_delta("u1", PositionDelta("base", MEME, "MEME", -5000.0, None),
                db_path=db, now=1000.0)
    assert get_position("u1", "base", MEME, db_path=db) is None
    # No row was created, and no file need even exist for a clean read.
    assert entries_for("u1", db_path=db) == {}


# --------------------------------------------------------------------------
# tenancy, anonymity, missing store
# --------------------------------------------------------------------------

def test_positions_are_tenant_scoped(db):
    apply_delta("owner", PositionDelta("base", MEME, "MEME", 5000.0, 100.0),
                db_path=db, now=1000.0)
    assert get_position("owner", "base", MEME, db_path=db) is not None
    assert get_position("intruder", "base", MEME, db_path=db) is None
    assert entries_for("intruder", db_path=db) == {}


def test_anonymous_tenant_never_writes(db):
    apply_delta("", PositionDelta("base", MEME, "MEME", 5000.0, 100.0),
                db_path=db, now=1000.0)
    apply_delta("   ", PositionDelta("base", MEME, "MEME", 5000.0, 100.0),
                db_path=db, now=1000.0)
    # Nothing was written for an owner-less position.
    assert entries_for("", db_path=db) == {}
    assert not os.path.isfile(db)


def test_read_never_creates_the_store(tmp_path):
    missing = str(tmp_path / "nope.db")
    assert get_position("u1", "base", MEME, db_path=missing) is None
    assert entries_for("u1", db_path=missing) == {}
    assert not os.path.isfile(missing)     # a read must not leave an empty db


def test_entries_for_keys_by_normalized_address(db):
    apply_delta("u1", PositionDelta("base", MEME.upper(), "MEME", 5000.0, 100.0),
                db_path=db, now=1000.0)
    ents = entries_for("u1", db_path=db)
    # keyed by the normalized (lowercased) address, matching the ledger join.
    assert MEME.lower() in ents
    assert ents[MEME.lower()].entry_usd == 100.0


def test_open_positions_db_path_uses_data_dir(tmp_path):
    p = open_positions_db_path(str(tmp_path))
    assert p == os.path.join(str(tmp_path), "open_positions.db")


# --------------------------------------------------------------------------
# the swap classifier (PURE)
# --------------------------------------------------------------------------

def test_classify_stable_to_meme_is_a_single_buy():
    deltas = classify_swap(chain="base", token_in=USDC, token_out=MEME,
                           in_native=False, in_symbol="USDC", out_symbol="MEME",
                           in_qty=100.0, out_qty=5000.0, cost_usd=100.0)
    assert len(deltas) == 1
    d = deltas[0]
    assert d.address == MEME.lower() and d.qty == 5000.0 and d.cost_usd == 100.0


def test_classify_meme_to_stable_is_a_single_sell():
    deltas = classify_swap(chain="base", token_in=MEME, token_out=USDC,
                           in_native=False, in_symbol="MEME", out_symbol="USDC",
                           in_qty=5000.0, out_qty=98.0, cost_usd=98.0)
    assert len(deltas) == 1
    d = deltas[0]
    assert d.address == MEME.lower() and d.qty == -5000.0 and d.cost_usd is None


def test_classify_native_to_meme_is_a_buy():
    deltas = classify_swap(chain="base", token_in="native", token_out=MEME,
                           in_native=True, in_symbol="ETH", out_symbol="MEME",
                           in_qty=0.05, out_qty=5000.0, cost_usd=120.0)
    assert len(deltas) == 1
    assert deltas[0].address == MEME.lower() and deltas[0].qty == 5000.0


def test_classify_meme_to_meme_closes_one_opens_other():
    deltas = classify_swap(chain="base", token_in=MEME, token_out=MEME2,
                           in_native=False, in_symbol="MEME", out_symbol="MEME2",
                           in_qty=5000.0, out_qty=7000.0, cost_usd=90.0)
    by_addr = {d.address: d for d in deltas}
    assert by_addr[MEME.lower()].qty == -5000.0
    assert by_addr[MEME2.lower()].qty == 7000.0
    assert by_addr[MEME2.lower()].cost_usd == 90.0


def test_classify_value_to_value_tracks_nothing():
    deltas = classify_swap(chain="base", token_in=USDC, token_out=WETH,
                           in_native=False, in_symbol="USDC", out_symbol="WETH",
                           in_qty=100.0, out_qty=0.03, cost_usd=100.0)
    assert deltas == []


def test_classify_unknown_out_qty_drops_the_buy_leg():
    deltas = classify_swap(chain="base", token_in=USDC, token_out=MEME,
                           in_native=False, in_symbol="USDC", out_symbol="MEME",
                           in_qty=100.0, out_qty=None, cost_usd=100.0)
    # no size for the acquired position -> not written with an unknown qty.
    assert deltas == []


def test_apply_deltas_round_trips_a_meme_to_meme_swap(db):
    deltas = classify_swap(chain="base", token_in=MEME, token_out=MEME2,
                           in_native=False, in_symbol="MEME", out_symbol="MEME2",
                           in_qty=5000.0, out_qty=7000.0, cost_usd=90.0)
    # seed the MEME position so the dispose leg has something to reduce
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 5000.0, 80.0),
                db_path=db, now=1000.0)
    apply_deltas("u1", deltas, db_path=db, now=2000.0)
    assert get_position("u1", "base", MEME, db_path=db) is None      # fully sold
    opened = get_position("u1", "base", MEME2, db_path=db)
    assert opened is not None and opened.qty == 7000.0 and opened.entry_usd == 90.0
