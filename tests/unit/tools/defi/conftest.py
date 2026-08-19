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
