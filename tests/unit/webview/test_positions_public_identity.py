"""``/api/webgate/positions`` knows WHOSE book it is showing, without a seed.

The console runs as its own unit (``polyrob-webview.service``) and, once the
master seed moved to ``/etc/polyrob/wallet.env``, holds none. The addresses it
renders are PUBLIC, so a seedless console must still name them — otherwise the
one page that shows the agent's book cannot say who the book belongs to.

Nothing in ``api_positions`` changes for this: the factory hands it a
public-only wallet and the existing address reads answer. That is the point of
the test — the fix is at the wallet seam, not in another surface.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import core.wallet.factory as factory
from core.wallet import public_identity
from core.wallet.agent_wallet import AgentWallet
from core.wallet.config import load_wallet_config

SEED = "c" * 40


@pytest.fixture(autouse=True)
def _isolated_data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u1")
    monkeypatch.delenv("AGENT_WALLET_DERIVATION", raising=False)
    factory.reset_agent_wallet_cache()
    yield
    factory.reset_agent_wallet_cache()


def _router_client():
    import webview.pages as pages
    app = FastAPI()
    app.include_router(pages.router)
    return TestClient(app), pages


def test_positions_shows_addresses_with_no_seed(monkeypatch):
    seeded = AgentWallet(load_wallet_config({"AGENT_WALLET_ENABLED": "true",
                                             "AGENT_WALLET_MASTER_SEED": SEED}))
    public_identity.write_public_identity(seeded)

    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.delenv("AGENT_WALLET_MASTER_SEED", raising=False)
    factory.reset_agent_wallet_cache()

    client, pages = _router_client()
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "u1")
    # Keep the read off the network: only the ADDRESS half is under test.
    monkeypatch.setattr("tools.defi.defi_data_enabled", lambda: False)

    body = client.get("/api/webgate/positions").json()
    assert body["addresses"]["evm"] == seeded.address
    assert body["addresses"]["solana"] == seeded.solana_address


def test_positions_addresses_absent_when_nothing_was_ever_published(monkeypatch):
    """No record = no snapshot has been taken, which is a real answer — never a
    fabricated or half-right address."""
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.delenv("AGENT_WALLET_MASTER_SEED", raising=False)
    factory.reset_agent_wallet_cache()

    client, pages = _router_client()
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "u1")
    monkeypatch.setattr("tools.defi.defi_data_enabled", lambda: False)

    body = client.get("/api/webgate/positions").json()
    assert body["addresses"] == {}
