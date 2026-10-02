"""050 §7.4 — the book gains a holder.

Before 050 the open-position PK was ``(user_id, chain, address)``: a token-bound account and the
treasury holding the same token would share ONE row, and an account's sell would close
the treasury's position. Pins:
 - a pre-050 store migrates once, every row becoming the treasury's (``account=''``),
   including on a READ (or the Money Book would read empty until the next trade);
 - the migration is idempotent and survives a second process;
 - the same token in the treasury and in a token-bound account are two rows, read separately;
 - ``seed_inherited`` records what an account held at adopt (no cost basis), refuses the
   treasury and never overwrites a tracked row;
 - ``PolicyGate.record(account=)`` stamps the audit entry and every position leg;
 - ``trade_index`` filters by holder.
"""
import json
import sqlite3

import pytest

from core.exec_identity import reset_exec_identity, set_exec_identity
from core.open_positions import (PositionDelta, _init, apply_delta, entries_for, get_position,
                                 seed_inherited)
from core.wallet import trade_index
from core.wallet.policy import PolicyGate

MEME = "0xb200000000000000000000ea8625786a776539fb"
NFT_ACCOUNT = "0xE0f47C083B28C76124129786cBd02a4489b86ec4"


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "open_positions.db")


def _pre050(db):
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE open_positions (
            user_id TEXT NOT NULL, chain TEXT NOT NULL, address TEXT NOT NULL, symbol TEXT,
            qty REAL NOT NULL, entry_usd REAL NOT NULL, entry_ts REAL NOT NULL,
            updated_ts REAL NOT NULL, PRIMARY KEY (user_id, chain, address));
        CREATE INDEX idx_open_positions_addr ON open_positions(user_id, address);
    """)
    conn.execute("INSERT INTO open_positions VALUES ('u1','base',?, 'MEME', 10, 5, 1, 1)", (MEME,))
    conn.commit()
    conn.close()


def test_a_pre050_store_migrates_on_read_and_rows_become_the_treasurys(db):
    _pre050(db)
    rows = entries_for("u1", db_path=db)
    assert MEME in rows and rows[MEME].account == "" and rows[MEME].origin == "trade"
    cols = [r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(open_positions)")]
    assert "account" in cols and "origin" in cols


def test_the_migration_is_idempotent(db):
    _pre050(db)
    _init(db)
    _init(db)
    assert get_position("u1", "base", MEME, db_path=db).qty == 10
    pk = [r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(open_positions)") if r[5]]
    assert pk == ["user_id", "chain", "account", "address"]


def test_treasury_and_account_hold_the_same_token_as_two_rows(db):
    apply_delta("u1", PositionDelta("robinhood", MEME, "MEME", 10, 5.0), db_path=db)
    apply_delta("u1", PositionDelta("robinhood", MEME, "MEME", 3, 1.0, account=NFT_ACCOUNT), db_path=db)
    # an account SELL closes the account's row, never the treasury's
    apply_delta("u1", PositionDelta("robinhood", MEME, "MEME", -3, account=NFT_ACCOUNT), db_path=db)
    assert get_position("u1", "robinhood", MEME, db_path=db).qty == 10
    assert get_position("u1", "robinhood", MEME, db_path=db, account=NFT_ACCOUNT) is None
    assert MEME in entries_for("u1", db_path=db)
    assert entries_for("u1", db_path=db, account=NFT_ACCOUNT) == {}


def test_inherited_positions_carry_no_cost_basis(db):
    assert seed_inherited("u1", chain="robinhood", account=NFT_ACCOUNT, address=MEME, symbol="MEME",
                          qty=7, db_path=db)
    pos = get_position("u1", "robinhood", MEME, db_path=db, account=NFT_ACCOUNT)
    # 071 W3: no basis is NULL (unknown), never a $0 basis
    assert pos.origin == "inherited" and pos.entry_usd is None and pos.qty == 7
    assert pos.account == NFT_ACCOUNT.lower()
    # never the treasury, never over a tracked row
    assert not seed_inherited("u1", chain="robinhood", account="", address=MEME, symbol="M", qty=1,
                              db_path=db)
    assert not seed_inherited("u1", chain="robinhood", account=NFT_ACCOUNT, address=MEME, symbol="M", qty=99,
                              db_path=db)
    assert get_position("u1", "robinhood", MEME, db_path=db, account=NFT_ACCOUNT).qty == 7


def test_policy_record_stamps_the_holder(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    gate = PolicyGate(max_per_tx_usd=1000.0)
    tok = set_exec_identity("u1", "s1")
    try:
        gate.record(venue="defi", action="swap", amount_usd=10.0, counterparty="0xspender",
                    idempotency_key=None, result_ref="0xacct", chain="robinhood",
                    positions=[PositionDelta("robinhood", MEME, "MEME", 5.0, 10.0)], account=NFT_ACCOUNT)
        gate.record(venue="defi", action="swap", amount_usd=10.0, counterparty="0xspender",
                    idempotency_key=None, result_ref="0xtreasury", chain="robinhood")
    finally:
        reset_exec_identity(tok)
    assert gate.audit_log[-2]["account"] == NFT_ACCOUNT.lower()
    assert "account" not in gate.audit_log[-1]  # byte-identical treasury entries
    path = str(tmp_path / "open_positions.db")
    assert get_position("u1", "robinhood", MEME, db_path=path, account=NFT_ACCOUNT).qty == 5.0
    assert get_position("u1", "robinhood", MEME, db_path=path) is None


def test_trade_index_filters_by_holder(tmp_path):
    ledger = tmp_path / "audit.jsonl"
    ledger.write_text("\n".join(json.dumps(e) for e in [
        {"venue": "defi", "result_ref": "0xAA"},
        {"venue": "defi", "result_ref": "0xBB", "account": NFT_ACCOUNT.lower()},
    ]) + "\n")
    p = str(ledger)
    assert trade_index.own_trade_tx_refs(path=p) == {"0xaa", "0xbb"}
    assert trade_index.own_trade_tx_refs(path=p, account="") == {"0xaa"}
    assert trade_index.own_trade_tx_refs(path=p, account=NFT_ACCOUNT) == {"0xbb"}
    assert trade_index.is_own_trade_tx("0xbb", path=p, account=NFT_ACCOUNT)
    assert not trade_index.is_own_trade_tx("0xbb", path=p, account="")
