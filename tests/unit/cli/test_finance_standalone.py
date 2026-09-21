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


def test_render_finance_standalone_empty_db_says_unknown_not_zero(tmp_path):
    """C48 (2026-09-21): an empty bot.db (money tables ABSENT) renders ONE fact —
    the stores could not be read, so every figure is UNKNOWN. It used to print a
    disjunction ("no data yet … or metering is off / not yet initialized"), which
    is three guesses printed as one answer."""
    db_path = tmp_path / "bot.db"
    sqlite3.connect(str(db_path)).close()  # valid but empty — both money legs absent
    out = render_finance(user_id="rob", days=7, db_path=str(db_path))
    assert "finance" in out
    assert "tenant rob" in out
    assert "unavailable" in out.lower()
    assert "unknown, not zero" in out.lower()
    assert " or " not in out.lower()   # no disjunction
    assert "$0.00" not in out          # no fabricated zero balance sheet


def test_render_finance_standalone_no_db_names_a_verb_not_a_flag(tmp_path):
    """C48: standalone with no bot.db resolved says the store does not exist and
    names the VERB that creates it — never an env flag (`X402_INVOICE_ENABLED`
    was in this line) and never a disjunction."""
    out = render_finance(user_id="rob", days=7, db_path=None, standalone=True)
    assert "unavailable" in out.lower()
    assert "unknown, not zero" in out.lower()
    assert "polyrob run" in out
    assert "X402_INVOICE_ENABLED" not in out
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
