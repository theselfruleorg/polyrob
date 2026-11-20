"""connect() deletes no row (proposal 125 S16, B-H7; owner decision D14 = delete).

The old ``_cleanup_orphaned_records`` deleted every ``credit_transactions`` /
``api_keys`` / … row whose user had no profile row, on EVERY connect, with the
foreign keys switched off. A money ledger row is never deleted by a connect.
"""
import asyncio
import sqlite3

from modules.database.connection import DatabaseConnection


def test_an_orphan_ledger_row_survives_connect(tmp_path):
    db = tmp_path / "bot.db"
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE user_profiles (user_id TEXT PRIMARY KEY)")
    raw.execute("CREATE TABLE credit_transactions (id INTEGER PRIMARY KEY, user_id TEXT, amount REAL)")
    raw.execute("INSERT INTO credit_transactions (user_id, amount) VALUES ('no-profile', 5.0)")
    raw.commit()
    raw.close()

    async def go():
        conn = DatabaseConnection(db)
        await conn.connect()
        await conn.close()
    asyncio.run(go())

    n = sqlite3.connect(db).execute("SELECT COUNT(*) FROM credit_transactions").fetchone()[0]
    assert n == 1
    assert not hasattr(DatabaseConnection, "_cleanup_orphaned_records")
