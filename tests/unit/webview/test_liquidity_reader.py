"""Liquidity reader: tenant scope, explicit network reads, owner-only wallet."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.status_snapshot import Section
import webview.pages as pages


def client(monkeypatch, uid='tenant'):
    monkeypatch.setenv('POLYROB_OWNER_USER_ID', 'owner')
    monkeypatch.setattr(pages, '_effective_user_id', lambda request: uid)
    app = FastAPI()
    app.include_router(pages.router)
    return TestClient(app)


def test_default_is_owner_scoped_and_does_not_enumerate(monkeypatch):
    seen = []
    def section(uid, data_dir, enumerate_fn):
        seen.append((uid, enumerate_fn))
        return Section(name='liquidity', data={'held': None, 'moved': []})
    monkeypatch.setattr('core.status_liquidity.liquidity_section', section)
    response = client(monkeypatch, 'owner').get('/api/webgate/liquidity')
    assert response.status_code == 200
    assert response.json()['data']['held'] is None
    assert seen == [('owner', None)]


def test_non_owner_cannot_enumerate_treasury(monkeypatch):
    def forbidden(*args):
        raise AssertionError('must reject before reading wallet')
    monkeypatch.setattr('core.wallet.factory.get_agent_wallet', forbidden)
    assert client(monkeypatch).get('/api/webgate/liquidity?onchain=true').status_code == 403


def test_owner_wallet_failure_is_unavailable_not_empty(monkeypatch, tmp_path):
    from tests.unit.core.test_status_collectibles import _db
    _db(tmp_path, [])
    monkeypatch.setattr(pages, '_data_dir', lambda: str(tmp_path))
    monkeypatch.setattr('core.wallet.factory.get_agent_wallet', lambda: None)
    response = client(monkeypatch, 'owner').get('/api/webgate/liquidity?onchain=true')
    assert response.status_code == 200
    data = response.json()['data']
    assert data['held'] is None
    assert 'not enabled' in data['held_error']
