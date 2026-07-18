"""H14a (2026-07-15): `polyrob finance` must render a real balance sheet standalone.

It previously built no container and passed no db, so build_ledger's
DependencyContainer.get_instance() raised and render_finance printed the
developer-speak line "finance unavailable (Configuration required for first
initialization)" on EVERY invocation. Passing a resolved bot.db path lets the ledger
legs run (fail-open to 0 when a table is absent) — a real answer, never the container
error.
"""
import sqlite3

from cli.ui.commands.h_finance import render_finance
from modules.database.connection import DatabaseConnection
from modules.database.user_profiles import UserProfiles
from modules.database.x402_tables import X402Tables


def test_render_finance_standalone_empty_db_says_no_data(tmp_path):
    """H14b: an empty bot.db (money tables ABSENT) must render an honest 'no data
    yet' state — NOT a fabricated $0.00 balance sheet, and never the developer
    'unavailable' container error."""
    db_path = tmp_path / "bot.db"
    sqlite3.connect(str(db_path)).close()  # valid but empty — both money legs absent
    out = render_finance(user_id="rob", days=7, db_path=str(db_path))
    assert "unavailable" not in out.lower()
    assert "finance" in out
    assert "tenant rob" in out
    assert "no data yet" in out.lower()
    assert "$0.00" not in out          # no fabricated zero balance sheet


def test_render_finance_standalone_no_db_says_no_data():
    """H14a: standalone with no bot.db resolved renders honest 'no data yet',
    not the container 'Configuration required for first initialization' error."""
    out = render_finance(user_id="rob", days=7, db_path=None, standalone=True)
    assert "unavailable" not in out.lower()
    assert "no data yet" in out.lower()
    assert "tenant rob" in out


def test_render_finance_standalone_present_tables_zero_is_honest(tmp_path):
    """A REAL initialized bot.db with zero activity renders an honest $0.00 sheet
    (genuinely zero, tables present) — distinct from the 'no data yet' state."""
    db_path = tmp_path / "bot.db"

    import asyncio

    async def setup():
        db = DatabaseConnection(db_path)
        await db.connect()
        await UserProfiles(db).create_table()
        await X402Tables(db).create_tables()
        await db.close()

    asyncio.run(setup())
    out = render_finance(user_id="rob", days=7, db_path=str(db_path))
    assert "finance" in out
    assert "tenant rob" in out
    # inbound table present => not the "no data yet" empty-state; income renders
    # (terminology is income/spend — "earned" is retired).
    assert "income" in out.lower()
    assert "earned" not in out.lower()
