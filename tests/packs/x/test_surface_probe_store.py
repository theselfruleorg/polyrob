"""`polyrob surfaces probe x` reads the OAuth2 token through the store resolver (P2-6)."""
import pytest

pytest.importorskip("polyrob_x")

from polyrob_x.surface import probe as mod


@pytest.mark.asyncio
async def test_probe_uses_the_store_token_not_only_the_env(monkeypatch):
    import polyrob_x.x_oauth2 as xo
    monkeypatch.setattr(xo, "resolve_access_token", lambda **k: "store-token")
    seen = {}

    async def _http(method, url, headers=None, json=None):
        seen["auth"] = headers["Authorization"]
        return 200, {"data": {"username": "rob"}}, ""
    monkeypatch.setattr(mod, "http_json", _http)
    res = await mod.probe({})
    assert res.state == "ok" and "@rob" in res.detail
    assert seen["auth"] == "Bearer store-token"


@pytest.mark.asyncio
async def test_refused_oauth2_names_the_relogin_remedy(monkeypatch):
    import polyrob_x.x_oauth2 as xo
    monkeypatch.setattr(xo, "resolve_access_token", lambda **k: "dead")

    async def _http(method, url, headers=None, json=None):
        return 401, {}, ""
    monkeypatch.setattr(mod, "http_json", _http)
    res = await mod.probe({})
    assert res.state == "failed" and "/x login" in res.detail


@pytest.mark.asyncio
async def test_static_env_token_still_works_without_a_store(monkeypatch):
    import polyrob_x.x_oauth2 as xo
    monkeypatch.setattr(xo, "resolve_access_token", lambda **k: None)
    seen = {}

    async def _http(method, url, headers=None, json=None):
        seen["auth"] = headers["Authorization"]
        return 200, {"data": {"username": "rob"}}, ""
    monkeypatch.setattr(mod, "http_json", _http)
    res = await mod.probe({"TWITTER_OAUTH2_ACCESS_TOKEN": "env-tok"})
    assert res.state == "ok" and seen["auth"] == "Bearer env-tok"
