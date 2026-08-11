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
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "data_home"))
    yield
