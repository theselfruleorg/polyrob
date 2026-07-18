"""§6.2 metering truth — a METERING-ONLY usage tracker for headless deploys.

The live gap: goal/cron containers had no usage tracker at all (992×
"usage_tracker not available — NO BILLING!" in 72h while OpenRouter burned
$68.89), because the orchestrator required BOTH database_manager AND
balance_manager. A single-owner headless box has no credit system — but
metering truth (real api_cost_usd into usage_records, which the $10/day
budget gate reads) must not depend on one. With balance_manager=None the
tracker RECORDS and never DEDUCTS.
"""
import asyncio

import pytest


class _FakeDB:
    def __init__(self):
        self.executed = []

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))

    async def fetch_one(self, sql, params=None):
        return None


class _Telemetry:
    def capture_event(self, *a, **k):
        pass

    def capture(self, *a, **k):
        pass


def _tracker(db, balance):
    from modules.credits.usage_tracker import LLMUsageTracker
    return LLMUsageTracker(db=db, balance_manager=balance,
                           telemetry_manager=_Telemetry())


def test_metering_only_records_usage_without_deduction():
    db = _FakeDB()
    tracker = _tracker(db, None)
    rec = asyncio.run(tracker.record_llm_usage(
        user_id="rob", session_id="s1", agent_id="a1",
        model="deepseek-v3.2", provider="openrouter",
        input_tokens=1000, output_tokens=200,
    ))
    assert rec is not None
    # G-26: the write is now "INSERT OR IGNORE INTO usage_records" (request_id
    # dedup) -- match on "INTO usage_records" so this doesn't couple to the
    # exact INSERT modifier.
    inserts = [sql for sql, _ in db.executed if "INTO usage_records" in sql]
    assert inserts, "usage must be recorded even without a balance_manager"


def test_metering_only_never_raises_insufficient_credits(monkeypatch):
    monkeypatch.setenv("FAIL_ON_INSUFFICIENT_CREDITS", "true")
    db = _FakeDB()
    tracker = _tracker(db, None)
    # would raise via _deduct_from_balance if the deduction leg ran
    rec = asyncio.run(tracker.record_llm_usage(
        user_id="rob", session_id="s1", agent_id="a1",
        model="gpt-5", provider="openai",
        input_tokens=50000, output_tokens=10000,
    ))
    assert rec is not None


def test_build_from_services_metering_only_when_no_balance():
    from modules.credits.usage_tracker import build_usage_tracker
    db = _FakeDB()
    t = build_usage_tracker(db=db, balance_manager=None, telemetry_manager=_Telemetry())
    assert t is not None
    assert t.balance is None


def test_build_from_services_none_without_db():
    from modules.credits.usage_tracker import build_usage_tracker
    assert build_usage_tracker(db=None, balance_manager=object(),
                               telemetry_manager=_Telemetry()) is None


class _FKHealDB(_FakeDB):
    """usage_records insert FK-fails until a user_profiles row exists —
    the live headless shape from proposal 009."""

    def __init__(self, profile_insert_fails=False):
        super().__init__()
        self.profile_created = False
        self.profile_insert_fails = profile_insert_fails

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if "INTO user_profiles" in sql:
            if self.profile_insert_fails:
                raise Exception("disk I/O error")
            self.profile_created = True
            return None
        if "INTO usage_records" in sql and not self.profile_created:
            raise Exception("DatabaseError: FOREIGN KEY constraint failed")
        return None


def test_metering_fk_selfheal_creates_profile_and_retries():
    """Proposal 009 option 3: metering-only + missing profile → self-heal + retry."""
    db = _FKHealDB()
    tracker = _tracker(db, None)
    rec = asyncio.run(tracker.record_llm_usage(
        user_id="rob", session_id="s1", agent_id="a1",
        model="deepseek-v3.2", provider="openrouter",
        input_tokens=1000, output_tokens=200,
    ))
    assert rec is not None
    profile_inserts = [p for sql, p in db.executed if "INTO user_profiles" in sql]
    assert profile_inserts == [("rob", "local:rob")]
    usage_attempts = [sql for sql, _ in db.executed if "INTO usage_records" in sql]
    assert len(usage_attempts) == 2, "one failed attempt + one successful retry"


def test_metering_fk_selfheal_gives_up_when_profile_insert_fails():
    db = _FKHealDB(profile_insert_fails=True)
    tracker = _tracker(db, None)
    with pytest.raises(Exception, match="(?i)foreign key"):
        asyncio.run(tracker.record_llm_usage(
            user_id="rob", session_id="s1", agent_id="a1",
            model="deepseek-v3.2", provider="openrouter",
            input_tokens=10, output_tokens=5,
        ))
    usage_attempts = [sql for sql, _ in db.executed if "INTO usage_records" in sql]
    assert len(usage_attempts) == 1, "no retry when the heal itself failed"


def test_fk_selfheal_never_runs_in_server_mode():
    """With a balance manager present (multi-tenant server) a missing profile is
    a genuine error: no synthetic profile row, the FK failure propagates."""
    good_db = _FakeDB()
    rec = asyncio.run(_tracker(good_db, None).record_llm_usage(
        user_id="alice", session_id="s2", agent_id="a1",
        model="deepseek-v3.2", provider="openrouter",
        input_tokens=10, output_tokens=5,
    ))
    server_tracker = _tracker(_FKHealDB(), balance=object())
    with pytest.raises(Exception, match="(?i)foreign key"):
        asyncio.run(server_tracker._write_to_database(rec))
    assert not any("INTO user_profiles" in sql
                   for sql, _ in server_tracker.db.executed)


def test_non_fk_db_error_propagates_without_heal():
    class _BrokenDB(_FakeDB):
        async def execute(self, sql, params=None):
            self.executed.append((sql, params))
            if "INTO usage_records" in sql:
                raise Exception("database is locked")

    db = _BrokenDB()
    tracker = _tracker(db, None)
    with pytest.raises(Exception, match="locked"):
        asyncio.run(tracker.record_llm_usage(
            user_id="rob", session_id="s1", agent_id="a1",
            model="deepseek-v3.2", provider="openrouter",
            input_tokens=10, output_tokens=5,
        ))
    assert not any("INTO user_profiles" in sql for sql, _ in db.executed)


def test_orchestrator_wiring_uses_build_usage_tracker_source():
    """Source-inspection (pattern: test_cli_container_data_dir): the orchestrator
    must build its tracker through build_usage_tracker so db-only containers get
    the metering-only tracker instead of None."""
    import inspect
    import agents.task.agent.orchestrator as orch_mod
    src = inspect.getsource(orch_mod)
    assert "build_usage_tracker" in src


def test_cli_container_registers_database_manager_source():
    """§6.1: build_cli_container must register database_manager so headless
    goal/cron runs have the x402 payment-request store (the live
    'payment-request store unavailable' blocker) and metering truth."""
    import inspect
    import core.bootstrap as bootstrap
    src = inspect.getsource(bootstrap.build_cli_container)
    assert "database_manager" in src
