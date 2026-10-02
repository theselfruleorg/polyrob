"""071 W3 — one book: nullable basis, realized P&L, qty source, high-water.

Pins:
 - an existing store with ``entry_usd NOT NULL`` (the shape on prod) upgrades IN
   PLACE on the next read or write: every row and column survives, an
   ``inherited`` row's placeholder ``0`` becomes NULL, a trade row keeps its basis;
 - the upgrade is idempotent (a second process re-checks inside the lock);
 - an unknown cost is a NULL basis, and stays NULL once any leg is unknown;
 - a dispose books realized P&L by AVERAGE COST, and it outlives a full close;
 - an unknown basis or proceeds is an unknown LEG, never a $0 one;
 - ``qty_source`` records measured (receipt) vs quote; mixed legs read ``quote``;
 - ``observe_price`` only raises the high-water mark and never creates a store;
 - ``positions_for`` keeps the chain and is STRICT on an unreadable store.
"""
import sqlite3

import pytest

from core.open_positions import (
    PositionDelta, _init, apply_delta, classify_swap, entries_for, get_position,
    observe_price, positions_for, realized_for,
)

MEME = "0xb200000000000000000000ea8625786a776539fb"
MEME2 = "0xb200000000000000000000b344cb4a1e8bd51968"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ACCT = "0xe0f47c083b28c76124129786cbd02a4489b86ec4"


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "open_positions.db")


def _prod_shape(db):
    """The post-W0 / pre-071 store: entry_usd NOT NULL, no 071 columns."""
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE open_positions (
            user_id TEXT NOT NULL, chain TEXT NOT NULL, account TEXT NOT NULL DEFAULT '',
            address TEXT NOT NULL, symbol TEXT, qty REAL NOT NULL, entry_usd REAL NOT NULL,
            entry_ts REAL NOT NULL, updated_ts REAL NOT NULL,
            origin TEXT NOT NULL DEFAULT 'trade',
            status TEXT NOT NULL DEFAULT 'open', status_reason TEXT NOT NULL DEFAULT '',
            status_ts REAL,
            PRIMARY KEY (user_id, chain, account, address));
        CREATE INDEX idx_open_positions_addr ON open_positions(user_id, address);
    """)
    conn.execute("INSERT INTO open_positions VALUES "
                 "('u1','base','',?,'MEME',10,134.54,1,2,'trade','quarantined','look-alike',3)",
                 (MEME,))
    conn.execute("INSERT INTO open_positions VALUES "
                 "('u1','robinhood',?,?,'INH',7,0,1,1,'inherited','open','',NULL)",
                 (ACCT, MEME2))
    conn.commit()
    conn.close()


def _notnull(db):
    return {r[1]: r[3] for r in sqlite3.connect(db).execute("PRAGMA table_info(open_positions)")}


# -------------------------------------------------------------- migration

def test_prod_shape_store_upgrades_in_place_on_read(db):
    _prod_shape(db)
    rows = entries_for("u1", db_path=db)
    pos = rows[MEME]
    assert pos.entry_usd == pytest.approx(134.54) and pos.qty == 10
    assert pos.status == "quarantined" and pos.status_reason == "look-alike"
    assert pos.qty_source == ""          # written before the column existed
    assert _notnull(db)["entry_usd"] == 0
    inh = get_position("u1", "robinhood", MEME2, db_path=db, account=ACCT)
    assert inh.origin == "inherited" and inh.entry_usd is None   # 0 was a placeholder
    idx = [r[1] for r in sqlite3.connect(db).execute("PRAGMA index_list(open_positions)")]
    assert "idx_open_positions_addr" in idx
    tables = {r[0] for r in sqlite3.connect(db).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "open_positions_pre071" not in tables


def test_upgrade_is_idempotent_and_keeps_rows(db):
    _prod_shape(db)
    _init(db)
    _init(db)
    assert get_position("u1", "base", MEME, db_path=db).entry_usd == pytest.approx(134.54)
    pk = [r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(open_positions)") if r[5]]
    assert pk == ["user_id", "chain", "account", "address"]


def test_upgrade_on_write_then_a_sell_books_realized(db):
    _prod_shape(db)
    apply_delta("u1", PositionDelta("base", MEME, "MEME", -5, proceeds_usd=100.0),
                db_path=db, now=10.0)
    pos = get_position("u1", "base", MEME, db_path=db)
    assert pos.qty == 5 and pos.entry_usd == pytest.approx(67.27)
    (r,) = realized_for("u1", db_path=db)
    assert r.realized_usd == pytest.approx(100.0 - 67.27) and r.known_legs == 1


# -------------------------------------------------------------- unknown basis

def test_unknown_cost_is_a_null_basis(db):
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 10, None), db_path=db)
    assert get_position("u1", "base", MEME, db_path=db).entry_usd is None


def test_a_basis_part_unknown_stays_unknown(db):
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 10, 50.0), db_path=db)
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 10, None), db_path=db)
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 10, 50.0), db_path=db)
    pos = get_position("u1", "base", MEME, db_path=db)
    assert pos.qty == 30 and pos.entry_usd is None


def test_classify_reads_a_non_positive_value_as_unknown_and_sets_proceeds():
    buy = classify_swap(chain="base", token_in=USDC, token_out=MEME, in_native=False,
                        in_symbol="USDC", out_symbol="MEME", in_qty=10, out_qty=5,
                        cost_usd=0.0, qty_source="receipt")
    assert buy[0].cost_usd is None and buy[0].qty_source == "receipt"
    sell = classify_swap(chain="base", token_in=MEME, token_out=USDC, in_native=False,
                         in_symbol="MEME", out_symbol="USDC", in_qty=5, out_qty=12,
                         cost_usd=12.0)
    assert sell[0].qty == -5 and sell[0].proceeds_usd == 12.0
    assert sell[0].qty_source == "quote"     # the default: not measured


# -------------------------------------------------------------- realized (average cost)

def test_realized_is_average_cost_and_outlives_a_full_close(db):
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 100, 100.0), db_path=db)
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 100, 300.0), db_path=db)
    # avg cost $2/token: sell 50 for $150 -> +$50
    apply_delta("u1", PositionDelta("base", MEME, "MEME", -50, proceeds_usd=150.0), db_path=db)
    # sell the remaining 150 for $240 -> basis $300 -> -$60
    apply_delta("u1", PositionDelta("base", MEME, "MEME", -150, proceeds_usd=240.0), db_path=db)
    assert get_position("u1", "base", MEME, db_path=db) is None
    (r,) = realized_for("u1", db_path=db)
    assert r.realized_usd == pytest.approx(-10.0)
    assert r.known_legs == 2 and r.unknown_legs == 0 and r.sold_qty == 200


def test_oversell_realizes_only_the_tracked_size(db):
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 10, 10.0), db_path=db)
    apply_delta("u1", PositionDelta("base", MEME, "MEME", -20, proceeds_usd=40.0), db_path=db)
    (r,) = realized_for("u1", db_path=db)
    assert r.realized_usd == pytest.approx(20.0 - 10.0) and r.sold_qty == 10


def test_unknown_basis_or_proceeds_is_an_unknown_leg(db):
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 10, None), db_path=db)
    apply_delta("u1", PositionDelta("base", MEME, "MEME", -5, proceeds_usd=9.0), db_path=db)
    apply_delta("u1", PositionDelta("base", MEME2, "M2", 10, 10.0), db_path=db)
    apply_delta("u1", PositionDelta("base", MEME2, "M2", -5), db_path=db)
    rows = {r.address: r for r in realized_for("u1", db_path=db)}
    for addr in (MEME, MEME2):
        assert rows[addr].unknown_legs == 1 and rows[addr].known_legs == 0
        assert rows[addr].realized_usd == 0.0


def test_untracked_sell_books_nothing(db):
    apply_delta("u1", PositionDelta("base", MEME2, "M2", 1, 1.0), db_path=db)
    apply_delta("u1", PositionDelta("base", MEME, "MEME", -5, proceeds_usd=9.0), db_path=db)
    assert realized_for("u1", db_path=db) == []


# -------------------------------------------------------------- qty source, high-water

def test_qty_source_measured_then_quoted_reads_quote(db):
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 1, 1.0, qty_source="receipt"),
                db_path=db)
    assert get_position("u1", "base", MEME, db_path=db).qty_source == "receipt"
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 1, 1.0, qty_source="quote"),
                db_path=db)
    assert get_position("u1", "base", MEME, db_path=db).qty_source == "quote"


def test_observe_price_only_raises_the_high_water(db, tmp_path):
    assert observe_price("u1", "base", MEME, 2.0, db_path=str(tmp_path / "none.db")) is None
    assert not (tmp_path / "none.db").exists()
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 1, 1.0), db_path=db)
    assert observe_price("u1", "base", MEME, 2.0, db_path=db, now=5.0) == 2.0
    assert observe_price("u1", "base", MEME, 1.5, db_path=db, now=6.0) == 2.0
    pos = get_position("u1", "base", MEME, db_path=db)
    assert pos.high_water_usd == 2.0 and pos.high_water_ts == 5.0
    # a later buy keeps the mark
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 1, 1.0), db_path=db)
    assert get_position("u1", "base", MEME, db_path=db).high_water_usd == 2.0


def test_positions_for_keeps_chain_and_is_strict(db, tmp_path):
    apply_delta("u1", PositionDelta("base", MEME, "MEME", 1, 1.0), db_path=db)
    apply_delta("u1", PositionDelta("arbitrum", MEME, "MEME", 2, 3.0), db_path=db)
    rows = positions_for("u1", db_path=db)
    assert {(r.chain, r.qty) for r in rows} == {("base", 1.0), ("arbitrum", 2.0)}
    assert [r.chain for r in positions_for("u1", chain="arbitrum", db_path=db)] == ["arbitrum"]
    assert positions_for("u1", db_path=str(tmp_path / "absent.db")) == []
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database at all, just bytes" * 10)
    with pytest.raises(Exception):
        positions_for("u1", db_path=str(bad))
