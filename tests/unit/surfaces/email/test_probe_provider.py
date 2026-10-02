"""OS13 — the email probe picks its transport with ``email_provider()``, the
one resolver, never a re-derivation that can disagree with it."""
import asyncio

import pytest

import surfaces.email.probe as probe_mod
from core.config_policy import email_provider


@pytest.mark.parametrize("env", [
    {"EMAIL_PROVIDER": "agentmial", "AGENTMAIL_API_KEY": "k"},   # a typo
    {"EMAIL_PROVIDER": "auto", "AGENTMAIL_API_KEY": "k"},
    {"AGENTMAIL_API_KEY": "k"},
    {"EMAIL_PROVIDER": "smtp", "AGENTMAIL_API_KEY": "k"},
])
def test_probe_follows_email_provider(monkeypatch, env):
    seen = []

    async def fake_http_json(method, url, **kw):
        seen.append(url)
        return 200, {}, ""

    monkeypatch.setattr(probe_mod, "http_json", fake_http_json)
    res = asyncio.run(probe_mod.probe(dict(env)))
    if email_provider(env) == "agentmail":
        assert seen and "agentmail" in seen[0]
    else:
        assert not seen
        assert res.state == "unavailable" and "GMAIL_EMAIL" in res.detail
