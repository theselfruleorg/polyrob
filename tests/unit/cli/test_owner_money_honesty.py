"""Owner-CLI money honesty (Task 8): M16 tenant-scope printing + shared money
resolver, L10 --status-before-LIMIT + settle wake condition.

These lock the 2026-07-15 wallet/crypto UX-honesty fixes: sibling money views
must print WHICH tenant they scope to, `owner invoices --status` must filter in
SQL before LIMIT (so an old pending row past 50 newer rows isn't dropped), and
`owner settle` must state that the session wake is conditional.
"""
import asyncio

import pytest
from click.testing import CliRunner

from cli.commands.owner import owner
from modules.database.connection import DatabaseConnection
from modules.database.user_profiles import UserProfiles
from modules.database.x402_tables import X402Tables


async def _seed_db(db_path):
    db = DatabaseConnection(db_path)
    await db.connect()
    await UserProfiles(db).create_table()
    await X402Tables(db).create_tables()
    # x402_payment_requests.user_id has an FK to user_profiles — seed the tenant
    # row so direct INSERTs (needed to control created_at for the LIMIT test)
    # don't trip the constraint.
    await db.execute(
        "INSERT OR IGNORE INTO user_profiles (user_id, wallet_address) VALUES (?, ?)",
        ("rob", "0xWALLET_rob"))
    return db


def _insert_invoice(db, *, rid, status, created_at, amount_usd=5.0, user_id="rob"):
    meta = '{"kind": "agent_invoice", "tenant_id": "%s", "purpose": "widget"}' % user_id
    return db.execute(
        """INSERT INTO x402_payment_requests
             (id, user_id, amount, amount_usd, asset, chain, recipient, nonce,
              deadline, status, metadata, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (rid, user_id, str(int(amount_usd * 1e6)), amount_usd, "USDC", "base",
         "0xTREASURY", f"nonce-{rid}", 9999999999, status, meta, created_at))


def test_invoices_prints_scope_all_tenants(tmp_path, monkeypatch):
    """M16: no --user prints the ALL-tenants scope so the owner isn't confused
    about which bucket the listing covers."""
    db_path = tmp_path / "bot.db"

    async def setup():
        db = await _seed_db(db_path)
        try:
            await _insert_invoice(db, rid="r1", status="pending",
                                  created_at="2026-07-15 10:00:00")
        finally:
            await db.close()

    asyncio.run(setup())
    monkeypatch.setenv("DB_PATH", str(db_path))
    res = CliRunner().invoke(owner, ["invoices"])
    assert res.exit_code == 0, res.output
    assert "scope:" in res.output.lower()
    assert "all tenants" in res.output.lower()


def test_invoices_prints_scope_tenant(tmp_path, monkeypatch):
    db_path = tmp_path / "bot.db"

    async def setup():
        db = await _seed_db(db_path)
        try:
            await _insert_invoice(db, rid="r1", status="pending",
                                  created_at="2026-07-15 10:00:00")
        finally:
            await db.close()

    asyncio.run(setup())
    monkeypatch.setenv("DB_PATH", str(db_path))
    res = CliRunner().invoke(owner, ["invoices", "--user", "rob"])
    assert res.exit_code == 0, res.output
    assert "scope:" in res.output.lower()
    assert "tenant rob" in res.output


def test_invoices_status_filter_before_limit(tmp_path, monkeypatch):
    """L10: --status must filter IN SQL before LIMIT 50 — a single old pending
    invoice behind 51 newer completed rows must still surface with
    `--status pending` (the old Python-after-LIMIT filter dropped it)."""
    db_path = tmp_path / "bot.db"

    async def setup():
        db = await _seed_db(db_path)
        try:
            # 51 newer completed rows (2026-07-15 ...), 1 OLDEST pending row (2020).
            for i in range(51):
                await _insert_invoice(db, rid=f"c{i:02d}", status="completed",
                                      created_at=f"2026-07-15 12:{i//60:02d}:{i%60:02d}")
            await _insert_invoice(db, rid="old_pending", status="pending",
                                  created_at="2020-01-01 00:00:00")
        finally:
            await db.close()

    asyncio.run(setup())
    monkeypatch.setenv("DB_PATH", str(db_path))
    res = CliRunner().invoke(owner, ["invoices", "--status", "pending"])
    assert res.exit_code == 0, res.output
    assert "old_pending" in res.output           # filtered in SQL, survives LIMIT
    assert "c00" not in res.output               # completed rows excluded


def test_settle_states_wake_condition_when_off(tmp_path, monkeypatch):
    """L10: `owner settle` states the wake is conditional — when
    X402_INVOICE_ENABLED is off, it warns no session wake fires."""
    db_path = tmp_path / "bot.db"

    async def setup():
        db = await _seed_db(db_path)
        try:
            await _insert_invoice(db, rid="pay1", status="pending",
                                  created_at="2026-07-15 10:00:00")
        finally:
            await db.close()

    asyncio.run(setup())
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.delenv("X402_INVOICE_ENABLED", raising=False)
    res = CliRunner().invoke(owner, ["settle", "pay1"])
    assert res.exit_code == 0, res.output
    assert "settled pay1" in res.output
    assert "X402_INVOICE_ENABLED" in res.output
    assert "no session wake" in res.output.lower()
