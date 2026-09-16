from fastapi import FastAPI
from fastapi.testclient import TestClient

import webview.pages as pages


def _client(monkeypatch, user_id):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner")
    monkeypatch.setattr(pages, "_effective_user_id", lambda request: user_id)
    app = FastAPI()
    app.include_router(pages.router)
    return TestClient(app)


def test_wallet_reader_is_owner_scoped(monkeypatch):
    assert _client(monkeypatch, "customer").get("/api/webgate/wallet").status_code == 403


def test_wallet_reader_returns_public_view(monkeypatch, tmp_path):
    seen = {}

    def view(uid, *, data_dir=None):
        from core.wallet.view import WalletView
        seen.update(uid=uid, data_dir=data_dir)
        return WalletView(owner=uid, state="public_only")

    monkeypatch.setattr("core.wallet.view.wallet_view", view)
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    response = _client(monkeypatch, "owner").get("/api/webgate/wallet")
    assert response.status_code == 200
    assert response.json()["state"] == "public_only"
    assert seen == {"uid": "owner", "data_dir": str(tmp_path)}
