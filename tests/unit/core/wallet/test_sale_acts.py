"""115 W7 — the seller's on-chain sale acts of an ERC-6551P collection and the SALE HOLD.

* ``core/wallet/sale_acts.py``: every selector and topic computed from its signature, the exact
  calldata, the ERC-6551 clone test the collection uses for ``SellerIsAccount``;
* the guard (``core/wallet/tx_guard_account.py`` through ``tx_guard.authorize``): ``ready`` is
  ``readyToSell(ownId)`` by the account (refused for another id, for a treasury that is itself
  an ERC-6551 account, without ``via_account``, for any other declaration); the measured result
  is ONE ``SaleReady(id, treasury, state)``, ``saleReadyOf`` true after and nothing moved;
  ``unready`` is ``cancelReady`` from the treasury with ``saleReadyOf`` false after; the market
  acts emit their event and move the salt / nonce;
* the hold: an automatic act through a READY account refuses, an unreadable readiness refuses
  an automatic act, an act the owner asked for passes with the note that readiness ends.
"""
import pytest

from core.wallet import abi, collection_registry, erc6551, sale_acts as A, tx_guard
from core.wallet import tx_guard_account
from core.wallet.simulation import Deltas
from tests.unit.core.wallet import test_tx_guard_polyrob_account as P
from tests.unit.core.wallet.test_tx_guard_via_account import (
    COLLECTION, STATE, TOKEN_ID, TREASURY, _erc20_deltas, _erc20_inner, _erc20_intent, _outer,
    _run, _via)

ACCOUNT = P.ACCOUNT
MARKET = "0x" + "b1" * 20
OTHER = "0x5555555555555555555555555555555555555555"


@pytest.fixture(autouse=True)
def _pins(monkeypatch):
    monkeypatch.setattr(erc6551, "CODE_SHA256", {erc6551.REGISTRY.lower(): P._H00,
                                                 erc6551.ACCOUNT_V3_IMPL.lower(): "f" * 64})
    monkeypatch.setattr(collection_registry, "profiles", lambda: (P._profile(),))


class SaleRpc(P.PolyrobRpc):
    """The ERC-6551P chain of the guard tests, plus ``ownerOf`` and the treasury's code."""

    def __init__(self, *, nft_owner=TREASURY, treasury_code="0x", **kw):
        super().__init__(**kw)
        self.nft_owner, self.treasury_code = nft_owner, treasury_code

    def __call__(self, method, params):
        if method == "eth_getCode" and params[0].lower() == TREASURY.lower():
            return self.treasury_code
        if method == "eth_call" and params[0]["data"][:10] == abi.selector("ownerOf(uint256)"):
            return P._word(int(self.nft_owner, 16))
        return super().__call__(method, params)


def _w(n):
    return "0x" + format(int(n), "064x")


# ------------------------------------------------------------------------------ the literals

@pytest.mark.parametrize("literal, signature", [
    (A.READY_SELECTOR, "readyToSell(uint256)"),
    (A.CANCEL_READY_SELECTOR, "cancelReady(uint256)"),
    (A.SALE_READY_OF_SELECTOR, "saleReadyOf(uint256)"),
    (A.MARKET_CANCEL_SELECTOR, "cancel(uint256)"),
    (A.INCREMENT_NONCE_SELECTOR, "incrementNonce()"),
    (A.NONCES_SELECTOR, "nonces(address)"),
    (A.USED_SALT_SELECTOR, "usedSalt(address,uint256)"),
])
def test_every_selector_is_its_signature(literal, signature):
    assert literal == abi.selector(signature)


def test_the_ready_selector_is_the_one_the_account_lets_last():
    # polyrob-desk contracts/src/account/PolyrobAccount.sol:63 READY_TO_SELL = 0x4b8f6ae4
    assert A.READY_SELECTOR == "0x4b8f6ae4"


@pytest.mark.parametrize("literal, signature", [
    (A.TOPIC_SALE_READY, "SaleReady(uint256,address,uint256)"),
    (A.TOPIC_SALT_CANCELLED, "SaltCancelled(address,uint256)"),
    (A.TOPIC_NONCE_INCREMENTED, "NonceIncremented(address,uint256)"),
    (A.TOPIC_SOLD, "Sold(address,uint256,address,address,address,uint256,uint256)"),
])
def test_every_topic_is_its_signature(literal, signature):
    from eth_utils import keccak
    assert literal == "0x" + keccak(text=signature).hex()


def test_the_calls_are_exact_and_anything_else_is_named():
    assert A.collection_act(A.encode_ready(7)) == (A.KIND_READY, 7, None)
    assert A.collection_act(A.encode_cancel_ready(7)) == (A.KIND_UNREADY, 7, None)
    assert A.collection_act("0x095ea7b3" + "00" * 64) == (None, None, None)
    kind, _arg, why = A.collection_act(A.encode_ready(7) + "00")
    assert kind is None and "canonical" in why
    assert A.market_act(A.encode_increment_nonce()) == (A.KIND_CANCEL_ALL, None, None)
    assert A.market_act(A.encode_market_cancel(9)) == (A.KIND_CANCEL_SALT, 9, None)
    assert "only to cancel" in A.market_act("0x12345678")[2]


def test_the_clone_test_is_the_collections():
    clone = "0x363d3d373d3d3d363d73" + "ab" * 20 + "5af43d82803e903d91602b57fd5bf3" + "00" * 128
    assert A.is_erc6551_clone(clone)
    assert not A.is_erc6551_clone(clone + "00")          # 174 bytes
    assert not A.is_erc6551_clone("0xef0100" + "ab" * 20)  # an EIP-7702 delegation
    assert not A.is_erc6551_clone("0x")


def test_an_unreadable_or_odd_readiness_raises():
    with pytest.raises(Exception):
        A.read_sale_ready(lambda m, p: "0x", COLLECTION, 1)
    with pytest.raises(ValueError, match="not a bool"):
        A.read_sale_ready(lambda m, p: _w(2), COLLECTION, 1)
    assert A.read_sale_ready(lambda m, p: _w(1), COLLECTION, 1) is True


# ------------------------------------------------------------------------------ the sale hold

def _act(rpc):
    return _run(_via(_erc20_intent(), account=ACCOUNT), _erc20_deltas(),
                _outer(_erc20_inner(), to=ACCOUNT), holder=TREASURY, rpc=rpc)


def test_an_automatic_act_through_a_ready_account_is_held():
    d = _act(P.PolyrobRpc(ready=True))
    assert d.allowed is False and A.HOLD_TEXT in d.reason, d.reason


def test_an_unreadable_readiness_holds_an_automatic_act():
    d = _act(P.PolyrobRpc(ready=None))
    assert d.allowed is False and "could not be read" in d.reason and "on hold" in d.reason


def test_an_account_that_is_not_ready_is_not_held():
    d = _act(P.PolyrobRpc(ready=False))
    assert d.allowed is True and "NOTE" not in d.reason, d.reason


def test_the_owners_act_through_a_ready_account_passes_with_the_note(monkeypatch):
    monkeypatch.setattr(tx_guard, "owner_authority", lambda *a, **k: (True, False))
    d = _act(P.PolyrobRpc(ready=True))
    assert d.allowed is True and d.lane == "owner_direct", d.reason
    assert f"NOTE: {A.HOLD_NOTE}" in d.reason


def test_an_owner_approved_card_is_the_owner_asking(monkeypatch):
    monkeypatch.setattr(tx_guard, "owner_authority", lambda *a, **k: (False, True))
    d = _act(P.PolyrobRpc(ready=None))
    assert d.allowed is True and A.HOLD_UNREAD_NOTE in d.reason, d.reason


def test_a_tokenbound_account_has_no_readiness_and_no_hold():
    """AccountV3 kind: ``sale_ready_view`` None — no read at all."""
    notes = []
    rpc = P.PolyrobRpc(ready=True)
    assert tx_guard_account._sale_hold(rpc, P._profile(), erc6551_kind_v3(), ACCOUNT,
                                       owner_asked=False, sale_act=None, notes=notes) is None
    assert notes == [] and abi.selector("saleReadyOf(uint256)") not in rpc.reads


def erc6551_kind_v3():
    from core.wallet.account_kinds import TOKENBOUND_V3
    return TOKENBOUND_V3


def test_the_ready_act_itself_is_not_held():
    notes = []
    from core.wallet.account_kinds import ERC6551P
    assert tx_guard_account._sale_hold(P.PolyrobRpc(ready=True), P._profile(), ERC6551P, ACCOUNT,
                                       owner_asked=False, sale_act=(A.KIND_READY, 1, COLLECTION),
                                       notes=notes) is None


# ------------------------------------------------------------------------------ ready

def _ready_intent(**kw):
    base = dict(chain="base", token=None, to=COLLECTION, amount_raw=0, max_spend_usd=5.0,
                is_nft_op=True, idempotency_key="k-ready")
    base.update(kw)
    return _via(tx_guard.TxIntent(**base), account=ACCOUNT)


def _ready_tx(token_id=TOKEN_ID):
    return _outer({"to": COLLECTION, "data": A.encode_ready(token_id)}, to=ACCOUNT)


def _view(target, data, before, after):
    key = (target.lower(), data.lower())
    return {"views_before": {key: before}, "views_after": {key: after}}


def _ready_deltas(*, token_id=TOKEN_ID, owner=TREASURY, after=1, extra_logs=(), **kw):
    logs = ({"address": COLLECTION, "topics": [A.TOPIC_SALE_READY, _w(token_id),
                                                A.topic_word(owner)], "data": _w(STATE + 1)},
            *extra_logs)
    base = dict(ok=True, native_delta=0, gas_used=60_000, logs=logs,
                event_topics=tuple((lg["address"].lower(), lg["topics"][0]) for lg in logs),
                sender_moved=("SaleReady names the treasury",),
                **_view(COLLECTION, A.encode_sale_ready_of(TOKEN_ID), 0, after))
    base.update(kw)
    return Deltas(**base)


def test_ready_is_measured_and_allowed():
    calls = []
    d = _run(_ready_intent(), _ready_deltas(), _ready_tx(), holder=TREASURY, rpc=SaleRpc(),
             sim_calls=calls)
    assert d.allowed is True, d.reason
    (kw,) = calls
    assert kw["views"] == ((COLLECTION.lower(), A.encode_sale_ready_of(TOKEN_ID)),)
    assert kw["sender"] == TREASURY and kw["holder"] == ACCOUNT


@pytest.mark.parametrize("deltas, needle", [
    (_ready_deltas(after=0), "does not make the NFT ready"),
    (_ready_deltas(token_id=2), "another id or owner"),
    (_ready_deltas(owner=OTHER), "another id or owner"),
    (_ready_deltas(holder_transfers=(("0x" + "77" * 20, OTHER.lower(), 5),)), "moves nothing"),
    (_ready_deltas(holder_approvals=(("0x" + "77" * 20, OTHER.lower(), 5),)), "moves nothing"),
    (_ready_deltas(extra_logs=({"address": "0x" + "99" * 20, "topics": ["0x" + "11" * 32],
                                "data": "0x"},)), "every log of it comes from"),
    (_ready_deltas(extra_logs=({"address": COLLECTION, "topics": [
        A.TOPIC_SALE_READY, _w(TOKEN_ID), A.topic_word(TREASURY)], "data": "0x"},)), "exactly one"),
    (_ready_deltas(views_before={}, views_after={}), "did not measure"),
])
def test_ready_measurement_refusals(deltas, needle):
    d = _run(_ready_intent(), deltas, _ready_tx(), holder=TREASURY, rpc=SaleRpc())
    assert d.allowed is False and needle in d.reason, d.reason


def test_ready_for_another_token_than_the_accounts_own_is_refused():
    d = _run(_ready_intent(), _ready_deltas(), _ready_tx(token_id=2), holder=TREASURY, rpc=SaleRpc())
    assert d.allowed is False and "OWN token" in d.reason, d.reason


def test_a_treasury_that_is_an_erc6551_account_cannot_sell():
    clone = "0x363d3d373d3d3d363d73" + "ab" * 20 + "5af43d82803e903d91602b57fd5bf3" + "00" * 128
    d = _run(_ready_intent(), _ready_deltas(), _ready_tx(), holder=TREASURY,
             rpc=SaleRpc(treasury_code=clone))
    assert d.allowed is False and "SellerIsAccount" in d.reason, d.reason


@pytest.mark.parametrize("intent_kw, needle", [
    (dict(is_nft_op=False), "amount must be greater than zero"),   # step 3, before the gate
    (dict(nft_out=((COLLECTION, "erc721", 1, 1),)), "nft_out"),
    (dict(to="0x" + "12" * 20), "addressed to"),
])
def test_ready_declared_otherwise_is_refused(intent_kw, needle):
    d = _run(_ready_intent(**intent_kw), _ready_deltas(), _ready_tx(), holder=TREASURY,
             rpc=SaleRpc())
    assert d.allowed is False and needle in d.reason, d.reason


def test_a_generic_account_call_cannot_make_an_nft_ready():
    """The ERC-20 send shape addressed to the collection with readyToSell calldata."""
    intent = _via(_erc20_intent(to=COLLECTION), account=ACCOUNT)
    d = _run(intent, _erc20_deltas(), _ready_tx(), holder=TREASURY, rpc=SaleRpc())
    assert d.allowed is False and "sale act" in d.reason, d.reason


def test_ready_without_the_account_is_refused():
    intent = tx_guard.TxIntent(chain="base", token=None, to=COLLECTION, amount_raw=0,
                               max_spend_usd=5.0, is_nft_op=True, idempotency_key="k")
    tx = {"to": COLLECTION, "data": A.encode_ready(1), "value": 0, "chainId": 8453, "nonce": 1,
          "gas": 100_000, "maxFeePerGas": 10 ** 8}
    d = _run(intent, _ready_deltas(), tx, holder=TREASURY, rpc=SaleRpc())
    assert d.allowed is False and "NotTokenAccount" in d.reason, d.reason


# ------------------------------------------------------------------------------ unready

def _direct(target, data):
    return {"to": target, "data": data, "value": 0, "chainId": 8453, "nonce": 1, "gas": 100_000,
            "maxFeePerGas": 10 ** 8}


def _unready_intent(**kw):
    base = dict(chain="base", token=None, to=COLLECTION, amount_raw=0, max_spend_usd=5.0,
                is_nft_op=True, idempotency_key="k-unready")
    base.update(kw)
    return tx_guard.TxIntent(**base)


def _unready_deltas(after=0, logs=()):
    return Deltas(ok=True, native_delta=0, gas_used=30_000, logs=tuple(logs),
                  **_view(COLLECTION, A.encode_sale_ready_of(TOKEN_ID), 1, after))


def test_unready_from_the_treasury_is_allowed():
    trait = {"address": COLLECTION, "topics": ["0x" + "83" * 32, "0x" + "00" * 32, _w(TOKEN_ID)],
             "data": "0x"}   # the collection's own readiness event (ERC-7496), naming no address
    d = _run(_unready_intent(), _unready_deltas(logs=(trait,)),
             _direct(COLLECTION, A.encode_cancel_ready(TOKEN_ID)), holder=TREASURY, rpc=SaleRpc())
    assert d.allowed is True, d.reason


@pytest.mark.parametrize("deltas, rpc, needle", [
    (_unready_deltas(after=1), SaleRpc(), "readiness did not end"),
    (_unready_deltas(), SaleRpc(nft_owner=OTHER), "only the owner"),
    (_unready_deltas(logs=({"address": COLLECTION, "topics": [A.TOPIC_SALE_READY, _w(1),
                                                              A.topic_word(TREASURY)],
                            "data": "0x"},)), SaleRpc(), "not part of it"),
])
def test_unready_refusals(deltas, rpc, needle):
    d = _run(_unready_intent(), deltas, _direct(COLLECTION, A.encode_cancel_ready(TOKEN_ID)),
             holder=TREASURY, rpc=rpc)
    assert d.allowed is False and needle in d.reason, d.reason


def test_unready_through_the_account_is_refused():
    intent = _via(_unready_intent(), account=ACCOUNT)
    tx = _outer({"to": COLLECTION, "data": A.encode_cancel_ready(TOKEN_ID)}, to=ACCOUNT)
    d = _run(intent, _unready_deltas(), tx, holder=TREASURY, rpc=SaleRpc())
    assert d.allowed is False and "sent by the treasury itself" in d.reason, d.reason


# ------------------------------------------------------------------------------ the market

def _market_deltas(kind, *, before, after, signer=TREASURY, value=None):
    if kind == A.KIND_CANCEL_ALL:
        topic, data, view = A.TOPIC_NONCE_INCREMENTED, A.encode_nonces(TREASURY), None
        value = after if value is None else value
    else:
        topic, data = A.TOPIC_SALT_CANCELLED, A.encode_used_salt(TREASURY, 9)
        value = 9 if value is None else value
    log = {"address": MARKET, "topics": [topic, A.topic_word(signer)], "data": _w(value)}
    return Deltas(ok=True, native_delta=0, gas_used=30_000, logs=(log,),
                  **_view(MARKET, data, before, after))


def _market_intent():
    return tx_guard.TxIntent(chain="base", token=None, to=MARKET, amount_raw=0, max_spend_usd=5.0,
                             is_nft_op=True, idempotency_key="k-market")


@pytest.mark.parametrize("data, deltas", [
    (A.encode_increment_nonce(), _market_deltas(A.KIND_CANCEL_ALL, before=3, after=4)),
    (A.encode_market_cancel(9), _market_deltas(A.KIND_CANCEL_SALT, before=0, after=1)),
])
def test_a_market_cancel_is_allowed(data, deltas):
    d = _run(_market_intent(), deltas, _direct(MARKET, data), holder=TREASURY, rpc=SaleRpc())
    assert d.allowed is True, d.reason


@pytest.mark.parametrize("data, deltas, needle", [
    (A.encode_increment_nonce(), _market_deltas(A.KIND_CANCEL_ALL, before=3, after=5), "exactly one"),
    (A.encode_increment_nonce(), _market_deltas(A.KIND_CANCEL_ALL, before=3, after=4, signer=OTHER),
     "another signer"),
    (A.encode_market_cancel(9), _market_deltas(A.KIND_CANCEL_SALT, before=0, after=1, value=8),
     "another salt"),
    (A.encode_market_cancel(9), _market_deltas(A.KIND_CANCEL_SALT, before=0, after=0),
     "exactly this salt"),
    ("0x12345678", _market_deltas(A.KIND_CANCEL_ALL, before=0, after=1), "only to cancel"),
])
def test_market_refusals(data, deltas, needle):
    d = _run(_market_intent(), deltas, _direct(MARKET, data), holder=TREASURY, rpc=SaleRpc())
    assert d.allowed is False and needle in d.reason, d.reason


def test_a_changed_market_code_refuses(monkeypatch):
    rpc = SaleRpc()
    real = rpc.__call__

    def changed(method, params):
        if method == "eth_getCode" and params[0].lower() == MARKET:
            return "0x6080"
        return real(method, params)
    d = _run(_market_intent(), _market_deltas(A.KIND_CANCEL_ALL, before=3, after=4),
             _direct(MARKET, A.encode_increment_nonce()), holder=TREASURY, rpc=changed)
    assert d.allowed is False and "pinned market" in d.reason, d.reason
