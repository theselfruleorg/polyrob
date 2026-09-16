"""Keep DeFi tests out of the developer's real data home.

`core/wallet/tokens.py` defaults its cache to `sidecar_db_path("defi_tokens.db")`,
so any test that exercises the tool without injecting `identity_fn`/`db_path`
writes into `<data_home>/defi_tokens.db` for real. That was observed: a test
fixture address (0x1111…1111) turned up in the live cache and then in a live
`portfolio` scan set.

Mirrors the existing precedent in tests/conftest.py, which forces
AUTONOMY_STATE_DURABLE=off so unit runs never touch the real autonomy DB.
"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_defi_data_home(tmp_path, monkeypatch):
    # The dir must EXIST — sqlite cannot create defi_tokens.db inside a
    # nonexistent directory ("unable to open database file"), which made every
    # identity-reading test return an error result instead of token output.
    data_home = tmp_path / "data_home"
    data_home.mkdir()
    monkeypatch.setenv("POLYROB_DATA_DIR", str(data_home))
    # A CLI test earlier in the full run calls load_env(local_mode=True), which
    # leaks the developer's REAL ~/.polyrob/.env (incl. ALCHEMY_API_KEY) into
    # os.environ — alchemy_index.available() then flips true and portfolio tests
    # make LIVE network calls for the fixture holder. Unit tests never network.
    for var in ("ALCHEMY_API_KEY", "ALCHEMY_RPC_URL_ETHEREUM",
                "ALCHEMY_RPC_URL_BASE", "ALCHEMY_RPC_URL_ROBINHOOD"):
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture(autouse=True)
def _no_indexer_network(monkeypatch):
    """Unit tests never reach a public indexer.

    `token_resolve` now asks TWO indexes, so a test that stubs only `search_fn`
    used to fall through to a live GeckoTerminal call — one such test started
    returning 15 real USDC rows for a case written as "the index found nothing".
    Blocking the network entry points makes that impossible: a test that wants
    the second index injects `search2_fn`, and one that does not gets a named
    "did not answer", never someone else's live data.
    """
    def _blocked(*a, **kw):
        raise RuntimeError("unit tests must not reach the network")

    for module in ("tools.defi.providers.geckoterminal", "tools.defi.providers.dexscreener"):
        monkeypatch.setattr(f"{module}._get", _blocked, raising=False)
    yield
