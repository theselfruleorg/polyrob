"""W0: a position has a lifecycle — ``open | quarantined | written_off``.

A look-alike the identity gate quarantines keeps its cost basis (honest P&L)
but is no longer a symbol claim. Pre-W0 stores upgrade in place; every old row
reads ``open``.
"""
import sqlite3

import pytest

from core import open_positions as op

FAKE = "0x357A04366240aa3c9d916Aa0F15c3033686C9007"


def _buy(db, qty=10.0, cost=134.54):
    op.apply_delta("rob", op.PositionDelta(chain="robinhood", address=FAKE.lower(),
                                           symbol="PNL", qty=qty, cost_usd=cost),
                   db_path=db)


def test_a_new_row_is_open_and_can_be_quarantined_keeping_its_cost(tmp_path):
    db = str(tmp_path / "open_positions.db")
    _buy(db)
    e = op.entries_for("rob", db_path=db)[FAKE.lower()]
    assert e.status == "open"
    assert op.set_status("rob", "robinhood", FAKE, "quarantined", reason="look-alike", db_path=db)
    e = op.entries_for("rob", db_path=db)[FAKE.lower()]
    assert e.status == "quarantined" and e.status_reason == "look-alike"
    assert e.entry_usd == pytest.approx(134.54) and e.qty == 10.0
    # idempotent: a second quarantine changes nothing
    assert op.set_status("rob", "robinhood", FAKE, "quarantined", db_path=db) is False


def test_a_later_buy_does_not_lift_the_quarantine(tmp_path):
    db = str(tmp_path / "open_positions.db")
    _buy(db)
    op.set_status("rob", "robinhood", FAKE, "quarantined", reason="x", db_path=db)
    _buy(db, qty=1.0, cost=1.0)
    e = op.get_position("rob", "robinhood", FAKE, db_path=db)
    assert e.status == "quarantined" and e.qty == 11.0


def test_an_unknown_status_is_refused(tmp_path):
    with pytest.raises(ValueError):
        op.set_status("rob", "robinhood", FAKE, "gone", db_path=str(tmp_path / "x.db"))


def test_set_status_never_creates_the_store(tmp_path):
    db = tmp_path / "open_positions.db"
    assert op.set_status("rob", "robinhood", FAKE, "quarantined", db_path=str(db)) is False
    assert not db.exists()


def test_a_pre_w0_store_upgrades_and_reads_open(tmp_path):
    db = str(tmp_path / "open_positions.db")
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE open_positions (
        user_id TEXT NOT NULL, chain TEXT NOT NULL, account TEXT NOT NULL DEFAULT '',
        address TEXT NOT NULL, symbol TEXT, qty REAL NOT NULL, entry_usd REAL NOT NULL,
        entry_ts REAL NOT NULL, updated_ts REAL NOT NULL,
        origin TEXT NOT NULL DEFAULT 'trade',
        PRIMARY KEY (user_id, chain, account, address))""")
    conn.execute("INSERT INTO open_positions VALUES ('rob','robinhood','',?, 'PNL', 5, 134.54, 1, 1, 'trade')",
                 (FAKE.lower(),))
    conn.commit()
    conn.close()
    e = op.entries_for("rob", db_path=db, strict=True)[FAKE.lower()]
    assert e.status == "open" and e.entry_usd == pytest.approx(134.54)
    assert op.set_status("rob", "robinhood", FAKE, "quarantined", db_path=db)
    assert op.entries_for("rob", db_path=db)[FAKE.lower()].status == "quarantined"
