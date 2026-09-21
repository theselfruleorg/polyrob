"""Shared env isolation and DB rig for the x402 unit tests."""
import pytest
import pytest_asyncio


@pytest.fixture(autouse=True)
def _no_leaked_alchemy_key(monkeypatch):
    """Assert RPC defaults against a CLEAN env.

    A CLI test earlier in a full run calls ``load_env(local_mode=True)``, which
    leaks the developer's real ``~/.polyrob/.env`` — including
    ``ALCHEMY_API_KEY`` — into ``os.environ``. Since 2026-08-24
    ``core.wallet.onchain.rpc_url_for_chain`` composes an Alchemy endpoint from
    that key when no ``DEFI_EVM_RPC_<CHAIN>`` pin is set, so a leaked key
    silently changes what "the default endpoint" resolves to and these tests
    fail only in the full suite. Same precedent as
    tests/unit/tools/defi/conftest.py. A test that WANTS the key sets it
    explicitly with monkeypatch.
    """
    monkeypatch.delenv("ALCHEMY_API_KEY", raising=False)


@pytest_asyncio.fixture()
async def x402_db(tmp_path):
    """A REAL x402 schema on a temp DB (046).

    The all-fakes pattern is why N1 shipped, so every 046 test runs against the
    same `X402Tables.create_tables()` production takes — which is also what adds
    the asset columns, so a column missing in prod is a column missing here.
    """
    from modules.database.connection import DatabaseConnection
    from modules.database.user_profiles import UserProfiles
    from modules.database.x402_tables import X402Tables

    db = DatabaseConnection(tmp_path / "x402.db")
    await db.connect()
    await UserProfiles(db).create_table()
    await X402Tables(db).create_tables()
    try:
        yield db
    finally:
        await db.close()


@pytest.fixture(autouse=True)
def _reset_treasury_cache():
    """B41 caches the advertised treasury address per PROCESS.

    These tests monkeypatch the wallet/env the resolver reads, so the cache
    must not carry one test's answer into the next.
    """
    from api.x402_advertisement import reset_treasury_cache
    reset_treasury_cache()
    yield
    reset_treasury_cache()
