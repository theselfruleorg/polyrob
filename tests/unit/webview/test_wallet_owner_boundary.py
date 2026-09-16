import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
import webview.pages as pages


@pytest.mark.parametrize('path', ['positions', 'book', 'ledger', 'liquidity'])
def test_customer_cannot_read_operator_wallet(monkeypatch, path):
    monkeypatch.setenv('POLYROB_OWNER_USER_ID', 'owner')
    monkeypatch.setattr(pages, '_effective_user_id', lambda req: 'customer')
    def forbidden():
        raise AssertionError('wallet must not be resolved')
    monkeypatch.setattr('core.wallet.factory.get_agent_wallet', forbidden)
    app = FastAPI()
    app.include_router(pages.router)
    assert TestClient(app).get('/api/webgate/' + path).status_code == 403


def test_owner_cannot_select_arbitrary_ledger(monkeypatch):
    monkeypatch.setenv('POLYROB_OWNER_USER_ID', 'owner')
    monkeypatch.setattr(pages, '_effective_user_id', lambda req: 'owner')
    app = FastAPI()
    app.include_router(pages.router)
    assert TestClient(app).get('/api/webgate/positions?ledger=/tmp/other').status_code == 400


def test_owner_wallet_reader_is_mounted_and_scoped(monkeypatch):
    monkeypatch.setenv('POLYROB_OWNER_USER_ID', 'owner')
    monkeypatch.setattr(pages, '_effective_user_id', lambda req: 'owner')
    seen = {}

    def view(uid, *, data_dir=None):
        from core.wallet.view import WalletView
        seen.update(uid=uid, data_dir=data_dir)
        return WalletView(owner=uid, state='public_only')

    monkeypatch.setattr('core.wallet.view.wallet_view', view)
    app = FastAPI()
    app.include_router(pages.router)
    response = TestClient(app).get('/api/webgate/wallet')
    assert response.status_code == 200
    assert response.json()['state'] == 'public_only'
    assert seen['uid'] == 'owner'
