"""`_with_bot_db` (every `polyrob owner` money verb): a failed open or read of
bot.db is a not-ok message, never a Python traceback; a usage refusal from the
verb still reaches click, and Ctrl-C is never swallowed."""
import asyncio
import sqlite3

import click
import pytest

from cli.commands import owner as O


@pytest.fixture
def bot_db(tmp_path, monkeypatch):
    path = tmp_path / "bot.db"
    sqlite3.connect(path).close()
    monkeypatch.setattr(O, "_bot_db_path", lambda: str(path))
    return path


def test_a_failed_read_is_a_message(bot_db):
    async def run(db):
        return await db.fetch_all("SELECT * FROM x402_payment_requests")

    ok, msg = asyncio.run(O._with_bot_db(run))
    assert ok is False
    assert msg.startswith("could not read bot.db: ")
    assert "no such table" in msg


def test_a_good_read_returns_the_result(bot_db):
    async def run(db):
        return await db.fetch_all("SELECT 1 AS one")

    assert asyncio.run(O._with_bot_db(run)) == (True, [{"one": 1}])


@pytest.mark.parametrize("exc", [click.UsageError("say --sent"), KeyboardInterrupt()])
def test_a_usage_refusal_and_ctrl_c_pass_through(bot_db, exc):
    async def run(db):
        raise exc

    with pytest.raises(type(exc)):
        asyncio.run(O._with_bot_db(run))
