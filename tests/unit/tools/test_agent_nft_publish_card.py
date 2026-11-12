"""115 W6 (G3) — ``agent_nft_bind_identity`` publishes the agent's card on the identity the
account already owns: ``setAgentURI(agentId, card)`` THROUGH the account, declared as a
registration UPDATE (nothing minted), the card naming that agentId on that registry and the
account as its agentWallet. The same card already published = no transaction. An ERC-6551P
token's identity comes from the collection's ``agentIdOf`` (no log scan)."""
import base64
import json

from core.wallet import abi, erc6551, identity_card
from tests.unit.tools.test_agent_nft_core_verbs import (
    ACCOUNT, REGISTRY, _identity_chain, _no_package, _w, armed)  # noqa: F401 — the fixture
from tests.unit.tools.test_agent_nft_withdraw import FakeRail, _run, _tool
from tools.agent_nft.core_verbs import CARD_BATCH_NOTE
from tools.agent_nft.tool import BindParams


def _with_uri(rpc, uri=None, broken=False):
    def call(method, params, *a, **k):
        if (method == "eth_call" and params[0]["to"].lower() == REGISTRY.lower()
                and params[0]["data"].startswith(identity_card.TOKEN_URI_SELECTOR)):
            if broken:
                raise RuntimeError("rpc down")
            return "0x" + abi.encode([{"type": "string"}], [uri or "https://old.example/a.json"]).hex()
        return rpc(method, params, *a, **k)
    return call


def _card(data):
    agent_id, uri = identity_card.decode_set_agent_uri(data)
    return agent_id, json.loads(base64.b64decode(uri.split(",", 1)[1]))


def test_bind_publishes_the_card_on_the_identity_the_account_owns(armed):
    tool, seen = _tool(armed, _with_uri(_identity_chain(armed.address)))
    res = _run(_no_package(tool).agent_nft_bind_identity(BindParams(chain="robinhood", nft="3",
                                                                   dry_run=False)))
    assert res.error is None, res.error
    (intent, tx, _kw), = seen
    assert intent.is_registration and not intent.expects_mint and intent.via_account == ACCOUNT
    inner_to, value, data, op = erc6551.decode_execute(tx["data"])
    assert inner_to.lower() == REGISTRY.lower() and value == 0 and op == 0
    agent_id, doc = _card(data)
    assert agent_id == 3
    assert doc["registrations"] == [{"agentId": 3, "agentRegistry": f"eip155:4663:{REGISTRY}"}]
    assert {"name": "agentWallet", "endpoint": f"eip155:4663:{ACCOUNT}"} in doc["services"]
    out = res.extracted_content
    assert "PUBLISH CARD — setAgentURI(agentId 3) THROUGH the account" in out
    assert CARD_BATCH_NOTE.strip() in out and "card now: a different file" in out
    assert len(FakeRail.sent) == 1


def test_the_same_card_already_published_sends_nothing(armed):
    from tools.defi.agent_registration import account_agent_uri
    card = account_agent_uri(4663, ACCOUNT, agent_id=3, registry=REGISTRY)
    tool, seen = _tool(armed, _with_uri(_identity_chain(armed.address), uri=card))
    res = _run(_no_package(tool).agent_nft_bind_identity(BindParams(chain="robinhood", nft="3",
                                                                   dry_run=False)))
    assert "already publishes this card: no transaction" in res.extracted_content
    assert seen == [] and FakeRail.sent == []


def test_an_unreadable_current_card_is_named_and_the_guard_still_judges(armed):
    tool, seen = _tool(armed, _with_uri(_identity_chain(armed.address), broken=True))
    res = _run(_no_package(tool).agent_nft_bind_identity(BindParams(chain="robinhood", nft="3")))
    assert res.error is None and "card now: UNREADABLE" in res.extracted_content
    assert len(seen) == 1


def test_a_foreign_uri_is_refused_on_the_publish_branch_too(armed):
    tool, seen = _tool(armed, _with_uri(_identity_chain(armed.address)))
    res = _run(_no_package(tool).agent_nft_bind_identity(BindParams(
        chain="robinhood", nft="3", agent_uri="https://attacker.example/x.json")))
    assert "alternate agent_uri" in res.error and seen == []


def test_an_erc6551p_identity_comes_from_the_collection_view(monkeypatch):
    """The collection's ``agentIdOf`` names the identity; no registry log scan."""
    import types
    from core.wallet import collection_registry, erc8004_reputation
    from tools.agent_nft import core_verbs
    version = types.SimpleNamespace(kind=types.SimpleNamespace(collection_iface="0x6b7481e3"))
    monkeypatch.setattr(collection_registry, "profile_for", lambda *_a: types.SimpleNamespace(
        version_for_account=lambda *_b: version))
    monkeypatch.setattr(erc8004_reputation, "agent_id_of", lambda rpc, c, t: 12)
    held = types.SimpleNamespace(chain="robinhood", chain_id=4663, collection="0x" + "c0" * 20,
                                 token_id=1, account=ACCOUNT)

    def no_logs(method, params):
        raise AssertionError(f"no {method} expected")
    assert core_verbs._account_agent_id(no_logs, held) == 12
    assert _w(1)  # the helper module is shared
