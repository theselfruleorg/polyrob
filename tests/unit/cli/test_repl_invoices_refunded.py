"""REPL parity: `/invoices refunded <id> [tx]` RECORDS a refund on the REPL, as on
Telegram (the same shared reply, over the REPL's own bot.db); bare
`/invoices refunded` still lists the refunded rows."""
from tests.unit.cli.test_repl_owner_verbs import _ctx, _invoice_db  # noqa: F401


def _status(db_path, rid):
    import sqlite3
    with sqlite3.connect(db_path) as c:
        return c.execute("SELECT status FROM x402_payment_requests WHERE id=?",
                         (rid,)).fetchone()[0]


def _owe(db_path, rid):
    import sqlite3
    with sqlite3.connect(db_path) as c:
        c.execute("UPDATE x402_payment_requests SET status='refund_due' WHERE id=?", (rid,))


def test_repl_invoices_refunded_records_the_refund(_invoice_db, monkeypatch):
    import os
    from cli.ui.commands.h_owner import h_invoices
    rid = _invoice_db["request_id"]
    db_path = os.environ["DB_PATH"]
    _owe(db_path, rid)
    ctx, rec = _ctx(args=["refunded", rid[:8], "0xfeed"], user_id="u1")
    h_invoices(ctx)
    assert "Refund recorded" in rec.text
    assert _status(db_path, rid) == "refunded"


def test_repl_invoices_refunded_refuses_an_id_not_owed(_invoice_db):
    import os
    from cli.ui.commands.h_owner import h_invoices
    rid = _invoice_db["request_id"]
    ctx, rec = _ctx(args=["refunded", rid[:8]], user_id="u1")
    h_invoices(ctx)
    assert "refunds you owe" in rec.text
    assert _status(os.environ["DB_PATH"], rid) == "pending"


def test_bare_repl_invoices_refunded_still_lists(_invoice_db):
    from cli.ui.commands.h_owner import h_invoices
    ctx, rec = _ctx(args=["refunded"], user_id="u1")
    h_invoices(ctx)
    assert "refunded" in rec.text and "Refund recorded" not in rec.text

