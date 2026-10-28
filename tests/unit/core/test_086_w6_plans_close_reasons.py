"""086 W6 — plan on a mandate entry, typed close reasons on the realized table.

Pins:
 - a sell's ``close_reason`` reaches ``position_realized`` (the latest leg's);
 - an older realized table gains the column IN PLACE, rows kept, reason '';
 - ``normalize_close_reason`` takes the typed words and ``breaker:<name>`` only;
 - a mandate ENTRY without a plan is refused before any quote; an exit is not;
   no mandate = no requirement; an invalid plan is refused either way;
 - ``record_after_entry`` keys the plan by the book row's entry_ts; ``plan_for``
   reads it; an absent store is no plan and never created by a read.
"""
import os
import sqlite3
from types import SimpleNamespace

import pytest

from core.open_positions import PositionDelta, apply_delta, classify_swap, realized_for
from core import position_plans as pp

MEME = "0xb200000000000000000000ea8625786a776539fb"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PLAN = {"thesis": "volume breakout", "confidence": 0.6,
        "invalidation": "loses the 1h low", "preset": "memecoin"}


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "open_positions.db")


def _buy(db, qty=10.0, cost=10.0, now=100.0):
    apply_delta("u1", PositionDelta(chain="base", address=MEME, symbol="MEME", qty=qty,
                                    cost_usd=cost), db_path=db, now=now)


def test_close_reason_reaches_realized(db):
    _buy(db)
    apply_delta("u1", PositionDelta(chain="base", address=MEME, symbol="MEME", qty=-4,
                                    proceeds_usd=2.0, close_reason="stop_loss"),
                db_path=db, now=200.0)
    (r,) = realized_for("u1", db_path=db)
    assert r.close_reason == "stop_loss"
    apply_delta("u1", PositionDelta(chain="base", address=MEME, symbol="MEME", qty=-6,
                                    proceeds_usd=9.0, close_reason="take_profit"),
                db_path=db, now=300.0)
    (r,) = realized_for("u1", db_path=db)
    assert r.close_reason == "take_profit" and r.known_legs == 2


def test_classify_swap_carries_close_reason():
    legs = classify_swap(chain="base", token_in=MEME, token_out=USDC, in_native=False,
                         in_symbol="MEME", out_symbol="USDC", in_qty=5.0, out_qty=4.0,
                         cost_usd=4.0, close_reason="trailing")
    (sell,) = [d for d in legs if d.qty < 0]
    assert sell.close_reason == "trailing"


def test_old_realized_table_gains_column_in_place(db):
    _buy(db)
    conn = sqlite3.connect(db)
    conn.executescript("""
        DROP TABLE position_realized;
        CREATE TABLE position_realized (
            user_id TEXT NOT NULL, chain TEXT NOT NULL, account TEXT NOT NULL DEFAULT '',
            address TEXT NOT NULL, symbol TEXT, realized_usd REAL NOT NULL DEFAULT 0,
            known_legs INTEGER NOT NULL DEFAULT 0, unknown_legs INTEGER NOT NULL DEFAULT 0,
            sold_qty REAL NOT NULL DEFAULT 0, updated_ts REAL NOT NULL,
            PRIMARY KEY (user_id, chain, account, address));
    """)
    conn.execute("INSERT INTO position_realized VALUES ('u1','base','',?,'MEME',1.5,1,0,2,5)",
                 (MEME,))
    conn.commit()
    conn.close()
    (r,) = realized_for("u1", db_path=db)
    assert r.close_reason == "" and r.realized_usd == 1.5
    cols = [c[1] for c in sqlite3.connect(db).execute("PRAGMA table_info(position_realized)")]
    assert "close_reason" in cols


@pytest.mark.parametrize("word,out", [("stop_loss", "stop_loss"), ("", ""), (None, ""),
                                      ("Breaker:daily_loss", "breaker:daily_loss"),
                                      ("orphan", "orphan")])
def test_normalize_close_reason(word, out):
    assert pp.normalize_close_reason(word) == out


@pytest.mark.parametrize("word", ["vibes", "breaker:", "breaker:a b"])
def test_normalize_close_reason_refuses(word):
    with pytest.raises(ValueError):
        pp.normalize_close_reason(word)


def _ctx(mandate=None):
    return SimpleNamespace(metadata={"mandate": mandate} if mandate else {})


def test_mandate_entry_without_plan_is_refused():
    why = pp.plan_refusal(_ctx("m1"), None, chain="base", token_in=USDC, token_out=MEME,
                          in_native=False)
    assert why and "m1" in why and "plan" in why and "\n" not in why


def test_mandate_exit_needs_no_plan():
    assert pp.plan_refusal(_ctx("m1"), None, chain="base", token_in=MEME, token_out=USDC,
                           in_native=False) is None


def test_no_mandate_needs_no_plan():
    assert pp.plan_refusal(_ctx(), None, chain="base", token_in=USDC, token_out=MEME,
                           in_native=False) is None


def test_mandate_entry_with_plan_passes_and_bad_plan_refused():
    assert pp.plan_refusal(_ctx("m1"), PLAN, chain="base", token_in=USDC, token_out=MEME,
                           in_native=False) is None
    bad = dict(PLAN, confidence=1.5)
    why = pp.plan_refusal(_ctx(), bad, chain="base", token_in=USDC, token_out=MEME,
                          in_native=False)
    assert why and "confidence" in why


def test_record_after_entry_and_plan_for(db, tmp_path):
    plans = str(tmp_path / "trading.db")
    assert pp.plan_for("u1", "base", MEME, path=plans) is None
    assert not os.path.exists(plans)  # a read never creates the store
    assert not pp.record_after_entry("u1", chain="base", address=MEME, plan=PLAN,
                                     book_path=db, path=plans)  # no row yet
    _buy(db, now=123.0)
    assert pp.record_after_entry("u1", chain="base", address=MEME.upper().replace("0X", "0x"),
                                 plan=PLAN, mandate_id="m1", book_path=db, path=plans)
    p = pp.plan_for("u1", "BASE", MEME, path=plans)
    assert p.thesis == "volume breakout" and p.entry_ts == 123.0 and p.mandate_id == "m1"
    assert pp.plan_for("u1", "base", MEME, entry_ts=123.0, path=plans) is not None
    assert pp.plan_for("u1", "base", MEME, entry_ts=999.0, path=plans) is None
    assert pp.plan_for("u2", "base", MEME, path=plans) is None
    assert "stops only tighten" in p.render()
