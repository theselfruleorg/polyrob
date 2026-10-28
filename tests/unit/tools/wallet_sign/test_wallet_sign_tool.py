"""090 P0 — the ``wallet_sign`` tool: the 402Mob-shaped enrollment end to end.

The challenge is fetched by the tool, signed only when it classifies A, posted
back only to the same host; a returned key is stored encrypted and never shown;
a replayed nonce, a cross-host submit, an autonomous text form and the
autonomous rate all refuse.
"""
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from core.wallet.signer import LocalEoaSigner
from tools.wallet_sign.store import WalletSignStore
from tools.wallet_sign.tool import (AUTONOMOUS_PER_ORIGIN_DAY, ApiCallParams, ChallengeParams,
                                    MessageParams, StatusParams, WalletSignTool)

KEY = bytes.fromhex("11" * 32)
MERCHANT_KEY = "mk_live_" + "Z9" * 16
HOST = "mob.example"


class _Wallet:
    def __init__(self):
        self.signer = LocalEoaSigner(KEY)

    def operational_signer(self):
        return self.signer


class _Fetch:
    """Records each request; answers the challenge and the enrollment."""

    def __init__(self, message=None, status=200):
        self.calls = []
        self.message = message
        self.status = status

    async def __call__(self, url, *, method="GET", json_body=None, extra_headers=None):
        self.calls.append(dict(url=url, method=method, body=json_body, headers=extra_headers))
        if url.endswith("/enrollment-challenges"):
            msg = self.message or (f"402Mob merchant enrollment\nWallet: {json_body['wallet']}\n"
                                   f"Nonce: 7c1e9a52")
            body = {"challengeId": "c-123", "message": msg}
        elif url.endswith("/enrollments"):
            body = {"merchant": {"id": "m1", "apiKey": MERCHANT_KEY}, "ok": True}
        else:
            body = {"echo": (extra_headers or {}).get("Authorization", ""), "offers": []}
        return SimpleNamespace(status=self.status, body=json.dumps(body).encode(),
                               content_type="application/json", final_url=url)


@pytest.fixture
def tool(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_ENCRYPTION_KEY", "kVf0iP5nA2bmh2V4Ue2JzvY6m7cRrj3m8Y5u4ePqP3Q=")  # gitleaks:allow (fake fixture)
    import core.security.encryption as enc
    monkeypatch.delitem(enc.__dict__, "_encryption_instance", raising=False)
    fetch = _Fetch()
    t = WalletSignTool(wallet=_Wallet(), store=WalletSignStore(str(tmp_path / "ws.db")),
                       fetch_fn=fetch)
    t.fetch = fetch
    return t


def _run(coro):
    return asyncio.run(coro)


def _enroll(**over):
    base = dict(challenge_url=f"https://{HOST}/v1/merchants/enrollment-challenges",
                challenge_body_json='{"wallet": "{address}", "name": "Rob"}',
                submit_url=f"https://{HOST}/v1/merchants/enrollments",
                submit_body_json=('{"wallet": "{address}", "name": "Rob", '
                                  '"challengeId": "{challenge.challengeId}", '
                                  '"signature": "{signature}"}'))
    base.update(over)
    return ChallengeParams(**base)


def test_enrollment_signs_submits_and_stores_the_key(tool):
    res = _run(tool.wallet_sign_challenge(_enroll()))
    assert res.error is None, res.error
    challenge, submit = tool.fetch.calls
    address = tool._wallet.signer.address
    assert challenge["body"] == {"wallet": address, "name": "Rob"}
    assert submit["body"]["challengeId"] == "c-123"
    sig = submit["body"]["signature"]
    msg = f"402Mob merchant enrollment\nWallet: {address}\nNonce: 7c1e9a52"
    assert Account.recover_message(encode_defunct(msg.encode()), signature=sig) == address
    assert MERCHANT_KEY not in res.extracted_content
    assert "Stored credential #1" in res.extracted_content
    assert tool._get_store().credential_secret(1)["secret"] == MERCHANT_KEY


def test_the_stored_key_is_used_only_on_its_own_host(tool):
    _run(tool.wallet_sign_challenge(_enroll()))
    ok = _run(tool.wallet_sign_api_call(ApiCallParams(url=f"https://{HOST}/v1/offers",
                                                      credential_id=1)))
    assert ok.error is None and MERCHANT_KEY not in ok.extracted_content
    assert tool.fetch.calls[-1]["headers"] == {"Authorization": "Bearer " + MERCHANT_KEY}
    bad = _run(tool.wallet_sign_api_call(ApiCallParams(url="https://evil.example/x",
                                                       credential_id=1)))
    assert bad.error and "never sent" in bad.error


def test_a_submit_to_another_host_refuses_before_any_request(tool):
    res = _run(tool.wallet_sign_challenge(_enroll(submit_url="https://evil.example/enroll")))
    assert res.error and "only to the site" in res.error
    assert tool.fetch.calls == []


@pytest.mark.parametrize("url", ["http://mob.example/c", "ftp://mob.example/c", "mob.example"])
def test_only_https(tool, url):
    res = _run(tool.wallet_sign_challenge(_enroll(challenge_url=url, submit_url=None)))
    assert res.error and "https" in res.error


def test_a_blind_challenge_is_not_signed(tool):
    tool.fetch.message = "0x" + "ab" * 32
    res = _run(tool.wallet_sign_challenge(_enroll()))
    assert res.error and "class D" in res.error
    assert len(tool.fetch.calls) == 1  # never submitted


def test_a_replayed_sign_in_nonce_refuses(tool):
    now = datetime.now(timezone.utc)
    iso = lambda s: datetime.fromtimestamp(now.timestamp() + s, tz=timezone.utc).isoformat()
    address = tool._wallet.signer.address
    tool.fetch.message = (f"{HOST} wants you to sign in with your Ethereum account:\n{address}\n\n"
                          f"URI: https://{HOST}/\nVersion: 1\nChain ID: 8453\nNonce: abcd1234XY\n"
                          f"Issued At: {iso(0)}\nExpiration Time: {iso(300)}")
    first = _run(tool.wallet_sign_challenge(_enroll(submit_url=None)))
    assert first.error is None, first.error
    tool._unsent.clear()  # the unsent-signature guard is tested on its own
    again = _run(tool.wallet_sign_challenge(_enroll(submit_url=None)))
    assert again.error and "signed already" in again.error


def test_the_autonomous_rate_per_site(tool, monkeypatch):
    monkeypatch.setattr(WalletSignTool, "_turn", lambda self, ctx: (None, True))
    for i in range(AUTONOMOUS_PER_ORIGIN_DAY):
        tool.fetch.message = f"{HOST} enrollment. Nonce n{i:08d}"
        assert _run(tool.wallet_sign_challenge(_enroll())).error is None
    tool.fetch.message = f"{HOST} enrollment. Nonce last0000"
    res = _run(tool.wallet_sign_challenge(_enroll()))
    assert res.error and "limit" in res.error


def test_the_autonomous_rate_across_sites(tool, monkeypatch):
    from tools.wallet_sign import tool as mod
    monkeypatch.setattr(mod, "AUTONOMOUS_ALL_ORIGINS_DAY", 3)
    monkeypatch.setattr(WalletSignTool, "_turn", lambda self, ctx: (None, True))
    for i in range(3):
        host = f"s{i}.example"
        tool.fetch.message = f"{host} enrollment. Nonce n{i:08d}"
        assert _run(tool.wallet_sign_challenge(_enroll(
            challenge_url=f"https://{host}/v1/merchants/enrollment-challenges",
            submit_url=f"https://{host}/v1/merchants/enrollments"))).error is None
    tool.fetch.message = "s9.example enrollment. Nonce zzzzzzzz"
    res = _run(tool.wallet_sign_challenge(_enroll(
        challenge_url="https://s9.example/v1/merchants/enrollment-challenges",
        submit_url="https://s9.example/v1/merchants/enrollments")))
    assert res.error and "across all sites" in res.error


def test_an_autonomous_run_never_gets_the_signature_back(tool, monkeypatch):
    monkeypatch.setattr(WalletSignTool, "_turn", lambda self, ctx: (None, True))
    tool.fetch.message = f"{HOST} enrollment. Nonce 12345678"
    res = _run(tool.wallet_sign_challenge(_enroll(submit_url=None)))
    assert res.error and "submit_url" in res.error and tool.fetch.calls == []


def test_an_autonomous_run_does_not_sign_a_challenge_that_does_not_name_its_site(tool, monkeypatch):
    monkeypatch.setattr(WalletSignTool, "_turn", lambda self, ctx: (None, True))
    res = _run(tool.wallet_sign_challenge(_enroll()))  # the default text names no host
    assert res.error and "owner turn" in res.error
    assert len(tool.fetch.calls) == 1


@pytest.mark.parametrize("message,cls", [
    ("Access zkSync account.\n\nOnly sign this message for a trusted client!", "D"),
    ("Welcome to OpenSea!\n\nClick to sign in and accept the OpenSea Terms of Service "
     "(https://opensea.io/tos).\n\nNonce:\nabc-12345", "C"),
    ("Sign to authorize mob.example to spend your USDC. Nonce 12345678", "C"),
])
def test_a_relayed_or_authority_challenge_is_not_signed(tool, message, cls):
    tool.fetch.message = message
    res = _run(tool.wallet_sign_challenge(_enroll()))
    assert res.error and f"class {cls}" in res.error, res.error
    assert len(tool.fetch.calls) == 1


def test_a_failed_signature_releases_its_reservation(tool, monkeypatch):
    from core.wallet import sign_policy
    def boom(*a, **k):
        raise RuntimeError("key unavailable")
    monkeypatch.setattr(sign_policy, "sign_classified", boom)
    res = _run(tool.wallet_sign_challenge(_enroll()))
    assert res.error
    assert tool._get_store().recent() == []


def test_a_non_json_submit_response_is_withheld(tool):
    async def fetch(url, **kw):
        if url.endswith("/enrollment-challenges"):
            return SimpleNamespace(status=200, body=json.dumps(
                {"challengeId": "c", "message": f"{HOST} login 12345678"}).encode())
        return SimpleNamespace(status=200, body=f"key={MERCHANT_KEY}".encode())
    tool._fetch_fn = fetch
    res = _run(tool.wallet_sign_challenge(_enroll()))
    assert res.error is None and MERCHANT_KEY not in res.extracted_content
    assert "withheld" in res.extracted_content


def test_a_session_token_is_captured(tool):
    async def fetch(url, **kw):
        if url.endswith("/enrollment-challenges"):
            return SimpleNamespace(status=200, body=json.dumps(
                {"challengeId": "c", "message": f"{HOST} login 12345678"}).encode())
        return SimpleNamespace(status=200, body=json.dumps({"session": "sess_abcdef123"}).encode())
    tool._fetch_fn = fetch
    res = _run(tool.wallet_sign_challenge(_enroll()))
    assert "sess_abcdef123" not in res.extracted_content and "Stored credential" in res.extracted_content


def test_a_failed_answer_stores_nothing_and_its_error_stays_readable(tool):
    async def fetch(url, **kw):
        if url.endswith("/enrollment-challenges"):
            return SimpleNamespace(status=200, body=json.dumps(
                {"challengeId": "c", "message": f"{HOST} login 12345678"}).encode())
        return SimpleNamespace(status=401, body=json.dumps(
            {"auth_error": "invalid signature for address",
             "session": "sess_abcdef123"}).encode())
    tool._fetch_fn = fetch
    res = _run(tool.wallet_sign_challenge(_enroll()))
    assert res.error and "invalid signature for address" in res.error
    assert "sess_abcdef123" not in res.error and "withheld" in res.error
    assert tool._get_store().credentials() == []


@pytest.mark.parametrize("header", ["Host", "Cookie", "Content-Length", "X-Forwarded-Host"])
def test_a_credential_rides_only_an_auth_header(header):
    with pytest.raises(Exception):
        ApiCallParams(url=f"https://{HOST}/x", credential_id=1, header=header)
    for ok in ("Authorization", "X-Api-Key", "X-Auth-Token", "Api-Key"):
        ApiCallParams(url=f"https://{HOST}/x", credential_id=1, header=ok)


def test_the_text_form_is_owner_only(tool, monkeypatch):
    monkeypatch.setattr(WalletSignTool, "_turn", lambda self, ctx: (None, True))
    res = _run(tool.wallet_sign_message(MessageParams(text=f"{HOST} login 12345678",
                                                      origin=HOST)))
    assert res.error and "autonomous run" in res.error


def test_the_text_form_signs_a_text_that_names_its_site(tool):
    res = _run(tool.wallet_sign_message(MessageParams(text=f"{HOST} login 12345678",
                                                      origin=HOST)))
    assert res.error is None and "signature: 0x" in res.extracted_content


def test_the_text_form_refuses_an_unbound_text(tool):
    res = _run(tool.wallet_sign_message(MessageParams(text="prove it 12345678", origin=HOST)))
    assert res.error and "class C" in res.error


def test_a_forged_turn_is_refused(tool, monkeypatch):
    from core.money import authorize
    monkeypatch.setattr(authorize, "authorize_spend",
                        lambda *a, **k: authorize.Verdict(authorize.REFUSED, "x", "turn"))
    res = _run(tool.wallet_sign_challenge(_enroll(), execution_context=object()))
    assert res.error and "self-wake" in res.error
    assert tool.fetch.calls == []


def test_status_lists_signatures_and_fingerprints_only(tool):
    _run(tool.wallet_sign_challenge(_enroll()))
    out = _run(tool.wallet_sign_status(StatusParams())).extracted_content
    assert HOST in out and "#1" in out and MERCHANT_KEY not in out


def test_the_tool_is_classified_and_gated():
    from core.tool_capabilities import TOOL_CAPABILITIES
    from core.verb_policy import policy_for
    assert {"money", "high_impact", "delegate_blocked"} <= set(TOOL_CAPABILITIES["wallet_sign"])
    for verb in ("wallet_sign_challenge", "wallet_sign_message", "wallet_sign_api_call",
                 "wallet_sign_status"):
        p = policy_for(verb)
        assert p.correspondent_blocked and p.room_denied, verb


def test_the_real_fetch_never_follows_a_redirect(monkeypatch):
    seen = {}

    async def fake_safe_fetch(url, **kw):
        seen.update(kw)
        return SimpleNamespace(status=200, body=b"{}")
    import tools.web_fetch.fetcher as fetcher
    monkeypatch.setattr(fetcher, "safe_fetch", fake_safe_fetch)
    t = WalletSignTool(wallet=_Wallet(), store=object())
    _run(t._post("https://mob.example/x", None, method="GET", headers={"Authorization": "k"}))
    assert seen["max_redirects"] == 0


def test_a_deep_parsed_json_body_is_accepted(tool):
    """Prod 2026-10-27 16:50Z: the tool-call layer deep-parses a JSON string argument
    into a dict; every wallet_sign_challenge call failed validation before reaching
    the site."""
    params = ChallengeParams(
        challenge_url=f"https://{HOST}/v1/merchants/enrollment-challenges",
        challenge_body_json={"wallet": "{address}", "name": "Rob"},
        submit_url=f"https://{HOST}/v1/merchants/enrollments",
        submit_body_json={"wallet": "{address}", "challengeId": "{challenge.challengeId}",
                          "signature": "{signature}"})
    assert isinstance(params.challenge_body_json, str)
    res = _run(tool.wallet_sign_challenge(params))
    assert res.error is None, res.error
    assert tool.fetch.calls[0]["body"] == {"wallet": tool._wallet.signer.address, "name": "Rob"}
    assert ApiCallParams(url=f"https://{HOST}/x", credential_id=1,
                         body_json={"a": 1}).body_json == '{"a": 1}'


# -- gaps closed after prod 2026-10-28 (14 signatures, none submitted) ---------------

def _named(tool):
    tool.fetch.message = f"{HOST} enrollment. Nonce 7c1e9a52"


def test_with_no_submit_url_the_result_says_not_sent_and_shows_the_challenge(tool):
    _named(tool)
    res = _run(tool.wallet_sign_challenge(_enroll(submit_url=None)))
    assert res.error is None, res.error
    out = res.extracted_content
    assert "NOT SENT" in out and "{challenge.challengeId}" in out
    assert '"challengeId": "c-123"' in out and "signature: 0x" in out
    assert len(tool.fetch.calls) == 1


def test_a_second_unsent_signature_for_the_same_site_refuses_before_any_request(tool):
    _named(tool)
    assert _run(tool.wallet_sign_challenge(_enroll(submit_url=None))).error is None
    again = _run(tool.wallet_sign_challenge(_enroll(
        submit_url=None, challenge_url=f"https://api.{HOST}/v1/merchants/enrollment-challenges")))
    assert again.error and "submit_url" in again.error and "sent nowhere" in again.error
    assert len(tool.fetch.calls) == 1
    # the one-call form still works, and clears the guard
    assert _run(tool.wallet_sign_challenge(_enroll())).error is None


def test_the_unsent_guard_expires(tool):
    _named(tool)
    clock = [1_000_000.0]
    tool._now_fn = lambda: clock[0]
    assert _run(tool.wallet_sign_challenge(_enroll(submit_url=None))).error is None
    clock[0] += 601
    tool.fetch.message = f"{HOST} enrollment. Nonce 8d2f0b63"
    assert _run(tool.wallet_sign_challenge(_enroll(submit_url=None))).error is None


class _SiteFetch:
    """A configurable site: per-path answers with optional Set-Cookie headers."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    async def __call__(self, url, *, method="GET", json_body=None, extra_headers=None):
        self.calls.append(dict(url=url, method=method, body=json_body, headers=extra_headers))
        for suffix, (status, body, cookies) in self.routes.items():
            if url.split("?")[0].endswith(suffix):
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                return SimpleNamespace(status=status, body=raw, set_cookies=cookies)
        return SimpleNamespace(status=404, body=b"{}", set_cookies=())


def test_a_get_challenge_with_the_address_in_the_url(tool):
    fetch = _SiteFetch({"/nonce": (200, {"message": f"{HOST} login. Nonce 1234abcd"}, ()),
                        "/verify": (200, {"ok": True}, ())})
    tool._fetch_fn = fetch
    res = _run(tool.wallet_sign_challenge(ChallengeParams(
        challenge_url=f"https://{HOST}/nonce?address={{address}}", challenge_method="GET",
        submit_url=f"https://{HOST}/verify",
        submit_body_json='{"sig": "{signature}", "who": "wallet {address}"}')))
    assert res.error is None, res.error
    address = tool._wallet.signer.address
    assert fetch.calls[0]["method"] == "GET" and fetch.calls[0]["body"] is None
    assert fetch.calls[0]["url"].endswith(f"address={address}")
    assert fetch.calls[1]["body"]["who"] == f"wallet {address}"   # a slot inside a string


def test_the_challenge_cookie_rides_to_the_submit_and_a_session_cookie_is_stored(tool):
    fetch = _SiteFetch({
        "/nonce": (200, {"message": f"{HOST} login. Nonce 1234abcd"},
                   ("nonce_sess=abc12345xyz; Path=/; HttpOnly",)),
        "/verify": (200, {"ok": True}, ("sid=SESSIONVALUE12345; Path=/; Secure",)),
        "/me": (200, {"me": 1}, ())})
    tool._fetch_fn = fetch
    res = _run(tool.wallet_sign_challenge(ChallengeParams(
        challenge_url=f"https://{HOST}/nonce", submit_url=f"https://{HOST}/verify",
        submit_body_json='{"sig": "{signature}"}')))
    assert res.error is None, res.error
    assert fetch.calls[1]["headers"] == {"Cookie": "nonce_sess=abc12345xyz"}
    assert "SESSIONVALUE12345" not in res.extracted_content
    assert "cookie_name='sid'" in res.extracted_content
    cred = tool._get_store().credentials()[0]
    assert cred["label"] == "cookie:sid"
    ok = _run(tool.wallet_sign_api_call(ApiCallParams(url=f"https://api.{HOST}/me",
                                                      credential_id=cred["id"],
                                                      cookie_name="sid")))
    assert ok.error is None, ok.error
    assert fetch.calls[-1]["headers"] == {"Cookie": "sid=SESSIONVALUE12345"}


def test_a_failed_submit_stores_no_cookie(tool):
    tool._fetch_fn = _SiteFetch({
        "/nonce": (200, {"message": f"{HOST} login. Nonce 1234abcd"}, ()),
        "/verify": (401, {"error": "bad"}, ("sid=SESSIONVALUE12345",))})
    res = _run(tool.wallet_sign_challenge(ChallengeParams(
        challenge_url=f"https://{HOST}/nonce", submit_url=f"https://{HOST}/verify")))
    assert res.error and tool._get_store().credentials() == []


def test_a_plain_text_challenge(tool):
    fetch = _SiteFetch({"/nonce": (200, f"{HOST} login. Nonce 1234abcd".encode(), ()),
                        "/verify": (200, {"ok": True}, ())})
    tool._fetch_fn = fetch
    bad = _run(tool.wallet_sign_challenge(ChallengeParams(
        challenge_url=f"https://{HOST}/nonce", submit_url=f"https://{HOST}/verify")))
    assert bad.error and "message_field=''" in bad.error
    res = _run(tool.wallet_sign_challenge(ChallengeParams(
        challenge_url=f"https://{HOST}/nonce", message_field="",
        submit_url=f"https://{HOST}/verify", submit_body_json='{"s": "{signature}"}')))
    assert res.error is None, res.error


SIWE_TEMPLATE = (f"{HOST} wants you to sign in with your Ethereum account:\n{{address}}\n\n"
                 f"URI: https://{HOST}\nVersion: 1\nChain ID: 8453\n"
                 f"Nonce: {{challenge.nonce}}\nIssued At: {{issued_at}}\n"
                 f"Expiration Time: {{expires_at}}")


def test_a_nonce_only_site_signs_a_siwe_message_built_from_a_template(tool):
    fetch = _SiteFetch({"/nonce": (200, {"nonce": "Nn12345678"}, ()),
                        "/verify": (200, {"ok": True}, ())})
    tool._fetch_fn = fetch
    res = _run(tool.wallet_sign_challenge(ChallengeParams(
        challenge_url=f"https://{HOST}/nonce", message_template=SIWE_TEMPLATE,
        submit_url=f"https://{HOST}/verify",
        submit_body_json='{"message": "{message}", "signature": "{signature}"}')))
    assert res.error is None, res.error
    sent = fetch.calls[1]["body"]
    assert "Nonce: Nn12345678" in sent["message"]
    address = tool._wallet.signer.address
    assert Account.recover_message(encode_defunct(sent["message"].encode()),
                                   signature=sent["signature"]) == address


@pytest.mark.parametrize("template", [
    f"{HOST} login. Nonce {{challenge.nonce}}",                     # not a sign-in message
    SIWE_TEMPLATE.replace(f"{HOST} wants", "bank.example wants"),  # another site
])
def test_a_template_that_is_not_a_sign_in_for_this_site_refuses(tool, template):
    fetch = _SiteFetch({"/nonce": (200, {"nonce": "Nn12345678"}, ())})
    tool._fetch_fn = fetch
    res = _run(tool.wallet_sign_challenge(ChallengeParams(
        challenge_url=f"https://{HOST}/nonce", message_template=template,
        submit_url=f"https://{HOST}/verify")))
    assert res.error and "not signed" in res.error.lower()
    assert len(fetch.calls) == 1 and tool._get_store().recent() == []


def test_a_sibling_submit_host_on_the_same_site_is_accepted(tool):
    _named(tool)
    res = _run(tool.wallet_sign_challenge(_enroll(
        submit_url=f"https://auth.{HOST}/v1/merchants/enrollments")))
    assert res.error is None, res.error
    assert tool.fetch.calls[1]["url"].startswith(f"https://auth.{HOST}/")


def test_a_filled_value_cannot_move_the_submit_to_another_site(tool):
    fetch = _SiteFetch({"/nonce": (200, {"message": f"{HOST} login. Nonce 1234abcd",
                                         "next": "evil.example"}, ())})
    tool._fetch_fn = fetch
    res = _run(tool.wallet_sign_challenge(ChallengeParams(
        challenge_url=f"https://{HOST}/nonce",
        submit_url=f"https://{HOST}/{{challenge.next}}/verify")))
    # quote() keeps the value inside the path: the host stays HOST
    assert all(HOST in c["url"].split("/")[2] for c in fetch.calls)
