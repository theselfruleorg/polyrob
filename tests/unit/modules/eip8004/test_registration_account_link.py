"""115 W6 (B2) — one agent, two entries: the instance's registration file names the identity a
held NFT's account owns, so the two entries on the registry link. The account's own card names
only itself (``tools.defi.agent_registration.account_agent_uri``)."""
import json

import pytest

from core.wallet import erc8004, held_identity
from modules.eip8004.registration import build_registration_file

REGISTRY = erc8004.resolve_identity_registry("robinhood")
ACCOUNT = "0x" + "ac" * 20


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    for k in ("EIP8004_ONCHAIN_ENABLED", "EIP8004_AGENT_ID", "EIP8004_IDENTITY_REGISTRY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYROB_INSTANCE_ID", "demo")
    return tmp_path


def _held(monkeypatch, rows=None, raises=False):
    def fake(*_a, **_k):
        if raises:
            raise ValueError("holdings file unreadable")
        return rows if rows is not None else [
            {"chain": "robinhood", "chain_id": 4663, "collection": "0x" + "c0" * 20, "token_id": 3,
             "account": ACCOUNT, "agent_id": 9, "registry": REGISTRY}]
    monkeypatch.setattr(held_identity, "account_identities", fake)


def test_the_instance_file_names_the_account_identity(monkeypatch):
    _held(monkeypatch)
    reg = build_registration_file("https://example.test")
    (entry,) = reg.registrations
    assert entry.agentId == 9 and entry.agentRegistry == f"eip155:4663:{REGISTRY}"
    assert entry.attestation == "nft-account"
    assert reg.trustMode == "local"          # the account's entry does not speak for the wallet


def test_with_a_verified_own_identity_both_entries_are_named(monkeypatch, _home):
    from core.instance import erc8004_record_path
    p = erc8004_record_path(_home, "demo")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"chain": "base", "chain_id": 8453, "agent_id": 42,
                             "registry": erc8004.resolve_identity_registry("base"),
                             "tx_hash": "0x" + "ab" * 32}))
    _held(monkeypatch)
    reg = build_registration_file("https://example.test")
    assert [(r.agentId, r.attestation) for r in reg.registrations] == [(42, "verified"),
                                                                       (9, "nft-account")]
    assert reg.trustMode == "onchain"


def test_an_unreadable_holdings_file_names_no_account_entry(monkeypatch):
    _held(monkeypatch, raises=True)
    reg = build_registration_file("https://example.test")
    assert reg.registrations == []


def test_the_account_card_names_only_its_own_identity(monkeypatch):
    import base64
    from tools.defi.agent_registration import account_agent_uri
    _held(monkeypatch)
    uri = account_agent_uri(4663, ACCOUNT)
    doc = json.loads(base64.b64decode(uri.split(",", 1)[1]))
    assert doc["registrations"] == []
    uri = account_agent_uri(4663, ACCOUNT, agent_id=9, registry=REGISTRY)
    doc = json.loads(base64.b64decode(uri.split(",", 1)[1]))
    assert doc["registrations"] == [{"agentId": 9, "agentRegistry": f"eip155:4663:{REGISTRY}"}]
    wallet = [s for s in doc["services"] if s["name"] == "agentWallet"]
    assert wallet == [{"name": "agentWallet", "endpoint": f"eip155:4663:{ACCOUNT}"}]


def test_a_five_digit_chain_id_is_not_read_as_a_credential(monkeypatch):
    """``46630:0x…`` (Robinhood testnet) matched the scrub's bot-token shape, so the account
    card and the instance file could not be built on a chain id of five digits or more."""
    from tools.defi.agent_registration import account_agent_uri, instance_agent_uri
    _held(monkeypatch, rows=[{"chain": "robinhood-testnet", "chain_id": 46630, "account": ACCOUNT,
                              "agent_id": 2, "registry": REGISTRY}])
    assert account_agent_uri(46630, ACCOUNT, agent_id=2, registry=REGISTRY).startswith("data:")
    assert instance_agent_uri().startswith("data:")


def test_a_credential_in_the_file_still_refuses(monkeypatch):
    from tools.defi import agent_registration as ar
    with pytest.raises(ValueError, match="credential material"):
        ar._refuse_credentials({"description": "token 123456789:" + "A" * 35,
                                "services": [{"endpoint": f"eip155:46630:{ACCOUNT}"}]})
