"""115 W6 (G3) — the agent's card published through an NFT account: ``account.execute(
identityRegistry, 0, setAgentURI(agentId, uri), 0)``, judged by the ONE guard.

* every selector and topic of ``core/wallet/identity_card.py`` computed from its signature;
* allowed: the account's own agentId (the collection's ``agentIdOf`` and the registry's
  ``ownerOf``), one ``URIUpdated(agentId, uri, account)`` and ``MetadataUpdate(agentId)`` from
  the pinned registry, nothing moved;
* refused: another agentId, an identity the account does not own, an unreadable identity, a
  missing or wrong ``URIUpdated``, a log from elsewhere, something moved, any other call to the
  registry through an account, value;
* the sale hold applies: an automatic publish through a READY account refuses.
"""
import pytest

from core.wallet import abi, collection_registry, erc6551, erc8004, identity_card as C, tx_guard
from core.wallet.simulation import Deltas
from tests.unit.core.wallet import test_tx_guard_polyrob_account as P
from tests.unit.core.wallet.test_tx_guard_via_account import (
    TOKEN_ID, TREASURY, _outer, _run, _via)

ACCOUNT = P.ACCOUNT
REGISTRY = erc8004.resolve_identity_registry("base")
AGENT_ID = 5
URI = "data:application/json;base64,eyJuYW1lIjoiYWdlbnQifQ=="
OTHER = "0x5555555555555555555555555555555555555555"


@pytest.fixture(autouse=True)
def _pins(monkeypatch):
    monkeypatch.setattr(erc6551, "CODE_SHA256", {erc6551.REGISTRY.lower(): P._H00,
                                                 erc6551.ACCOUNT_V3_IMPL.lower(): "f" * 64})
    monkeypatch.setattr(collection_registry, "profiles", lambda: (P._profile(),))


def _w(n):
    return "0x" + format(int(n), "064x")


class CardRpc(P.PolyrobRpc):
    """The ERC-6551P chain of the guard tests, plus the collection's identity views and the
    registry's ``ownerOf``."""

    def __init__(self, *, has=True, own_id=AGENT_ID, identity_owner=ACCOUNT, views_broken=False,
                 **kw):
        super().__init__(**kw)
        self.has, self.own_id, self.identity_owner = has, own_id, identity_owner
        self.views_broken = views_broken

    def __call__(self, method, params):
        if method == "eth_call":
            to, sel = params[0]["to"].lower(), params[0]["data"][:10]
            if sel in (abi.selector("hasIdentity(uint256)"), abi.selector("agentIdOf(uint256)")):
                self.reads.append(sel)
                if self.views_broken:
                    raise RuntimeError("rpc down")
                return _w(int(self.has) if sel == abi.selector("hasIdentity(uint256)") else self.own_id)
            if to == REGISTRY.lower() and sel == abi.selector("ownerOf(uint256)"):
                return P._word(int(self.identity_owner, 16))
        return super().__call__(method, params)


def _intent(**kw):
    base = dict(chain="base", token=None, to=REGISTRY, amount_raw=0, max_spend_usd=5.0,
                is_registration=True, expected_registry=REGISTRY, expects_mint=False,
                idempotency_key="k-card")
    base.update(kw)
    return _via(tx_guard.TxIntent(**base), account=ACCOUNT)


def _data(agent_id=AGENT_ID, uri=URI):
    return abi.encode_call("setAgentURI", [{"type": "uint256"}, {"type": "string"}],
                           [agent_id, uri])


def _tx(data=None, value=0, to=REGISTRY):
    return _outer({"to": to, "data": data or _data(), "value": value}, to=ACCOUNT)


def _uri_log(agent_id=AGENT_ID, by=ACCOUNT, uri=URI, emitter=REGISTRY):
    return {"address": emitter, "topics": [C.TOPIC_URI_UPDATED, _w(agent_id), C._word(by)],
            "data": "0x" + abi.encode([{"type": "string"}], [uri]).hex()}


def _meta_log(agent_id=AGENT_ID):
    return {"address": REGISTRY, "topics": [C.TOPIC_METADATA_UPDATE], "data": _w(agent_id)}


def _deltas(logs=None, **kw):
    logs = tuple(logs if logs is not None else (_meta_log(), _uri_log()))
    base = dict(ok=True, native_delta=0, gas_used=90_000, logs=logs,
                event_topics=tuple((lg["address"].lower(), lg["topics"][0]) for lg in logs))
    base.update(kw)
    return Deltas(**base)


# ------------------------------------------------------------------------------ the literals

@pytest.mark.parametrize("literal, signature", [
    (C.SET_AGENT_URI_SELECTOR, C.SET_AGENT_URI_SIGNATURE),
    (C.SET_METADATA_SELECTOR, "setMetadata(uint256,string,bytes)"),
    (C.TOKEN_URI_SELECTOR, "tokenURI(uint256)"),
    (C.REGISTER_SELECTORS[0], "register()"),
    (C.REGISTER_SELECTORS[1], "register(string)"),
    (C.REGISTER_SELECTORS[2], "register(string,(string,bytes)[])"),
])
def test_every_selector_is_its_signature(literal, signature):
    assert literal == abi.selector(signature)


@pytest.mark.parametrize("literal, signature", [
    (C.TOPIC_URI_UPDATED, "URIUpdated(uint256,string,address)"),
    (C.TOPIC_METADATA_UPDATE, "MetadataUpdate(uint256)"),
])
def test_every_topic_is_its_signature(literal, signature):
    from eth_utils import keccak
    assert literal == "0x" + keccak(text=signature).hex()


def test_the_calldata_decodes_only_in_its_canonical_form():
    assert C.decode_set_agent_uri(_data()) == (AGENT_ID, URI)
    with pytest.raises(ValueError):
        C.decode_set_agent_uri(_data() + "00" * 32)
    with pytest.raises(ValueError):
        C.decode_set_agent_uri("0x466648da" + _data()[10:])


# ------------------------------------------------------------------------------ allowed

def test_publishing_the_accounts_own_card_is_measured_and_allowed():
    rpc = CardRpc()
    d = _run(_intent(), _deltas(), _tx(), holder=TREASURY, rpc=rpc)
    assert d.allowed is True, d.reason
    assert abi.selector("agentIdOf(uint256)") in rpc.reads


def test_a_tokenbound_style_identity_without_the_collection_view_is_bound_by_owner_of():
    """The collection has no identity for this token (``hasIdentity`` false): the registry's
    ``ownerOf(agentId) == account`` alone binds it (an identity the account registered itself)."""
    d = _run(_intent(), _deltas(), _tx(), holder=TREASURY, rpc=CardRpc(has=False, own_id=0))
    assert d.allowed is True, d.reason


# ------------------------------------------------------------------------------ refused

@pytest.mark.parametrize("rpc, needle", [
    (CardRpc(own_id=6), "the account publishes only its own card"),
    (CardRpc(identity_owner=OTHER), "only its owner may publish its card"),
    (CardRpc(views_broken=True), "failing closed"),
])
def test_another_identity_or_an_unread_one_is_refused(rpc, needle):
    d = _run(_intent(), _deltas(), _tx(), holder=TREASURY, rpc=rpc)
    assert d.allowed is False and needle in d.reason, d.reason


@pytest.mark.parametrize("deltas, needle", [
    (_deltas(logs=(_meta_log(),)), "0 URIUpdated"),
    (_deltas(logs=(_uri_log(), _uri_log())), "2 URIUpdated"),
    (_deltas(logs=(_uri_log(agent_id=6),)), "another agentId, caller or URI"),
    (_deltas(logs=(_uri_log(by=TREASURY),)), "another agentId, caller or URI"),
    (_deltas(logs=(_uri_log(uri="https://elsewhere.example/a.json"),)), "another agentId, caller or URI"),
    (_deltas(logs=(_uri_log(), _meta_log(agent_id=6))), "MetadataUpdate names another agentId"),
    (_deltas(logs=(_uri_log(emitter=OTHER),)), "every log of it comes from"),
    (_deltas(logs=(_uri_log(), {"address": REGISTRY, "topics": ["0x" + "11" * 32], "data": "0x"})),
     "not part of it"),
    # a move or an approval is refused by the guard's earlier rules already (any shape)
    (_deltas(holder_transfers=(("0x" + "77" * 20, OTHER.lower(), 5),)), "refused:"),
    (_deltas(holder_nft_approvals=((REGISTRY.lower(), OTHER.lower(), AGENT_ID),)), "UNDECLARED Approval"),
])
def test_the_measured_result_refusals(deltas, needle):
    d = _run(_intent(), deltas, _tx(), holder=TREASURY, rpc=CardRpc())
    assert d.allowed is False and needle in d.reason, d.reason


def test_any_other_call_to_the_registry_through_an_account_is_refused():
    approve = abi.encode_call("approve", [{"type": "address"}, {"type": "uint256"}], [OTHER, AGENT_ID])
    d = _run(_intent(), _deltas(), _tx(data=approve), holder=TREASURY, rpc=CardRpc())
    assert d.allowed is False and "setAgentURI or setMetadata" in d.reason, d.reason
    move = abi.encode_call("transferFrom", [{"type": "address"}] * 2 + [{"type": "uint256"}],
                           [ACCOUNT, OTHER, AGENT_ID])
    plain = _via(tx_guard.TxIntent(chain="base", token=None, to=REGISTRY, amount_raw=0,
                                   max_spend_usd=5.0, is_nft_op=True, idempotency_key="k-mv"),
                 account=ACCOUNT)
    d = _run(plain, _deltas(logs=()), _tx(data=move), holder=TREASURY, rpc=CardRpc())
    assert d.allowed is False and "called only as a registration" in d.reason, d.reason


def test_an_update_cannot_wear_a_register_call_and_the_reverse():
    reg = abi.encode_call("register", [{"type": "string"}], [URI])
    d = _run(_intent(), _deltas(), _tx(data=reg), holder=TREASURY, rpc=CardRpc())
    assert d.allowed is False and "setAgentURI or setMetadata" in d.reason, d.reason
    d = _run(_intent(expects_mint=True), _deltas(), _tx(), holder=TREASURY, rpc=CardRpc())
    assert d.allowed is False and "register() overload" in d.reason, d.reason


def test_a_card_carries_no_value():
    d = _run(_intent(), _deltas(), _tx(value=1), holder=TREASURY, rpc=CardRpc())
    assert d.allowed is False and "no value" in d.reason, d.reason


def test_the_sale_hold_applies_to_an_automatic_publish():
    from core.wallet import sale_acts as A
    d = _run(_intent(), _deltas(), _tx(), holder=TREASURY, rpc=CardRpc(ready=True))
    assert d.allowed is False and A.HOLD_TEXT in d.reason, d.reason


def test_the_instance_wallets_own_set_agent_uri_is_unchanged():
    """No account: the instance's ``set_agent_uri`` keeps the update shape it had."""
    intent = tx_guard.TxIntent(chain="base", token=None, to=REGISTRY, amount_raw=0,
                               max_spend_usd=5.0, is_registration=True, expected_registry=REGISTRY,
                               expects_mint=False, idempotency_key="k-own")
    tx = {"to": REGISTRY, "data": _data(), "value": 0, "chainId": 8453, "nonce": 5,
          "gas": 200_000, "maxFeePerGas": 10 ** 8}
    d = _run(intent, _deltas(logs=()), tx, holder=TREASURY)
    assert d.allowed is True, d.reason
    assert C.runtime_refusal(intent, {"to": REGISTRY, "data": _data()}, None) is None


def test_the_token_uri_read_names_an_unreadable_card():
    with pytest.raises(ValueError):
        C.read_token_uri(lambda m, p: "0x", "base", AGENT_ID)
    raw = "0x" + abi.encode([{"type": "string"}], [URI]).hex()
    assert C.read_token_uri(lambda m, p: raw, "base", AGENT_ID) == URI
    assert TOKEN_ID == 1
