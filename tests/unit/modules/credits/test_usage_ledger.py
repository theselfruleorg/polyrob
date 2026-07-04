"""F14 (P2): x402/admin usage isn't billed, but must still be LEDGERED.

Without a usage ledger there's no way to reconcile or back-bill late/failed x402
settlements, and admin usage is invisible. Record usage for non-charged tiers.
"""
import logging
import types
import pytest

from modules.database.connection import DatabaseConnection
from modules.credits.usage_tracker import LLMUsageTracker


def _record():
    return types.SimpleNamespace(
        user_id="usr_1", session_id="s1", request_id="r1", model="gpt-4o",
        tokens=types.SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        costs=types.SimpleNamespace(api_cost_usd=0.02, credits_charged=3),
    )


@pytest.mark.asyncio
async def test_usage_ledger_records_noncharged_usage(tmp_path):
    db = DatabaseConnection(tmp_path / "u.db")
    await db.connect()
    tracker = LLMUsageTracker.__new__(LLMUsageTracker)
    tracker.db = db
    tracker.logger = logging.getLogger("test")
    try:
        await tracker._record_usage_ledger(_record())
        row = await db.fetch_one("SELECT * FROM usage_ledger WHERE user_id='usr_1'")
        assert row is not None
        assert row["model"] == "gpt-4o"
        assert row["api_cost_usd"] == 0.02
        assert row["credits_charged"] == 3
        assert row["prompt_tokens"] == 10
        assert row["completion_tokens"] == 5
    finally:
        await db.close()
