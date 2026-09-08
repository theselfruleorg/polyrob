"""The settlement watcher must never log an RPC URL verbatim.

Found live on prod 2026-08-24: the "on-chain detection active" line printed
`https://base-mainnet.g.alchemy.com/v2/<API KEY>` into the journal, because a
provider RPC carries its credential IN THE URL PATH — so every name-keyed
secret scrubber (which masks `*_KEY`/`*_TOKEN`/`*_SECRET` by variable name)
sails straight past it. Log the endpoint's identity, never its path.
"""
from modules.x402.settlement_watcher import _redact_rpc


def test_strips_the_credential_bearing_path():
    out = _redact_rpc("https://base-mainnet.g.alchemy.com/v2/SUPERSECRETKEY123")
    assert "SUPERSECRETKEY123" not in out
    assert "base-mainnet.g.alchemy.com" in out


def test_keeps_a_pathless_public_endpoint_readable():
    assert _redact_rpc("https://mainnet.base.org") == "https://mainnet.base.org"
    assert _redact_rpc("https://sepolia.base.org") == "https://sepolia.base.org"


def test_strips_userinfo_and_query_credentials():
    out = _redact_rpc("https://user:pass@rpc.example.com/path?apikey=abc123")
    for secret in ("pass", "abc123", "apikey"):
        assert secret not in out
    assert "rpc.example.com" in out


def test_never_raises_on_junk():
    assert _redact_rpc("") == ""
    assert isinstance(_redact_rpc("not a url"), str)
