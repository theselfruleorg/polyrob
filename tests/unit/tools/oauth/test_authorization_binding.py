import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest

from tools.oauth import OAuthError, GenericOAuth2Provider
from tests.unit.tools.oauth.test_oauth_manager import _manager


def setup():
    manager = _manager()
    calls = []

    async def post(url, data):
        calls.append((url, data))
        return {'access_token': 'access', 'refresh_token': 'refresh'}

    provider = GenericOAuth2Provider('test', {'client_id': 'id', 'auth_url': 'https://auth.example/start',
        'token_url': 'https://auth.example/token', 'redirect_uri': 'https://app.example/callback'}, http_post=post)
    manager.register(provider)
    return manager, calls


@pytest.mark.asyncio
async def test_pkce_callback_is_bound_to_user_provider_redirect_and_one_use():
    manager, calls = setup()
    query = parse_qs(urlsplit(manager.begin_authorization('alice', 'test')).query)
    state = query['state'][0]
    assert query['code_challenge_method'] == ['S256']
    assert 'code_verifier' not in query
    for user, provider, supplied in [('bob', 'test', state), ('alice', 'other', state), ('alice', 'test', 'bad')]:
        with pytest.raises(OAuthError, match='state'):
            await manager.finish_authorization(user, provider, state=supplied, code='code')
    assert calls == []
    await manager.finish_authorization('alice', 'test', state=state, code='code')
    data = calls[0][1]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(data['code_verifier'].encode()).digest()).rstrip(b'=').decode()
    assert query['code_challenge'] == [challenge]
    assert data['redirect_uri'] == query['redirect_uri'][0]
    assert manager.load_token('alice', 'test').access_token == 'access'
    assert manager.load_token('bob', 'test') is None
    with pytest.raises(OAuthError, match='state'):
        await manager.finish_authorization('alice', 'test', state=state, code='code')
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_expiry_and_restart_refuse_before_exchange(monkeypatch):
    manager, calls = setup()
    query = parse_qs(urlsplit(manager.begin_authorization('alice', 'test')).query)
    state = query['state'][0]
    import tools.oauth.manager as module
    future = module.time.monotonic() + 601
    monkeypatch.setattr(module.time, 'monotonic', lambda: future)
    with pytest.raises(OAuthError, match='expired'):
        await manager.finish_authorization('alice', 'test', state=state, code='code')
    assert not calls
    fresh, _ = setup()
    with pytest.raises(OAuthError, match='state'):
        await fresh.finish_authorization('alice', 'test', state=state, code='code')


@pytest.mark.asyncio
async def test_pending_flow_does_not_follow_reconfigured_provider():
    manager, calls = setup()
    state = parse_qs(urlsplit(manager.begin_authorization('alice', 'test')).query)['state'][0]
    replacement = GenericOAuth2Provider('test', {'client_id': 'id', 'auth_url': 'https://other/start',
        'token_url': 'https://other/token'}, http_post=lambda *args: pytest.fail('retargeted callback'))
    manager.register(replacement)
    await manager.finish_authorization('alice', 'test', state=state, code='code')
    assert calls[0][0] == 'https://auth.example/token'


def test_pending_authorization_count_is_bounded():
    manager, _ = setup()
    for _ in range(128):
        manager.begin_authorization('alice', 'test')
    with pytest.raises(OAuthError, match='Too many'):
        manager.begin_authorization('alice', 'test')


@pytest.mark.asyncio
@pytest.mark.parametrize('verifier', ['', 'short', 'x' * 129, '!' * 43])
async def test_provider_refuses_missing_or_invalid_pkce(verifier):
    manager, calls = setup()
    provider = manager.get_provider('test')
    with pytest.raises(OAuthError, match='PKCE'):
        provider.authorize_url(state='s' * 43, code_verifier=verifier)
    with pytest.raises(OAuthError, match='PKCE'):
        await provider.exchange_code('code', code_verifier=verifier)
    assert not calls
