"""098 W1 MO-19: the Polymarket credential row is written by partial writers.
A toggle or a limits update never resets the network or the allowance check;
a new wallet clears the old wallet's L2 creds; trading needs a key."""
import pytest
import pytest_asyncio

pytest.importorskip("polyrob_markets")

from polyrob_markets.polymarket.store import PolymarketDBHandler  # noqa: E402
from polyrob_markets.polymarket.store_models import ApiCredentials, TradingLimits  # noqa: E402

from modules.database.connection import DatabaseConnection  # noqa: E402

AMOY = 80002
W1 = "0x" + "1" * 40
W2 = "0x" + "2" * 40


class _Enc:
    def encrypt(self, v):
        return "enc:" + v

    def decrypt(self, v):
        return v[4:]


@pytest_asyncio.fixture
async def store(tmp_path):
    db = DatabaseConnection(tmp_path / "bot.db")
    await db.connect()
    h = PolymarketDBHandler(db, encryption=_Enc())
    await h.ensure_tables()
    await h.save_credentials(user_id="u", wallet_address=W1, private_key="k1", demo_mode=False,
                             chain_id=AMOY, allowances_verified=True,
                             api_credentials=ApiCredentials(api_key="ak", api_secret="s",
                                                            api_passphrase="p"))
    yield h
    await db.close()


@pytest.mark.asyncio
async def test_disable_enable_keeps_chain_id(store):
    assert await store.set_enabled("u", False)
    assert await store.set_enabled("u", True)
    c = await store.get_credentials("u")
    assert c.chain_id == AMOY and c.enabled


@pytest.mark.asyncio
async def test_toggle_keeps_allowances_verified(store):
    await store.set_limits("u", TradingLimits())
    await store.save_credentials(user_id="u", demo_mode=False, enabled=True)  # an update
    c = await store.get_credentials("u")
    assert c.allowances_verified and c.chain_id == AMOY


@pytest.mark.asyncio
async def test_new_wallet_clears_l2_creds(store):
    c = await store.get_credentials("u")
    assert c.has_api_credentials()
    await store.save_credentials(user_id="u", wallet_address=W2, private_key="k2",
                                 demo_mode=False)
    c = await store.get_credentials("u")
    assert not c.has_api_credentials() and not c.allowances_verified
    assert c.wallet_address == W2


@pytest.mark.asyncio
async def test_demo_false_without_key_rejected(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from polyrob_markets.polymarket import routes
    db = DatabaseConnection(tmp_path / "demo.db")
    await db.connect()
    h = PolymarketDBHandler(db, encryption=_Enc())
    await h.ensure_tables()
    await h.save_credentials(user_id="u", demo_mode=True)   # a demo row: no key

    async def _db():
        return h
    monkeypatch.setattr(routes, "get_polymarket_db", _db)
    from polyrob_markets.polymarket.api_models import ConfigurePolymarketRequest
    data = ConfigurePolymarketRequest(demo_mode=False)
    with pytest.raises(HTTPException) as e:
        await routes.configure_polymarket(None, data, user_id="u")
    assert e.value.status_code == 400
    await db.close()


@pytest.mark.asyncio
async def test_every_credential_write_drops_the_service_cache(store, monkeypatch):
    """098 close-out MO-19: the service's cached credentials and CLOB client
    for a user are dropped on every credential write (toggle, limits, wallet,
    L2 creds, delete), not only when a later read notices a difference."""
    import types
    from polyrob_markets.polymarket.service import PolymarketTool
    from tools.base_tool import BaseTool

    async def _base_init(self):
        return None
    monkeypatch.setattr(BaseTool, "_initialize", _base_init)
    tool = PolymarketTool(config=types.SimpleNamespace(), container=None, db_handler=store)
    await tool._initialize()   # registers the change listener on its store
    writes = [lambda: store.set_enabled("u", False), lambda: store.set_limits("u", TradingLimits()),
              lambda: store.save_credentials(user_id="u", wallet_address=W2, demo_mode=False),
              lambda: store.save_api_credentials("u", ApiCredentials("k", "s", "p")),
              lambda: store.delete_credentials("u")]
    for write in writes:
        tool._credentials_cache["u"] = object()
        tool._clob_clients["u"] = object()
        tool._clob_clients["other"] = "kept"
        await write()
        assert "u" not in tool._credentials_cache and "u" not in tool._clob_clients
        assert tool._clob_clients["other"] == "kept"
    await tool._cleanup()
