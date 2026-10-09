"""X OAuth 2.0 user token: encrypted store + auto-refresh (2026-09-17).

The X Chat DM read needs a user-context OAuth2 token that X expires after two
hours. The tree used to read ONE static env value and never refreshed it, so a
hand-minted token proved the rail once and then died. These tests pin the
resolver order (store → env seed → static env), the refresh-before-expiry
skew, refresh-token ROTATION, and that a failed refresh keeps the old pair.
"""
import pytest

pytest.importorskip("polyrob_x")

import json
import time

import httpx
from cryptography.fernet import Fernet
import pytest

from polyrob_x import x_oauth2 as xo


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("TWITTER_OAUTH2_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("TWITTER_OAUTH2_REFRESH_TOKEN", raising=False)
    monkeypatch.setenv("TWITTER_OAUTH2_CLIENT_ID", "cid123")
    monkeypatch.delenv("TWITTER_OAUTH2_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(xo, "_instance_key", lambda: "dangerob")
    return xo.XOAuth2Store(tmp_path / "x.json")


def _token_transport(responses):
    """MockTransport that records the form posted and replays `responses`."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": {"id": "12345", "username": "agent"}})
        calls.append({"form": dict(httpx.QueryParams(request.content.decode())),
                      "auth": request.headers.get("authorization", "")})
        status, body = responses.pop(0)
        return httpx.Response(status, json=body)
    return httpx.MockTransport(handler), calls


def test_store_roundtrip_is_encrypted_on_disk(store, tmp_path):
    xo.import_pair("acc-1", "ref-1", store=store)
    raw = (tmp_path / "x.json").read_text()
    assert "acc-1" not in raw and "ref-1" not in raw
    rec = store.load()
    assert rec["access_token"] == "acc-1" and rec["refresh_token"] == "ref-1"
    assert rec["expires_at"] > time.time() + 7000  # fresh 7200 s assumed


def test_resolve_returns_stored_token_when_not_near_expiry(store):
    xo.import_pair("acc-1", "ref-1", store=store)
    transport, calls = _token_transport([])
    assert xo.resolve_access_token(store=store, transport=transport) == "acc-1"
    assert calls == []  # no network when the token is healthy


def test_resolve_refreshes_within_skew_and_rotates_refresh_token(store):
    xo.import_pair("acc-old", "ref-old", expires_in=60, store=store)  # < REFRESH_SKEW_SEC
    transport, calls = _token_transport([(200, {
        "access_token": "acc-new", "refresh_token": "ref-new",
        "expires_in": 7200, "scope": "dm.read users.read tweet.read offline.access",
        "token_type": "bearer"})])
    tok = xo.resolve_access_token(store=store, transport=transport)
    assert tok == "acc-new"
    assert calls[0]["form"]["grant_type"] == "refresh_token"
    assert calls[0]["form"]["refresh_token"] == "ref-old"
    assert calls[0]["form"]["client_id"] == "cid123"
    assert calls[0]["auth"] == ""  # public app: no Basic header
    rec = store.load()
    assert rec["refresh_token"] == "ref-new" and rec["source"] == "refresh"
    assert rec["expires_at"] > time.time() + 7000


def test_building_a_client_never_spends_the_refresh_token(store):
    """2026-10-02: every process that BUILT a TwitterTool (a deploy import gate,
    a CLI run) spent the rotating refresh token. refresh=False reads only."""
    xo.import_pair("acc-old", "ref-old", expires_in=60, store=store)  # due
    transport, calls = _token_transport([])
    assert xo.resolve_access_token(store=store, transport=transport, refresh=False) == "acc-old"
    assert calls == []
    assert store.load()["refresh_token"] == "ref-old"


def test_refresh_log_names_the_process(store, caplog):
    xo.import_pair("acc-old", "ref-old", expires_in=60, store=store)
    transport, _ = _token_transport([(200, {"access_token": "n", "refresh_token": "r2",
                                            "expires_in": 7200})])
    with caplog.at_level("INFO", logger="polyrob_x.x_oauth2"):
        xo.resolve_access_token(store=store, transport=transport)
    assert any("token refreshed by " in r.getMessage() and "uid=" in r.getMessage()
               for r in caplog.records)


def test_confidential_app_sends_basic_auth(store, monkeypatch):
    monkeypatch.setenv("TWITTER_OAUTH2_CLIENT_SECRET", "shh")
    xo.import_pair("a", "r", expires_in=10, store=store)
    transport, calls = _token_transport([(200, {"access_token": "b", "refresh_token": "r2",
                                                "expires_in": 7200})])
    xo.resolve_access_token(store=store, transport=transport)
    assert calls[0]["auth"].startswith("Basic ")


def test_failed_refresh_keeps_the_old_pair_and_returns_old_token(store):
    xo.import_pair("acc-old", "ref-old", expires_in=10, store=store)
    transport, _ = _token_transport([(400, {"error": "invalid_request",
                                           "error_description": "Value passed for the token was invalid."})])
    tok = xo.resolve_access_token(store=store, transport=transport)
    assert tok == "acc-old"  # honest: the API's 401 is the signal, not a silent swap
    rec = store.load()
    assert rec["refresh_token"] == "ref-old" and rec["source"] == "import"


def test_refresh_without_client_id_names_the_remedy(store, monkeypatch):
    monkeypatch.delenv("TWITTER_OAUTH2_CLIENT_ID", raising=False)
    xo.import_pair("a", "r", store=store)
    with pytest.raises(RuntimeError) as ei:
        xo.refresh(store=store)
    assert "TWITTER_OAUTH2_CLIENT_ID" in str(ei.value)


def test_env_pair_seeds_the_store_once(store, monkeypatch):
    monkeypatch.setenv("TWITTER_OAUTH2_ACCESS_TOKEN", "env-acc")
    monkeypatch.setenv("TWITTER_OAUTH2_REFRESH_TOKEN", "env-ref")
    transport, calls = _token_transport([])
    assert xo.resolve_access_token(store=store, transport=transport) == "env-acc"
    rec = store.load()
    assert rec["source"] == "env" and rec["refresh_token"] == "env-ref"
    # the store now owns it: a changed env value is NOT re-read
    monkeypatch.setenv("TWITTER_OAUTH2_ACCESS_TOKEN", "env-acc-2")
    assert xo.resolve_access_token(store=store, transport=transport) == "env-acc"


def test_static_env_token_without_refresh_is_used_but_not_stored(store, monkeypatch):
    monkeypatch.setenv("TWITTER_OAUTH2_ACCESS_TOKEN", "static-acc")
    transport, _ = _token_transport([])
    assert xo.resolve_access_token(store=store, transport=transport) == "static-acc"
    assert store.load() is None


def test_status_never_exposes_token_values(store):
    xo.import_pair("acc-secret", "ref-secret", store=store)
    st = xo.status(store=store)
    blob = json.dumps(st)
    assert "acc-secret" not in blob and "ref-secret" not in blob
    assert st["stored"] and st["has_refresh_token"] and st["client_id_set"]


def test_pkce_pair_is_s256():
    import base64, hashlib
    v, c = xo.pkce_pair()
    assert 43 <= len(v) <= 128
    assert c == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")


def test_exchange_code_persists_pkce_pair(store):
    transport, calls = _token_transport([(200, {"access_token": "A", "refresh_token": "R",
                                                "expires_in": 7200, "scope": "dm.read"})])
    rec = xo.exchange_code("thecode", redirect_uri="http://127.0.0.1:8765/callback",
                           code_verifier="ver", store=store, transport=transport, expected_account_id="12345")
    f = calls[0]["form"]
    assert f["grant_type"] == "authorization_code" and f["code"] == "thecode"
    assert f["code_verifier"] == "ver" and f["redirect_uri"] == "http://127.0.0.1:8765/callback"
    assert rec["source"] == "pkce" and store.load()["access_token"] == "A"


# --- the surface client retries ONCE on 401 with a refreshed token -----------

@pytest.mark.asyncio
async def test_xdm_client_refreshes_and_retries_on_401(monkeypatch):
    import tweepy
    from polyrob_x.surface.client import XDMClient

    # A stateful stand-in for the store-backed resolver: a forced refresh
    # rotates the token; a plain resolve returns whatever is current.
    state = {"cur": "tok-old"}

    def _resolve(force_refresh=False, refresh=True):
        if force_refresh:
            state["cur"] = "tok-new"
        return state["cur"]
    monkeypatch.setattr(XDMClient, "_resolve_oauth2", staticmethod(_resolve))
    c = XDMClient()
    assert c._oauth2_access_token == "tok-old"

    class _Resp:
        def __init__(self, uid): self.data = type("D", (), {"id": uid})()

    built = []

    class FakeTweepy:
        def __init__(self, *, bearer_token, wait_on_rate_limit):
            built.append(bearer_token)
            self._bt = bearer_token

        def get_me(self, user_auth=False):
            if self._bt == "tok-old":
                raise tweepy.Unauthorized(httpx.Response(401, request=httpx.Request("GET", "https://x")))
            return _Resp("42")

    # tweepy.Unauthorized wants a requests-like response; build a stand-in.
    class _R:
        status_code = 401
        reason = "Unauthorized"
        text = "{}"
        def json(self): return {}
    monkeypatch.setattr(tweepy.Unauthorized, "__init__",
                        lambda self, *a, **k: Exception.__init__(self, "401"))
    monkeypatch.setattr("tweepy.Client", FakeTweepy)
    uid = await c.get_me()
    assert uid == "42"
    assert built == ["tok-old", "tok-new"]  # rebuilt exactly once on the fresh token


def test_twitter_tool_picks_up_a_token_imported_after_start(monkeypatch):
    """The tool started with NO OAuth2 token; a later oauth-import must be
    usable on the next DM read without a restart."""
    import logging
    from unittest.mock import MagicMock
    from polyrob_x.twitter_tool import TwitterTool
    t = object.__new__(TwitterTool)
    t.logger = logging.getLogger("tw")
    t.oauth2_access_token = None
    t.dm_client = None
    t.chat_client = None
    t.chat_private_keys_b64 = None
    t.chat_key_version = None
    t.chat_passphrase = None
    monkeypatch.setattr(TwitterTool, "_resolve_oauth2_token",
                        staticmethod(lambda force_refresh=False, refresh=True: "late-token"))
    monkeypatch.setattr("tweepy.Client", lambda **kw: MagicMock(bt=kw["bearer_token"]))
    t._ensure_oauth2_fresh()
    assert t.oauth2_access_token == "late-token"
    assert t.dm_client is not None and t.chat_client is not None


# --- a DEAD refresh token: relogin verdict, no re-POST (2026-09-26 P0/P1-1/P1-2) ---

_DEAD = (400, {"error": "invalid_request",
               "error_description": "Value passed for the token was invalid."})


@pytest.fixture(autouse=False)
def fresh_log_state():
    xo._REFUSAL_LOGGED.clear()
    yield
    xo._REFUSAL_LOGGED.clear()


@pytest.mark.parametrize("body", [
    {"error": "invalid_grant", "error_description": "refresh token revoked"},
    {"error": "invalid_request", "error_description": "Value passed for the token was invalid."},
])
def test_dead_refresh_token_raises_relogin_needed_and_records_the_verdict(store, body, fresh_log_state):
    from core.credential_verdicts import verdict
    xo.import_pair("acc-old", "ref-old", expires_in=10, store=store)
    transport, _ = _token_transport([(400, body)])
    with pytest.raises(xo.ReloginNeeded) as ei:
        xo.refresh(store=store, transport=transport)
    assert isinstance(ei.value, RuntimeError)
    assert "/x login" in str(ei.value) and "oauth-login" in str(ei.value)
    v = verdict("x_oauth2")
    assert v is not None and v.code == "relogin_needed"
    assert "/x login" in v.remedy and "polyrob x-account oauth-login" in v.remedy


def test_other_token_errors_are_not_relogin(store):
    from core.credential_verdicts import verdict
    xo.import_pair("a", "r", expires_in=10, store=store)
    transport, _ = _token_transport([(503, {"error": "server_error"})])
    with pytest.raises(RuntimeError) as ei:
        xo.refresh(store=store, transport=transport)
    assert not isinstance(ei.value, xo.ReloginNeeded)
    assert verdict("x_oauth2") is None


def test_open_verdict_stops_the_dead_refresh_token_being_reposted(store, caplog, fresh_log_state):
    import logging
    xo.import_pair("acc-old", "ref-old", expires_in=10, store=store)
    transport, calls = _token_transport([_DEAD])
    with caplog.at_level(logging.WARNING, logger=xo.__name__):
        assert xo.resolve_access_token(store=store, transport=transport) == "acc-old"
        assert len(calls) == 1
        # second + third resolve (and a forced one): NO POST
        xo.resolve_access_token(store=store, transport=transport)
        xo.resolve_access_token(store=store, transport=transport, force_refresh=True)
    assert len(calls) == 1
    held = [r for r in caplog.records if "not retrying" in r.getMessage()]
    assert len(held) == 1 and "/x login" in held[0].getMessage()
    st = xo.status(store=store)
    assert st["relogin_needed"] is True and st["relogin_needed_since"]


def test_expired_token_with_open_verdict_resolves_to_none(store, fresh_log_state):
    xo.import_pair("acc-old", "ref-old", expires_in=10, store=store)
    transport, calls = _token_transport([_DEAD])
    xo.resolve_access_token(store=store, transport=transport)
    rec = store.load()
    rec["expires_at"] = time.time() - 60
    store.save(rec)
    # obtained_at kept older than the verdict (the save above keeps it)
    assert xo.resolve_access_token(store=store, transport=transport) is None
    assert len(calls) == 1


def test_successful_exchange_clears_the_verdict_and_rearms_refresh(store, fresh_log_state):
    from core.credential_verdicts import verdict
    xo.import_pair("acc-old", "ref-old", expires_in=10, store=store)
    transport, calls = _token_transport([
        _DEAD,
        (200, {"access_token": "A2", "refresh_token": "R2", "expires_in": 7200}),
    ])
    xo.resolve_access_token(store=store, transport=transport)
    assert verdict("x_oauth2") is not None
    xo.exchange_code("c", redirect_uri="http://127.0.0.1:8765/callback",
                     code_verifier="v", store=store, transport=transport, expected_account_id="12345")
    assert verdict("x_oauth2") is None
    assert xo.status(store=store)["relogin_needed"] is False
    assert xo.resolve_access_token(store=store, transport=transport) == "A2"


def test_a_newer_store_record_outranks_a_stale_verdict(store, fresh_log_state):
    """The owner re-logged in from a process that could not clear the verdict:
    a record obtained after first_seen re-arms the refresh."""
    from core.credential_verdicts import record_rejection
    record_rejection("x_oauth2", "", code="relogin_needed", remedy="x")
    time.sleep(0.01)
    rec = xo._record_from_response({"access_token": "N", "refresh_token": "NR",
                                    "expires_in": 10}, source="pkce")
    store.save(rec)
    transport, calls = _token_transport([(200, {"access_token": "N2", "refresh_token": "NR2",
                                                "expires_in": 7200})])
    assert xo.relogin_verdict(store=store) is None
    assert xo.resolve_access_token(store=store, transport=transport) == "N2"
    assert len(calls) == 1


def test_successful_refresh_and_import_clear_the_verdict(store):
    from core.credential_verdicts import record_rejection, verdict
    record_rejection("x_oauth2", "", code="relogin_needed", remedy="x")
    xo.import_pair("a", "r", store=store)
    assert verdict("x_oauth2") is None
    record_rejection("x_oauth2", "", code="relogin_needed", remedy="x")
    transport, _ = _token_transport([(200, {"access_token": "b", "refresh_token": "r2",
                                            "expires_in": 7200})])
    xo.refresh(store=store, transport=transport)
    assert verdict("x_oauth2") is None


def test_x_oauth2_is_an_open_remedy_kind_with_a_ttl():
    import core.credential_verdicts as cv
    assert "x_oauth2" in cv.OPEN_REMEDY_KINDS
    assert cv.DEFAULT_TTL_BY_KIND["x_oauth2"] > 0


# --- cross-process refresh lock + no env-seed clobber (P1-3) ------------------

_CHILD = r'''
import os, sys, time, json
import httpx
from polyrob_x import x_oauth2 as xo
xo._instance_key = lambda: "dangerob"
counter = sys.argv[2]

def handler(request):
    with open(counter, "a") as fh:
        fh.write("post\n")
    time.sleep(0.6)   # hold the "server" long enough for the peer to arrive
    n = sum(1 for _ in open(counter))
    return httpx.Response(200, json={"access_token": f"acc-{n}",
                                     "refresh_token": f"ref-{n}", "expires_in": 7200})

open(sys.argv[3], "a").write("ready\n")
while sum(1 for _ in open(sys.argv[3])) < 2:
    time.sleep(0.01)
tok = xo.resolve_access_token(store=xo.XOAuth2Store(sys.argv[1]),
                              transport=httpx.MockTransport(handler))
print(tok)
'''


def _fresh_encryption(monkeypatch):
    """get_encryption() is a process singleton: drop it so the CURRENT
    MCP_ENCRYPTION_KEY is the one in use (what a child process will see)."""
    import core.security.encryption as enc
    monkeypatch.delattr(enc, "_encryption_instance", raising=False)


def test_two_processes_refresh_the_rotating_token_exactly_once(store, tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    _fresh_encryption(monkeypatch)
    xo.import_pair("acc-old", "ref-old", expires_in=10, store=store)
    counter = tmp_path / "posts.txt"
    counter.write_text("")
    barrier = tmp_path / "ready.txt"
    barrier.write_text("")
    script = tmp_path / "child.py"
    script.write_text(_CHILD)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    procs = [subprocess.Popen([sys.executable, str(script), str(tmp_path / "x.json"),
                               str(counter), str(barrier)],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True)
             for _ in range(2)]
    outs = [p.communicate(timeout=60) for p in procs]
    for p, (out, err) in zip(procs, outs):
        assert p.returncode == 0, err
    assert counter.read_text().count("post") == 1, outs  # ONE refresh POST
    toks = {out.strip().splitlines()[-1] for out, _ in outs}
    assert toks == {"acc-1"}                              # the loser re-loaded the winner's pair
    store.reload()
    assert store.load()["refresh_token"] == "ref-1"
    assert (tmp_path / "x.json.lock").exists()


def test_env_seed_never_clobbers_an_undecryptable_record(store, monkeypatch, tmp_path):
    from cryptography.fernet import Fernet
    xo.import_pair("acc-real", "ref-real", store=store)
    raw_before = (tmp_path / "x.json").read_text()
    # another key (e.g. the CLI without MCP_ENCRYPTION_KEY) cannot decrypt it
    monkeypatch.setenv("MCP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    _fresh_encryption(monkeypatch)
    monkeypatch.setenv("TWITTER_OAUTH2_ACCESS_TOKEN", "env-acc")
    monkeypatch.setenv("TWITTER_OAUTH2_REFRESH_TOKEN", "env-ref")
    other = xo.XOAuth2Store(tmp_path / "x.json")
    assert other.load() is None and other.has_key()
    transport, calls = _token_transport([])
    assert xo.resolve_access_token(store=other, transport=transport) == "env-acc"
    assert (tmp_path / "x.json").read_text() == raw_before  # NOT overwritten
    assert calls == []


def test_wrong_x_account_cannot_replace_existing_pair(store):
    xo.import_pair("old", "old-refresh", store=store)
    before = store.load()
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"access_token": "new", "refresh_token": "new-refresh"})
        return httpx.Response(200, json={"data": {"id": "999", "username": "wrong"}})
    with pytest.raises(RuntimeError, match="different account"):
        xo.exchange_code("code", redirect_uri="http://localhost/callback", code_verifier="v",
                         store=store, transport=httpx.MockTransport(handler), expected_account_id="12345")
    assert store.load() == before


def test_unpinned_x_login_refuses_before_exchanging_code(store):
    with pytest.raises(RuntimeError, match="account-id"):
        xo.exchange_code("code", redirect_uri="http://localhost/callback", code_verifier="v",
                         store=store, transport=httpx.MockTransport(lambda r: pytest.fail("network called")))


def test_x_account_pin_survives_token_refresh(store):
    xo.import_pair("A", "R", store=store)
    rec = store.load()
    rec.update(account_id="12345", account_username="agent")
    store.save(rec)
    transport, _ = _token_transport([(200, {"access_token": "A2", "refresh_token": "R2"})])
    refreshed = xo.refresh(store=store, transport=transport)
    assert refreshed["account_id"] == "12345"
    assert xo.expected_account(store) == "12345"


def test_refresh_lock_needs_only_read_access_and_is_group_shared(tmp_path):
    """A lock another identity created without write access for us must still
    serialise the refresh (no silent in-process fallback), and in a
    group-writable home the lock is group-openable, never world-openable."""
    import os
    import stat

    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o2770)
    store = xo.XOAuth2Store(shared / "x.json")
    lock = shared / "x.json.lock"
    lock.write_text("")
    os.chmod(lock, 0o440)
    xo._LOCK_WARNED.clear()
    with xo._refresh_lock(store):
        pass
    assert str(lock) not in xo._LOCK_WARNED
    assert stat.S_IMODE(lock.stat().st_mode) == 0o660


def test_refresh_lock_still_refuses_a_symlink(tmp_path):
    import os

    target = tmp_path / "elsewhere"
    target.write_text("")
    store = xo.XOAuth2Store(tmp_path / "x.json")
    os.symlink(str(target), str(tmp_path / "x.json.lock"))
    xo._LOCK_WARNED.clear()
    with xo._refresh_lock(store):
        pass
    assert str(tmp_path / "x.json.lock") in xo._LOCK_WARNED
