"""115 W5 — the payer addresses of this instance's COMPLETED invoices.

One store knows a payer address: ``x402_payment_requests`` in ``bot.db`` (the
``payer_address`` column, or ``metadata.payer_address`` stamped by the on-chain
settlement scan). Only a completed payable row counts; a refund, a pending row, a
machine payment and another chain family do not. An unreadable store raises — it
is never "none paid".
"""
import json
import sqlite3

import pytest

from modules.x402.paid_payers import PaidPayersUnreadable, paid_payer_addresses

A1, A2, A3, A4 = ("0x" + h * 20 for h in ("a1", "a2", "a3", "a4"))
SOL = "7EcDhSYGxXyscszYEp35KHN8vvw3svAuLKTzXwCFLtV"


def _db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE x402_payment_requests (
        id TEXT PRIMARY KEY, payer_address TEXT, chain TEXT NOT NULL,
        status TEXT, metadata TEXT)""")
    for i, (payer, chain, status, meta) in enumerate(rows):
        conn.execute("INSERT INTO x402_payment_requests VALUES (?,?,?,?,?)",
                     (f"r{i}", payer, chain, status, json.dumps(meta)))
    conn.commit()
    conn.close()
    return str(path)


def test_completed_invoices_and_orders_name_their_payers(tmp_path):
    path = _db(tmp_path / "bot.db", [
        (A1.upper().replace("0X", "0x"), "base", "completed", {"kind": "agent_invoice"}),
        (None, "robinhood", "completed", {"kind": "offer_order", "payer_address": A2}),
        (None, "base-sepolia", "completed", {"kind": "room_action", "payer_address": A4}),
    ])
    assert paid_payer_addresses(db_path=path) == frozenset({A1, A2, A4})


def test_unpaid_refunded_and_machine_rows_do_not_count(tmp_path):
    path = _db(tmp_path / "bot.db", [
        (A1, "base", "pending", {"kind": "agent_invoice"}),
        (A2, "base", "refunded", {"kind": "offer_order"}),
        (A3, "base", "refund_due", {"kind": "offer_order"}),
        (A4, "base", "completed", {"kind": "machine_payment"}),
        (A4, "base", "settled_no_tx", {"kind": "agent_invoice"}),
    ])
    assert paid_payer_addresses(db_path=path) == frozenset()


def test_another_chain_family_does_not_count(tmp_path):
    path = _db(tmp_path / "bot.db", [
        (SOL, "solana", "completed", {"kind": "agent_invoice"}),
        (A1, "solana", "completed", {"kind": "agent_invoice"}),
    ])
    assert paid_payer_addresses(db_path=path) == frozenset()
    assert paid_payer_addresses(db_path=path, family="svm") == frozenset({SOL})


def test_a_missing_store_holds_nothing(tmp_path):
    missing = tmp_path / "database" / "bot.db"
    assert paid_payer_addresses(db_path=str(missing)) == frozenset()
    assert not missing.exists(), "a read never creates the store"


def test_a_corrupt_store_is_unreadable_never_none_paid(tmp_path):
    bad = tmp_path / "bot.db"
    bad.write_bytes(b"not a sqlite file at all" * 100)
    with pytest.raises(PaidPayersUnreadable):
        paid_payer_addresses(db_path=str(bad))


def test_a_store_without_the_invoice_table_is_unreadable(tmp_path):
    path = tmp_path / "bot.db"
    sqlite3.connect(path).execute("CREATE TABLE other (x)").connection.close()
    with pytest.raises(PaidPayersUnreadable):
        paid_payer_addresses(db_path=str(path))


def test_the_default_path_is_the_one_bot_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bot.db"))
    _db(tmp_path / "bot.db", [(A3, "base", "completed", {"kind": "agent_invoice"})])
    assert paid_payer_addresses() == frozenset({A3})
