"""`polyrob owner invoices` — CLI listing of agent-created x402 invoices,
including the free-form payer_contact ("billed to") line (Task 8).

Covers both code paths in cli/commands/owner.py::invoices: the `--user`
path (delegates to modules.x402.invoicing.list_payment_requests) and the
no-`--user` fallback (a manual query over x402_payment_requests) — the two
independently need to surface payer_contact, and the fallback path has its
own metadata-parsing landmine (DatabaseConnection auto-parses JSON TEXT
columns to dicts, so a naive `json.loads(already_a_dict)` silently drops
every field via the broad except)."""
import asyncio
import json

import pytest
from click.testing import CliRunner

from cli.commands.owner import owner
from modules.database.connection import DatabaseConnection
from modules.database.user_profiles import UserProfiles
from modules.database.x402_tables import X402Tables
from modules.x402 import invoicing


@pytest.fixture(autouse=True)
def _treasury_env(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0xTREASURY")
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    for var in ("X402_INVOICE_MAX_USD", "X402_INVOICE_DAILY_MAX"):
        monkeypatch.delenv(var, raising=False)


async def _seed_db(db_path):
    db = DatabaseConnection(db_path)
    await db.connect()
    await UserProfiles(db).create_table()
    await X402Tables(db).create_tables()
    return db


def _make_db(tmp_path, *, payer_contact=None, purpose="widget"):
    db_path = tmp_path / "bot.db"

    async def setup():
        db = await _seed_db(db_path)
        try:
            await invoicing.create_payment_request(
                user_id="rob", session_id="s1", amount_usd=5.0, purpose=purpose,
                payer_contact=payer_contact, db=db)
        finally:
            await db.close()

    asyncio.run(setup())
    return db_path


def test_invoices_user_scoped_shows_billed_to(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, payer_contact="Alice <a@x.com>")
    monkeypatch.setenv("DB_PATH", str(db_path))

    res = CliRunner().invoke(owner, ["invoices", "--user", "rob"])
    assert res.exit_code == 0
    assert "Alice <a@x.com>" in res.output
    assert "billed to" in res.output.lower()


def test_invoices_user_scoped_omits_billed_to_when_absent(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, payer_contact=None)
    monkeypatch.setenv("DB_PATH", str(db_path))

    res = CliRunner().invoke(owner, ["invoices", "--user", "rob"])
    assert res.exit_code == 0
    assert "widget" in res.output  # sanity: the row is actually listed
    assert "billed to" not in res.output.lower()


def test_invoices_all_users_path_shows_billed_to(tmp_path, monkeypatch):
    """No --user: owner.py's fallback path queries x402_payment_requests
    directly (not via list_payment_requests) — must also surface
    payer_contact, tolerating the metadata auto-parse-to-dict landmine."""
    db_path = _make_db(tmp_path, payer_contact="Bob <b@x.com>")
    monkeypatch.setenv("DB_PATH", str(db_path))

    res = CliRunner().invoke(owner, ["invoices"])
    assert res.exit_code == 0
    assert "Bob <b@x.com>" in res.output
    assert "billed to" in res.output.lower()
    # the pre-existing bug this touches: purpose must survive too
    assert "widget" in res.output


def test_invoices_legacy_payer_hint_metadata_shows_in_cli(tmp_path, monkeypatch):
    """A pre-migration row (metadata.payer_hint, no payer_contact key) must
    still display via the CLI (back-compat read), on both code paths."""
    db_path = tmp_path / "bot.db"

    async def setup():
        db = await _seed_db(db_path)
        try:
            inv = await invoicing.create_payment_request(
                user_id="rob", session_id="s1", amount_usd=5.0, purpose="widget",
                db=db)
            legacy_meta = json.dumps({
                "kind": "agent_invoice", "session_id": "s1", "tenant_id": "rob",
                "purpose": "widget", "payer_hint": "Legacy Payer",
                "wake_delivered": False, "correspondent_ref": None,
            })
            await db.execute(
                "UPDATE x402_payment_requests SET metadata = ? WHERE id = ?",
                (legacy_meta, inv["request_id"]))
        finally:
            await db.close()

    asyncio.run(setup())
    monkeypatch.setenv("DB_PATH", str(db_path))

    res_user = CliRunner().invoke(owner, ["invoices", "--user", "rob"])
    assert res_user.exit_code == 0
    assert "Legacy Payer" in res_user.output

    res_all = CliRunner().invoke(owner, ["invoices"])
    assert res_all.exit_code == 0
    assert "Legacy Payer" in res_all.output


def test_invoices_empty_still_works(tmp_path, monkeypatch):
    db_path = tmp_path / "bot.db"

    async def setup():
        db = await _seed_db(db_path)
        await db.close()

    asyncio.run(setup())
    monkeypatch.setenv("DB_PATH", str(db_path))

    res = CliRunner().invoke(owner, ["invoices"])
    assert res.exit_code == 0
    assert "no invoices" in res.output


# --- Task 9b: the no-`--user` path must survive metadata compaction --------
# `owner.py`'s fallback query used to match `metadata LIKE '%"kind": "agent_invoice"%'`
# — a spaced literal against `json.dumps`'s default spacing. ANY later
# `json_set` on the row (e.g. the boot-time subscription dedup in
# `modules.database.x402_tables.dedupe_and_create_subscription_pending_unique_index`)
# re-serializes the WHOLE metadata blob COMPACTLY, which the spaced LIKE then
# silently stopped matching — the owner's OWN invoice listing dropped the row.
# `--user rob` (which delegates to `list_payment_requests`, already fixed in
# Task 9 to use `json_extract`) is unaffected; this proves the OTHER path.

def test_invoices_all_users_path_survives_metadata_compaction(tmp_path, monkeypatch):
    db_path = tmp_path / "bot.db"

    async def setup():
        db = await _seed_db(db_path)
        try:
            inv = await invoicing.create_payment_request(
                user_id="rob", session_id="s1", amount_usd=5.0,
                purpose="compacted widget", db=db)
            # Recompact the metadata blob exactly the way the boot-time
            # subscription dedup does (modules/database/x402_tables.py).
            await db.execute(
                "UPDATE x402_payment_requests "
                "SET metadata = json_set(metadata, '$.subscription_id', NULL) "
                "WHERE id = ?",
                (inv["request_id"],))
            # Prove the recompaction actually happened: the OLD spaced-LIKE
            # predicate this test guards against no longer matches this row
            # at all — without this assertion the test would not demonstrate
            # the bug's precondition, only the fix's postcondition.
            still_spaced = await db.fetch_all(
                "SELECT id FROM x402_payment_requests "
                "WHERE id = ? AND metadata LIKE '%\"kind\": \"agent_invoice\"%'",
                (inv["request_id"],))
            assert still_spaced == [], (
                "setup did not actually recompact the metadata blob — this "
                "test would not be exercising the Task 9b bug"
            )
        finally:
            await db.close()

    asyncio.run(setup())
    monkeypatch.setenv("DB_PATH", str(db_path))

    res = CliRunner().invoke(owner, ["invoices"])
    assert res.exit_code == 0
    assert "compacted widget" in res.output
    assert "no invoices" not in res.output
