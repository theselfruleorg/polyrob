"""G-1 (metering finalization): seed a user_profiles row for the owner/local
principal(s) so FK-constrained metering writes (usage_records -> user_profiles)
don't raise IntegrityError on a headless/single-owner deployment where nothing
else inserts into user_profiles until an external onboarding event (wallet
signup, x402 payer, surface directory).

See modules/database/user_profiles.py::ensure_owner_profile.
"""
import pytest

from modules.database.auth_tables import AuthTables
from modules.database.connection import DatabaseConnection
from modules.database.user_profiles import UserProfiles, ensure_owner_profile
from modules.memory.models import UserProfile


async def _make_db(tmp_path, name="bot.db"):
    db = DatabaseConnection(tmp_path / name)
    await db.connect()
    await UserProfiles(db).create_table()
    # AuthTables owns usage_records (the FK-constrained metering write).
    await AuthTables(db).create_tables()
    return db


def _clear_identity_env(monkeypatch):
    for key in (
        "POLYROB_OWNER_USER_ID",
        "BOT_OWNER_USER_ID",
        "SURFACE_SUPER_ADMIN_USER_IDS",
        "POLYROB_INSTANCE_ID",
        "BOT_INSTANCE_ID",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.asyncio
async def test_seeds_resolved_principals_and_unblocks_usage_records_insert(tmp_path, monkeypatch):
    _clear_identity_env(monkeypatch)
    db = await _make_db(tmp_path)
    try:
        ok = await ensure_owner_profile(db=db)
        assert ok is True

        rows = await db.fetch_all("SELECT user_id FROM user_profiles")
        ids = {r["user_id"] for r in rows}
        # Default env: owner principal defaults to the instance id ("rob"),
        # deduped against it; plus the local-CLI fallback tenant "local".
        assert ids == {"rob", "local"}

        # The FK write that used to raise IntegrityError on every LLM call
        # (usage_records.user_id -> user_profiles.user_id) now succeeds.
        await db.execute(
            """INSERT INTO usage_records (user_id, session_id, resource_type, cost)
               VALUES (?, ?, 'llm_call', 1)""",
            ("local", "s1"),
        )
        row = await db.fetch_one(
            "SELECT user_id FROM usage_records WHERE session_id = ?", ("s1",)
        )
        assert row["user_id"] == "local"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_idempotent_second_call_leaves_one_row_and_does_not_clobber(tmp_path, monkeypatch):
    _clear_identity_env(monkeypatch)
    db = await _make_db(tmp_path)
    try:
        # Onboarding already created a richer profile for 'local' before the
        # seed helper ever runs.
        table = UserProfiles(db)
        await table.upsert_user_profile(UserProfile(
            user_id="local",
            wallet_address="0x" + "1" * 40,
            first_name="Already Onboarded",
        ))

        ok1 = await ensure_owner_profile(db=db)
        ok2 = await ensure_owner_profile(db=db)
        assert ok1 is True
        assert ok2 is True

        rows = await db.fetch_all(
            "SELECT user_id FROM user_profiles WHERE user_id = 'local'"
        )
        assert len(rows) == 1

        profile = await table.get_user_profile("local")
        assert profile.first_name == "Already Onboarded"
        assert profile.wallet_address == "0x" + "1" * 40
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_race_pre_check_stale_read_does_not_clobber_concurrent_onboarding(tmp_path, monkeypatch):
    """Regression: the SELECT pre-check is only a fast-path skip — correctness
    must come from an atomic insert-if-absent write, not from the SELECT being
    race-free. An onboarding write (or the OTHER seed seam) landing between the
    check and the write must never be clobbered by the minimal seed profile.

    Force the race by making the pre-check always report "absent" even though
    a full onboarding row already exists for that user_id; the onboarding
    row's fields must survive completely untouched.
    """
    _clear_identity_env(monkeypatch)
    db = await _make_db(tmp_path)
    try:
        table = UserProfiles(db)
        await table.upsert_user_profile(UserProfile(
            user_id="local",
            wallet_address="0x" + "2" * 40,
            email="owner@example.com",
            first_name="Real Owner",
            tier="holder",
        ))

        async def _always_absent(self, user_id):
            return None

        monkeypatch.setattr(UserProfiles, "get_user_profile", _always_absent)

        ok = await ensure_owner_profile(db=db)
        assert ok is True

        # Bypass the (now-monkeypatched) pre-check and read the real row
        # straight from the database.
        row = await db.fetch_one(
            "SELECT * FROM user_profiles WHERE user_id = 'local'"
        )
        assert row["email"] == "owner@example.com"
        assert row["first_name"] == "Real Owner"
        assert row["tier"] == "holder"
        assert row["wallet_address"] == "0x" + "2" * 40

        # Still exactly one row — the seed write DID NOT clobber it.
        rows = await db.fetch_all(
            "SELECT user_id FROM user_profiles WHERE user_id = 'local'"
        )
        assert len(rows) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_fail_open_on_broken_db_returns_false_and_does_not_raise():
    class _BrokenDB:
        async def execute(self, *a, **kw):
            raise RuntimeError("no such table: user_profiles")

        async def fetch_one(self, *a, **kw):
            raise RuntimeError("no such table: user_profiles")

        async def fetch_all(self, *a, **kw):
            raise RuntimeError("no such table: user_profiles")

    ok = await ensure_owner_profile(db=_BrokenDB())
    assert ok is False


@pytest.mark.asyncio
async def test_no_db_available_returns_false_without_raising(monkeypatch):
    # No db passed, and no DependencyContainer singleton exists in-process:
    # must degrade to False, never raise.
    import core.container as container_mod
    monkeypatch.setattr(container_mod.DependencyContainer, "_instance", None)

    ok = await ensure_owner_profile(db=None)
    assert ok is False
