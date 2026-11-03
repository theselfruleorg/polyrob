"""098 TS-13: an OAuth refresh the provider refused is a stored needs_reauth."""
import asyncio
import time

import pytest

from tools.mcp.security import MCPEncryption
from tools.oauth import OAuthError, OAuthManager, OAuthProvider, OAuthToken
from tools.oauth.manager import TOKEN


@pytest.fixture(autouse=True)
def _verdicts(monkeypatch):
    import core.credential_verdicts as cv
    seen = []
    monkeypatch.setattr(cv, "record_rejection", lambda *a, **k: seen.append((a, k)))
    monkeypatch.setattr(cv, "clear_rejection", lambda *a, **k: seen.append(("clear", a)))
    return seen


class _Prov(OAuthProvider):
    name = "p"

    def __init__(self, fail=None):
        self.fail, self.calls = fail, 0

    def authorize_url(self, *, state, code_verifier, redirect_uri=None):
        return "https://auth.example/a"

    async def exchange_code(self, code, *, code_verifier, redirect_uri=None):
        return OAuthToken(access_token="fresh-grant", refresh_token="r1")

    async def refresh(self, token):
        self.calls += 1
        await asyncio.sleep(0.01)
        if self.fail:
            raise self.fail
        return OAuthToken(access_token=f"a{self.calls}", refresh_token="r0",
                          expires_at=time.time() + 3600)


def _mgr(prov):
    m = OAuthManager(encryption=MCPEncryption(key=MCPEncryption.generate_key()), store={})
    m.register(prov)
    m.store_token("u", "p", OAuthToken(access_token="old", refresh_token="r0",
                                       expires_at=time.time() - 10))
    return m


def test_table_is_well_formed():
    assert TOKEN.validate() == []


def test_invalid_grant_marks_needs_reauth_and_stops_refreshing(_verdicts):
    async def go():
        prov = _Prov(fail=RuntimeError('400 {"error": "invalid_grant"}'))
        m = _mgr(prov)
        with pytest.raises(OAuthError):
            await m.get_token("u", "p")
        assert m.state("u", "p") == "needs_reauth" and prov.calls == 1
        with pytest.raises(OAuthError, match="re-authorize p"):
            await m.get_token("u", "p")
        with pytest.raises(OAuthError, match="re-authorize p"):
            await m.force_refresh("u", "p")
        assert prov.calls == 1  # no further provider calls
        assert any(a[0] == "oauth" for a, _ in [x for x in _verdicts if x[0] != "clear"])
    asyncio.run(go())


def test_network_error_does_not_mark_needs_reauth():
    async def go():
        prov = _Prov(fail=ConnectionError("dns failure"))
        m = _mgr(prov)
        with pytest.raises(ConnectionError):
            await m.get_token("u", "p")
        assert m.state("u", "p") == "valid"
        prov.fail = None
        assert (await m.get_token("u", "p")).access_token == "a2"
    asyncio.run(go())


def test_authorize_clears_needs_reauth():
    async def go():
        prov = _Prov(fail=RuntimeError("invalid_grant"))
        m = _mgr(prov)
        with pytest.raises(OAuthError):
            await m.get_token("u", "p")
        url = m.begin_authorization("u", "p", redirect_uri="https://x/cb")
        state = url and next(iter(m._authorizations))
        await m.finish_authorization("u", "p", state=state, code="c")
        assert m.state("u", "p") == "valid"
        assert (await m.get_token("u", "p")).access_token == "fresh-grant"
    asyncio.run(go())


def test_force_refresh_after_peer_refresh_returns_current():
    async def go():
        prov = _Prov()
        m = _mgr(prov)
        first = await m.force_refresh("u", "p", observed_access_token="old")
        # a second caller whose request carried the OLD token: no second refresh
        second = await m.force_refresh("u", "p", observed_access_token="old")
        assert first.access_token == second.access_token == "a1" and prov.calls == 1
    asyncio.run(go())


def test_a_refresh_a_peer_already_rotated_returns_the_peer_token(_verdicts):
    """098 review: another process refreshed (and rotated the refresh token)
    while this one held the old token; the provider refuses our stale refresh,
    but the store now holds the peer's good token — return it, never latch
    needs_reauth over it."""
    async def go():
        good = OAuthToken(access_token="peer-fresh", refresh_token="r9",
                          expires_at=time.time() + 3600)

        class _Racing(_Prov):
            async def refresh(self, token):
                m.store_token("u", "p", good)          # the peer's write lands first
                raise RuntimeError('400 {"error": "invalid_grant"}')
        m = _mgr(_Racing())
        tok = await m.get_token("u", "p")
        assert tok.access_token == "peer-fresh"
        assert m.state("u", "p") == "valid"
    asyncio.run(go())


def test_the_file_store_reads_a_peer_write(tmp_path):
    from tools.oauth.file_store import FileTokenStore
    a = FileTokenStore(tmp_path / "t.json")
    b = FileTokenStore(tmp_path / "t.json")          # a second process's snapshot
    a[("u", "p")] = b"v1"
    assert b[("u", "p")] == b"v1"
    a[("u", "p")] = b"v2-longer"
    assert b.get(("u", "p")) == b"v2-longer" and ("u", "p") in b
