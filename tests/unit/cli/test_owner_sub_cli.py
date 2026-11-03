"""`polyrob owner sub list` / `polyrob owner sub cancel` (Task 14, R5) —
tenant-scoped CLI over the watchtower-subscriptions store."""
import asyncio

import pytest
from click.testing import CliRunner

from cli.commands.owner import owner
from modules.database.connection import DatabaseConnection
from modules.database.user_profiles import UserProfiles
from modules.database.x402_tables import X402Tables
from modules.x402 import subscriptions as subs


async def _seed_db(db_path):
    db = DatabaseConnection(db_path)
    await db.connect()
    await UserProfiles(db).create_table()
    await X402Tables(db).create_tables()
    return db


def _make_db_with_sub(tmp_path, *, user_id="alice"):
    db_path = tmp_path / "bot.db"

    async def setup():
        db = await _seed_db(db_path)
        try:
            return (await subs.create_subscription(
                user_id=user_id, correspondent_surface="email",
                correspondent_address="payer@example.com", cron_job_id="job1",
                amount_usd=10.0, db=db))["id"]
        finally:
            await db.close()

    sub_id = asyncio.run(setup())
    return db_path, sub_id


def test_sub_list_shows_tenant_subscriptions(tmp_path, monkeypatch):
    db_path, sub_id = _make_db_with_sub(tmp_path, user_id="alice")
    monkeypatch.setenv("DB_PATH", str(db_path))

    res = CliRunner().invoke(owner, ["sub", "list", "--user", "alice"])
    assert res.exit_code == 0
    assert sub_id in res.output
    assert "active" in res.output
    assert "job1" in res.output
    assert "10.00" in res.output


def test_sub_list_empty(tmp_path, monkeypatch):
    db_path = tmp_path / "bot.db"

    async def setup():
        db = await _seed_db(db_path)
        await db.close()

    asyncio.run(setup())
    monkeypatch.setenv("DB_PATH", str(db_path))

    res = CliRunner().invoke(owner, ["sub", "list", "--user", "alice"])
    assert res.exit_code == 0
    assert "no subscriptions" in res.output


def test_sub_list_is_tenant_scoped(tmp_path, monkeypatch):
    db_path = tmp_path / "bot.db"

    async def setup():
        db = await _seed_db(db_path)
        try:
            await subs.create_subscription(
                user_id="tenant_a", correspondent_surface="email",
                correspondent_address="a@x.com", cron_job_id="job_a", db=db)
            await subs.create_subscription(
                user_id="tenant_b", correspondent_surface="email",
                correspondent_address="b@x.com", cron_job_id="job_b", db=db)
        finally:
            await db.close()

    asyncio.run(setup())
    monkeypatch.setenv("DB_PATH", str(db_path))

    res = CliRunner().invoke(owner, ["sub", "list", "--user", "tenant_a"])
    assert res.exit_code == 0
    assert "job_a" in res.output
    assert "job_b" not in res.output


def test_sub_cancel_flips_status(tmp_path, monkeypatch):
    db_path, sub_id = _make_db_with_sub(tmp_path, user_id="alice")
    monkeypatch.setenv("DB_PATH", str(db_path))

    res = CliRunner().invoke(owner, ["sub", "cancel", sub_id, "--user", "alice"])
    assert res.exit_code == 0
    assert "canceled" in res.output.lower()

    res_list = CliRunner().invoke(owner, ["sub", "list", "--user", "alice"])
    assert "canceled" in res_list.output


def test_sub_cancel_wrong_tenant_refused(tmp_path, monkeypatch):
    db_path, sub_id = _make_db_with_sub(tmp_path, user_id="tenant_a")
    monkeypatch.setenv("DB_PATH", str(db_path))

    res = CliRunner().invoke(owner, ["sub", "cancel", sub_id, "--user", "tenant_b"])
    assert res.exit_code == 0
    assert "no active subscription" in res.output.lower()

    async def _verify():
        db = DatabaseConnection(db_path)
        await db.connect()
        try:
            row = await subs.get_subscription(sub_id, db=db)
            assert row["status"] == subs.STATUS_ACTIVE  # untouched
        finally:
            await db.close()

    asyncio.run(_verify())


def test_sub_cancel_unknown_id(tmp_path, monkeypatch):
    db_path, _sub_id = _make_db_with_sub(tmp_path, user_id="alice")
    monkeypatch.setenv("DB_PATH", str(db_path))

    res = CliRunner().invoke(owner, ["sub", "cancel", "sub_doesnotexist", "--user", "alice"])
    assert res.exit_code == 0
    assert "no active subscription" in res.output.lower()


# --- 098 close-out MO-05: `polyrob owner sub reactivate` ---------------------

def _set_status(db_path, sub_id, status, paid_through):
    async def run():
        db = DatabaseConnection(db_path)
        await db.connect()
        try:
            await db.execute("UPDATE subscriptions SET status=?, paid_through=? WHERE id=?",
                             (status, paid_through, sub_id))
        finally:
            await db.close()
    asyncio.run(run())


def _get(db_path, sub_id):
    async def run():
        db = DatabaseConnection(db_path)
        await db.connect()
        try:
            return await subs.get_subscription(sub_id, db=db)
        finally:
            await db.close()
    return asyncio.run(run())


def test_sub_reactivate_brings_a_suspended_subscription_back_for_a_period(tmp_path, monkeypatch):
    import time
    db_path, sub_id = _make_db_with_sub(tmp_path, user_id="alice")
    monkeypatch.setenv("DB_PATH", str(db_path))
    _set_status(db_path, sub_id, "suspended", int(time.time()) - 60 * 86400)
    before = int(time.time())

    res = CliRunner().invoke(owner, ["sub", "reactivate", sub_id, "--user", "alice"])
    assert res.exit_code == 0, res.output
    assert "reactivated" in res.output
    row = _get(db_path, sub_id)
    assert row["status"] == "active"
    assert row["paid_through"] >= before + int(row["period_days"]) * 86400 - 1


def test_sub_reactivate_refuses_canceled_active_and_other_tenants(tmp_path, monkeypatch):
    import time
    db_path, sub_id = _make_db_with_sub(tmp_path, user_id="alice")
    monkeypatch.setenv("DB_PATH", str(db_path))
    res = CliRunner().invoke(owner, ["sub", "reactivate", sub_id, "--user", "alice"])
    assert res.exit_code == 1 and "already active" in res.output
    _set_status(db_path, sub_id, "grace", int(time.time()) - 86400)
    res = CliRunner().invoke(owner, ["sub", "reactivate", sub_id, "--user", "mallory"])
    assert res.exit_code == 1 and "unknown" in res.output
    assert _get(db_path, sub_id)["status"] == "grace"
    _set_status(db_path, sub_id, "canceled", int(time.time()) - 86400)
    res = CliRunner().invoke(owner, ["sub", "reactivate", sub_id, "--user", "alice"])
    assert res.exit_code == 1 and "terminal" in res.output
    assert _get(db_path, sub_id)["status"] == "canceled"
