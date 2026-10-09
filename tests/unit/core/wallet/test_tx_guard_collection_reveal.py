"""tx_guard's collection-reveal shape: ``reveal(uint256[] ids)`` on a collection pinned in the
owner's collection registry with the ``reveal`` capability.

⚠️ The RECEIPT is the shape (the registration precedent): a reveal moves nothing, so without
the event assertion the guard could not tell "revealed 3 ids" from "a call that burned gas".
⚠️ The destination is PINNED (``core.wallet.collection_registry``), never declared by the caller.
"""
import pytest

from core.wallet import collection_reveal as R
from core.wallet import collection_registry, erc6551, tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas
from tests.collection_pins import code_rpc, SUPPLY, pin, profile

COLLECTION = "0xc011000000000000000000000000000000000001"
OTHER = "0x3333333333333333333333333333333333333333"
HOLDER = "0x2222222222222222222222222222222222222222"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
IDS = (3, 4, 7)


@pytest.fixture(autouse=True)
def _pinned(monkeypatch):
    pin(monkeypatch, COLLECTION)


def _tx(to=COLLECTION, ids=IDS, value=0, data=None):
    return {"to": to, "data": data if data is not None else R.encode_reveal(ids), "value": value,
            "chainId": 4663, "nonce": 1, "gas": 400_000, "maxFeePerGas": 10 ** 8}


def _intent(**kw):
    base = dict(chain="robinhood", token=None, to=COLLECTION, amount_raw=0, max_spend_usd=0.25,
                idempotency_key="k", is_collection_reveal=True, reveal_ids=IDS)
    base.update(kw)
    return tx_guard.TxIntent(**base)


def _word(n):
    return "0x" + format(int(n), "064x")


def _log(topic, *rest, address=COLLECTION):
    return {"address": address, "topics": [topic, *rest], "data": "0x"}


def _revealed_logs(ids=IDS, recommit=()):
    out = []
    for i in ids:
        if i in recommit:
            out.append(_log(R.TOPIC_RECOMMITTED, _word(i)))
        else:
            out += [_log(R.TOPIC_REVEALED, _word(i)), _log(R.TOPIC_METADATA_UPDATE),
                    _log(R.TOPIC_TRAIT_UPDATED, _word(0xabc))]
    return tuple(out)


def _deltas(**kw):
    base = dict(ok=True, native_delta=0, token_deltas={}, allowance_deltas={},
                gas_used=150_000, logs=_revealed_logs())
    base.update(kw)
    return Deltas(**base)


def _authorize(intent, deltas, *, tx=None, price=4000.0, entry_paused=False, halted=False,
               rpc=None):
    return tx_guard.authorize(
        intent, tx if tx is not None else _tx(ids=intent.reveal_ids or IDS),
        holder=HOLDER, gate=PolicyGate(max_per_tx_usd=100.0, daily_cap_usd=1000.0),
        execution_context=None, simulate_fn=lambda **_: deltas,
        price_fn=lambda chain, addr: price,
        rpc_is_pinned_fn=lambda chain: True, halted_fn=lambda: halted,
        entry_paused_fn=lambda: entry_paused, forged_fn=lambda ctx, tool: False,
        account_rpc=rpc or code_rpc())


# --- the happy path -----------------------------------------------------------

def test_a_reveal_of_due_ids_is_authorized_and_costs_its_fee():
    d = _authorize(_intent(), _deltas())
    assert d.allowed is True, d.reason
    # The supplied 400k gas limit exceeds the simulation-based 225k estimate.
    assert d.amount_usd == pytest.approx(0.16)
    assert "amount must be greater than zero" not in d.reason


def test_a_recommit_is_a_valid_receipt_too():
    d = _authorize(_intent(), _deltas(logs=_revealed_logs(recommit=(4,))))
    assert d.allowed is True, d.reason


def test_the_entry_pause_does_not_hold_a_reveal_but_the_full_pause_does():
    assert _authorize(_intent(), _deltas(), entry_paused=True).allowed is True
    d = _authorize(_intent(), _deltas(), halted=True)
    assert d.allowed is False and "paus" in d.reason.lower()


def test_a_fee_above_max_spend_refuses():
    d = _authorize(_intent(max_spend_usd=0.01), _deltas())
    assert d.allowed is False and "max_spend_usd" in d.reason


# --- the destination is pinned ------------------------------------------------

def test_no_pinned_collection_refuses(monkeypatch):
    pin(monkeypatch)
    d = _authorize(_intent(), _deltas())
    assert d.allowed is False and "no collection with the reveal capability is pinned" in d.reason


def test_a_collection_pinned_without_the_reveal_capability_refuses(monkeypatch):
    pin(monkeypatch, profile(COLLECTION, capabilities=("mint",)))
    d = _authorize(_intent(), _deltas())
    assert d.allowed is False and "reveal capability" in d.reason


def test_an_untrusted_registry_refuses(monkeypatch):
    def boom():
        raise collection_registry.CollectionRegistryError("unknown spec")
    monkeypatch.setattr(collection_registry, "profiles", boom)
    d = _authorize(_intent(), _deltas())
    assert d.allowed is False and "cannot be trusted" in d.reason


def test_the_ids_are_held_to_the_profiles_max_supply(monkeypatch):
    pin(monkeypatch, profile(COLLECTION, max_supply=5))
    d = _authorize(_intent(), _deltas())
    assert d.allowed is False and "outside 1..5" in d.reason


def test_an_intent_to_an_unpinned_contract_refuses():
    d = _authorize(_intent(to=OTHER), _deltas(), tx=_tx(to=OTHER))
    assert d.allowed is False and "not a pinned collection" in d.reason


def test_a_transaction_to_another_contract_refuses():
    d = _authorize(_intent(), _deltas(), tx=_tx(to=OTHER))
    assert d.allowed is False and "TRANSACTION is addressed" in d.reason


def test_a_collection_pinned_on_another_chain_refuses(monkeypatch):
    pin(monkeypatch, profile(COLLECTION, chain_id=46630))
    d = _authorize(_intent(), _deltas())
    assert d.allowed is False and "pinned" in d.reason


# --- value and calldata -------------------------------------------------------

def test_a_reveal_with_value_refuses():
    d = _authorize(_intent(), _deltas(), tx=_tx(value=1))
    assert d.allowed is False and "carries no value" in d.reason


def test_a_declared_amount_refuses():
    d = _authorize(_intent(amount_raw=1), _deltas())
    assert d.allowed is False and "amount_raw" in d.reason


def test_another_selector_refuses():
    data = "0x42842e0e" + "0" * 192        # safeTransferFrom(address,address,uint256)
    d = _authorize(_intent(), _deltas(), tx=_tx(data=data))
    assert d.allowed is False and "not reveal(uint256[])" in d.reason


def test_trailing_bytes_after_the_ids_refuse():
    d = _authorize(_intent(), _deltas(), tx=_tx(data=R.encode_reveal(IDS) + "00" * 32))
    assert d.allowed is False and "canonical" in d.reason


def test_calldata_ids_that_differ_from_the_declared_ids_refuse():
    d = _authorize(_intent(), _deltas(), tx=_tx(ids=(3, 4, 8)))
    assert d.allowed is False and "declares" in d.reason


@pytest.mark.parametrize("ids", [(), (4, 3), (3, 3), (0,), (SUPPLY + 1,),
                                 tuple(range(1, R.MAX_REVEAL_IDS + 2))])
def test_bad_id_lists_refuse(ids):
    d = _authorize(_intent(reveal_ids=ids), _deltas(), tx=_tx(ids=ids))
    assert d.allowed is False


def test_reveal_ids_without_the_shape_refuse():
    assert _authorize(_intent(is_collection_reveal=False), _deltas()).allowed is False
    d = _authorize(_intent(is_collection_reveal=False, is_nft_op=True,
                           expected_nft_in=((COLLECTION, "erc721", 3, 1),)), _deltas())
    assert d.allowed is False and "without is_collection_reveal" in d.reason


@pytest.mark.parametrize("extra", [dict(is_nft_op=True, expected_nft_in=((COLLECTION, "erc721", 3, 1),)),
                                   dict(is_registration=True, expected_registry=COLLECTION),
                                   dict(is_claim=True, min_native_inflow_wei=1),
                                   dict(token=USDC),
                                   dict(expected_allowance_grants=((USDC, OTHER, 1),)),
                                   dict(inflow_token=USDC, min_inflow_raw=1)])
def test_another_shape_or_declaration_alongside_refuses(extra):
    d = _authorize(_intent(**extra), _deltas())
    assert d.allowed is False


def test_via_a_token_bound_account_refuses():
    d = _authorize(_intent(via_account=OTHER, via_account_state=0), _deltas())
    assert d.allowed is False


# --- nothing of the signer's may move -----------------------------------------

def test_native_movement_refuses():
    d = _authorize(_intent(), _deltas(native_delta=-10 ** 15))
    assert d.allowed is False and "native" in d.reason


@pytest.mark.parametrize("field,value", [
    ("holder_transfers", ((USDC, OTHER, 5),)),
    ("holder_approvals", ((USDC, OTHER, 5),)),
    ("holder_nft_out", ((COLLECTION, "erc721", OTHER, 3, 1),)),
    ("holder_nft_in", ((COLLECTION, "erc721", OTHER, 3, 1),)),
    ("holder_operator_grants", ((COLLECTION, OTHER, True),)),
    ("holder_nft_approvals", ((COLLECTION, OTHER, 3),)),
    ("holder_permit2_grants", ((erc6551.PERMIT2, USDC, OTHER, 5),)),
    ("token_deltas", {USDC: -5}),
])
def test_any_asset_or_approval_movement_refuses(field, value):
    d = _authorize(_intent(), _deltas(**{field: value}))
    assert d.allowed is False


def test_a_log_naming_the_signer_refuses():
    logs = _revealed_logs() + (_log(R.TOPIC_METADATA_UPDATE, "0x" + "0" * 24 + HOLDER[2:]),)
    d = _authorize(_intent(), _deltas(logs=logs))
    assert d.allowed is False and "names the signer" in d.reason


def test_a_log_from_another_contract_refuses():
    logs = _revealed_logs() + (_log(R.TOPIC_METADATA_UPDATE, address=OTHER),)
    d = _authorize(_intent(), _deltas(logs=logs))
    assert d.allowed is False and "emit" in d.reason


def test_an_unrelated_event_from_the_collection_refuses():
    transfer = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    logs = _revealed_logs() + (_log(transfer, _word(1), _word(2), _word(3)),)
    d = _authorize(_intent(), _deltas(logs=logs))
    assert d.allowed is False and "not part of a reveal" in d.reason


# --- the receipt --------------------------------------------------------------

def test_no_reveal_event_refuses():
    d = _authorize(_intent(), _deltas(logs=()))
    assert d.allowed is False and "no Revealed or Recommitted" in d.reason


def test_a_declared_id_that_is_not_due_refuses():
    d = _authorize(_intent(), _deltas(logs=_revealed_logs(ids=(3, 4))))
    assert d.allowed is False and "every declared id" in d.reason


def test_an_undeclared_id_that_moves_refuses():
    d = _authorize(_intent(), _deltas(logs=_revealed_logs(ids=(3, 4, 7, 9))))
    assert d.allowed is False


def test_the_event_must_come_from_the_collection():
    logs = tuple(dict(l, address=OTHER) for l in _revealed_logs())
    d = _authorize(_intent(), _deltas(logs=logs))
    assert d.allowed is False


def test_the_codec_round_trips_and_pins_the_selector():
    assert R.REVEAL_SELECTOR == "0xb93f208a"        # the collection ABI's methodIdentifiers
    assert R.decode_reveal(R.encode_reveal([1, 2, 6551])) == [1, 2, 6551]


def test_a_changed_collection_runtime_refuses():
    """069 v4 A3: the pinned runtime_sha256 is re-checked against the live code."""
    d = _authorize(_intent(), _deltas(), rpc=code_rpc(code="0x6080deadbeef"))
    assert d.allowed is False and "not the pinned" in d.reason, d.reason
    d = _authorize(_intent(), _deltas(), rpc=code_rpc(code="0x"))
    assert d.allowed is False and "could not be read" in d.reason, d.reason
