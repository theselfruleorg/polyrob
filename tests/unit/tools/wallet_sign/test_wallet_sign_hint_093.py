"""093 WS-G — the 402Mob incident shape (prod 2026-10-28) as a regression case.

Rob called ``wallet_sign_challenge`` 12x with no ``submit_url`` and then told the owner
"my sign tool only signs — it never returns the challenge body". The tool description,
the NOT-SENT result and the refusal must each say that the tool itself fills the submit
body from the challenge, show the challenge's real keys and a filled example body, and
name the submit URL when the challenge response names one.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from tools.wallet_sign.store import WalletSignStore
from tools.wallet_sign.tool import (ChallengeParams, WalletSignTool, _example_submit_body,
                                    _submit_urls)

from tests.unit.tools.wallet_sign.test_wallet_sign_tool import _Wallet

HOST = "evidence.example"
EXAMPLE = '{"wallet":"{address}","challengeId":"{challenge.challengeId}","signature":"{signature}"}'


class _Mob:
    """The 402Mob shape: a challenge with ``challengeId`` + ``message`` (+ optional URL)."""

    def __init__(self, extra=None):
        self.calls, self.extra, self.n = [], extra or {}, 0

    async def __call__(self, url, *, method="GET", json_body=None, extra_headers=None):
        self.calls.append(dict(url=url, body=json_body))
        if url.endswith("/challenges"):
            self.n += 1
            body = dict({"challengeId": f"c-{self.n}",
                         "message": f"{HOST} merchant enrollment. Nonce 7c1e9a5{self.n}",
                         "expiresAt": "2026-10-28T13:00:00Z"}, **self.extra)
        else:
            body = {"ok": True}
        return SimpleNamespace(status=200, body=json.dumps(body).encode(), set_cookies=())


@pytest.fixture
def tool(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_ENCRYPTION_KEY", "kVf0iP5nA2bmh2V4Ue2JzvY6m7cRrj3m8Y5u4ePqP3Q=")  # gitleaks:allow (fake fixture)
    import core.security.encryption as enc
    monkeypatch.delitem(enc.__dict__, "_encryption_instance", raising=False)
    t = WalletSignTool(wallet=_Wallet(), store=WalletSignStore(str(tmp_path / "ws.db")),
                       fetch_fn=_Mob())
    return t


def _call(tool, **over):
    base = dict(challenge_url=f"https://{HOST}/v1/merchants/challenges",
                challenge_body_json='{"wallet": "{address}"}')
    base.update(over)
    return asyncio.run(tool.wallet_sign_challenge(ChallengeParams(**base)))


def test_the_description_says_the_tool_fills_the_body_from_the_challenge():
    desc = WalletSignTool.wallet_sign_challenge._description
    assert "The tool fills submit_body_json from the challenge it fetched" in desc
    assert "'{challenge.challengeId}' = that field of the challenge" in desc
    field = ChallengeParams.model_fields["submit_body_json"].description
    assert "{challenge.challengeId}" in field and "{signature}" in field


def test_the_not_sent_result_shows_the_keys_and_a_filled_example(tool):
    res = _call(tool)
    assert res.error is None, res.error
    out = res.extracted_content
    assert "NOT SENT" in out
    assert "top-level keys: challengeId, message, expiresAt" in out
    assert EXAMPLE in out
    assert "fills each '{challenge.<key>}' from the challenge it fetches" in out
    assert "names none" in out  # no submit URL in this challenge: say so, never guess


def test_the_refusal_after_an_unsent_signature_carries_the_same_hint(tool):
    assert _call(tool).error is None
    again = _call(tool)
    assert again.error and "sent nowhere" in again.error
    assert EXAMPLE in again.error and "challengeId, message, expiresAt" in again.error
    assert "fills each '{challenge.<key>}'" in again.error
    assert len(tool._fetch_fn.calls) == 1  # refused before any request


def test_a_submit_url_named_by_the_challenge_is_quoted(tool):
    tool._fetch_fn.extra = {"verifyUrl": "/v1/merchants/enrollments"}
    out = _call(tool).extracted_content
    assert f"submit_url: https://{HOST}/v1/merchants/enrollments (named in the challenge" in out


def test_the_example_body_fills_and_submits_in_one_call(tool):
    res = _call(tool, submit_url=f"https://{HOST}/v1/merchants/enrollments",
                submit_body_json=EXAMPLE)
    assert res.error is None, res.error
    submit = tool._fetch_fn.calls[-1]["body"]
    assert submit["challengeId"] == "c-1" and submit["signature"].startswith("0x")


def test_a_missing_challenge_field_names_the_real_keys(tool):
    res = _call(tool, submit_url=f"https://{HOST}/v1/merchants/enrollments",
                submit_body_json='{"id": "{challenge.id}", "signature": "{signature}"}')
    assert res.error and "Submitting it failed" in res.error
    assert EXAMPLE in res.error and "challengeId, message, expiresAt" in res.error


def test_the_autonomous_refusal_names_the_fill(tool, monkeypatch):
    monkeypatch.setattr(WalletSignTool, "_turn", lambda self, ctx: (None, True))
    res = _call(tool)
    assert res.error and "must give submit_url" in res.error
    assert "fills each '{challenge.<key>}'" in res.error


def test_helpers():
    assert _example_submit_body(["challengeId", "message", "expiresAt"], "message") == EXAMPLE
    assert _submit_urls({"a": {"submitUrl": "https://evil.example/x"}}, HOST) == []
    assert _submit_urls({"links": [f"https://api.{HOST}/verify"]}, HOST) == [
        f"https://api.{HOST}/verify"]
