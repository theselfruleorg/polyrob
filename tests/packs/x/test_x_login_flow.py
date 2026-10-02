"""`/x login` — the one-tap X OAuth 2.0 re-login (option A of the X evaluation).

Pins the pending record: it lives encrypted in the SAME store file as the
token (so the agent process that runs the verb and the console process that
receives the callback share it), it is single use, it expires after ten
minutes, a wrong state finds nothing, and it is bound to the owner who minted it.
"""
import pytest

pytest.importorskip("polyrob_x")

import time
from urllib.parse import parse_qs, urlsplit

import httpx
from cryptography.fernet import Fernet

from polyrob_x import x_login_flow as flow
from polyrob_x import x_oauth2 as xo

REDIRECT = "https://console.example" + flow.CALLBACK_PATH


@pytest.fixture
def path(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("TWITTER_OAUTH2_CLIENT_ID", "cid123")
    monkeypatch.delenv("TWITTER_OAUTH2_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("TWITTER_OAUTH2_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("TWITTER_OAUTH2_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr(xo, "_instance_key", lambda: "rob1")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))   # the default store = this one
    return tmp_path / ".x_session.json"


def _state_of(url):
    q = parse_qs(urlsplit(url).query)
    return q["state"][0], q


def _transport(calls, status=200, body=None):
    body = body if body is not None else {
        "access_token": "acc-new", "refresh_token": "ref-new", "expires_in": 7200,
        "scope": "dm.read tweet.read users.read offline.access", "token_type": "bearer"}

    def handler(request):
        calls.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(status, json=body)
    return httpx.MockTransport(handler)


def test_begin_mints_a_pkce_link_and_stores_nothing_in_the_clear(path):
    url = flow.begin_login("owner-1", REDIRECT, path=path)
    state, q = _state_of(url)
    assert url.startswith(xo.AUTHORIZE_URL)
    assert q["redirect_uri"] == [REDIRECT] and q["code_challenge_method"] == ["S256"]
    raw = path.read_text()
    assert state not in raw and "owner-1" not in raw
    assert flow.PENDING_PROVIDER in raw


def test_complete_exchanges_the_code_and_saves_the_pair(path):
    url = flow.begin_login("owner-1", REDIRECT, path=path)
    state, q = _state_of(url)
    calls = []
    res = flow.complete_login(state, "the-code", path=path, transport=_transport(calls))
    assert res.ok and res.owner_user_id == "owner-1"
    assert "dm.read" in res.scope and 7000 < res.expires_in <= 7200
    form = calls[0]
    assert form["grant_type"] == "authorization_code" and form["code"] == "the-code"
    assert form["redirect_uri"] == REDIRECT
    # the verifier matches the challenge sent to X
    import base64
    import hashlib
    digest = hashlib.sha256(form["code_verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).decode().rstrip("=") == q["code_challenge"][0]
    assert xo.XOAuth2Store(path).load()["access_token"] == "acc-new"
    assert "acc-new" not in repr(res)


def test_state_is_single_use(path):
    state, _q = _state_of(flow.begin_login("owner-1", REDIRECT, path=path))
    calls = []
    assert flow.complete_login(state, "c1", path=path, transport=_transport(calls)).ok
    again = flow.complete_login(state, "c2", path=path, transport=_transport(calls))
    assert not again.ok and "already used" in again.reason
    assert len(calls) == 1


def test_a_failed_exchange_still_burns_the_state(path):
    state, _q = _state_of(flow.begin_login("owner-1", REDIRECT, path=path))
    calls = []
    bad = flow.complete_login(state, "c", path=path, transport=_transport(
        calls, status=400, body={"error": "invalid_request"}))
    assert not bad.ok and "invalid_request" in bad.reason and bad.owner_user_id == "owner-1"
    assert not flow.complete_login(state, "c", path=path, transport=_transport(calls)).ok
    assert len(calls) == 1


def test_an_expired_link_is_refused_without_a_post(path):
    now = time.time()
    state, _q = _state_of(flow.begin_login("owner-1", REDIRECT, path=path, now=now))
    calls = []
    res = flow.complete_login(state, "c", path=path, transport=_transport(calls),
                              now=now + flow.PENDING_TTL_SEC + 1)
    assert not res.ok and "expired" in res.reason and calls == []


def test_a_wrong_state_finds_nothing(path):
    flow.begin_login("owner-1", REDIRECT, path=path)
    calls = []
    res = flow.complete_login("not-the-state", "c", path=path, transport=_transport(calls))
    assert not res.ok and "unknown" in res.reason and res.owner_user_id == ""
    assert not flow.complete_login("", "c", path=path, transport=_transport(calls)).ok
    assert calls == []


def test_the_link_is_bound_to_its_owner(path):
    state, _q = _state_of(flow.begin_login("owner-1", REDIRECT, path=path))
    calls = []
    res = flow.complete_login(state, "c", path=path, transport=_transport(calls),
                              expected_owner="someone-else")
    assert not res.ok and "another owner" in res.reason and calls == []
    with pytest.raises(flow.LoginFlowError):
        flow.begin_login("", REDIRECT, path=path)


def test_expired_rows_are_pruned_and_open_links_are_capped(path):
    now = time.time()
    flow.begin_login("owner-1", REDIRECT, path=path, now=now - 2 * flow.PENDING_TTL_SEC)
    for i in range(flow.MAX_PENDING + 2):
        flow.begin_login("owner-1", REDIRECT, path=path, now=now + i)
    from tools.oauth.file_store import FileTokenStore
    rows = [k for k in FileTokenStore(path) if k[1] == flow.PENDING_PROVIDER]
    assert len(rows) == flow.MAX_PENDING


def test_discard_burns_a_declined_link(path):
    state, _q = _state_of(flow.begin_login("owner-1", REDIRECT, path=path))
    assert flow.discard(state, path=path) == "owner-1"
    assert not flow.complete_login(state, "c", path=path).ok


@pytest.mark.parametrize("value, match", [
    ("", "is not set"),
    ("http://console.example" + flow.CALLBACK_PATH, "https"),
    ("https://console.example/x/oauth/callback", "must end in"),
    ("https://console.example" + flow.CALLBACK_PATH + "?a=1", "must end in"),
])
def test_the_redirect_flag_is_never_guessed(monkeypatch, value, match):
    monkeypatch.setenv(flow.REDIRECT_FLAG, value)
    with pytest.raises(flow.LoginFlowError, match=match) as err:
        flow.configured_redirect_uri()
    assert "developer.x.com" in str(err.value)


def test_the_redirect_flag_accepts_https_and_loopback(monkeypatch):
    monkeypatch.setenv(flow.REDIRECT_FLAG, REDIRECT)
    assert flow.configured_redirect_uri() == REDIRECT
    monkeypatch.setenv(flow.REDIRECT_FLAG, "http://127.0.0.1:5050" + flow.CALLBACK_PATH)
    assert flow.configured_redirect_uri().startswith("http://127.0.0.1")


def test_the_verb_answers_the_remedy_when_the_flag_is_unset(path, monkeypatch):
    from polyrob_x.owner_verbs import x_reply
    monkeypatch.delenv(flow.REDIRECT_FLAG, raising=False)
    out = x_reply("owner-1", ["login"])
    assert "X login cannot start" in out and flow.REDIRECT_FLAG in out
    assert flow.CALLBACK_PATH in out


def test_the_verb_sends_the_link(path, monkeypatch):
    from polyrob_x import owner_verbs
    monkeypatch.setenv(flow.REDIRECT_FLAG, REDIRECT)
    out = owner_verbs.x_reply("owner-1", ["login"])
    assert xo.AUTHORIZE_URL in out and "expires in 10 minutes" in out
    assert "Usage" in owner_verbs.x_reply("owner-1", ["frobnicate"])
    status = owner_verbs.x_reply("owner-1", ["status"])
    assert "API login (OAuth 2.0)" in status and "Browser session" in status
    assert "acc-" not in status
