"""tx_guard's collection paid-mint shape: ``mint(address to, uint256 qty, uint256 minPnlOutPerToken)``
on a collection pinned in the owner's collection registry with the ``mint`` capability
(testnet-run 2026-09-29 F2; 050 §7.3).

⚠️ The one shape where native value LEAVES and NFTs ARRIVE. Before it existed ``agent_nft_collection_mint``
declared ``is_nft_op`` and the NFT branch refused the payment ("an NFT transfer moved
-42000000000000000 wei") on every chain.
⚠️ The destination is PINNED (``core.wallet.collection_registry``); the price is a pinned constant.
"""
import pytest

from core.wallet import collection_mint as M
from core.wallet import collection_registry, erc6551, tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas
from tests.collection_pins import code_rpc, SUPPLY, pin, profile

COLLECTION = "0xc011000000000000000000000000000000000001"
OTHER = "0x3333333333333333333333333333333333333333"
HOLDER = "0x2222222222222222222222222222222222222222"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ZERO = "0x" + "0" * 40
IDS = (5, 6, 7)
PRICE = 42 * 10 ** 15


@pytest.fixture(autouse=True)
def _pinned(monkeypatch):
    pin(monkeypatch, COLLECTION)


def _tx(to=COLLECTION, recipient=HOLDER, qty=3, value=None, data=None, min_out=0):
    return {"to": to, "data": data if data is not None else M.encode_mint(recipient, qty, min_out),
            "value": 3 * PRICE if value is None else value,
            "chainId": 4663, "nonce": 1, "gas": 3_000_000, "maxFeePerGas": 10 ** 8}


def _intent(**kw):
    base = dict(chain="robinhood", token=None, to=COLLECTION, amount_raw=3 * PRICE, max_spend_usd=600.0,
                idempotency_key="k", is_collection_mint=True, mint_ids=IDS)
    base.update(kw)
    return tx_guard.TxIntent(**base)


def _word(n):
    return "0x" + format(int(n), "064x")


def _addr_word(a):
    return "0x" + "0" * 24 + a.lower()[2:]


def _log(topic, *rest, address=COLLECTION):
    return {"address": address, "topics": [topic, *rest], "data": "0x"}


def _mint_logs(ids=IDS, to=HOLDER):
    out = []
    for i in ids:
        out += [_log(M.TOPIC_TRANSFER, _word(0), _addr_word(to), _word(i)),
                _log(M.TOPIC_MINTED, _word(i), _addr_word(to))]
    # the rest of a real mint: account creation, the PNL buy — none names the signer
    out.append(_log("0x" + "ab" * 32, _word(1), address=OTHER))
    return tuple(out)


def _deltas(ids=IDS, **kw):
    base = dict(ok=True, native_delta=-len(ids) * PRICE, token_deltas={}, allowance_deltas={},
                gas_used=1_000_000, logs=_mint_logs(ids),
                holder_nft_in=tuple((COLLECTION, "erc721", ZERO, i, 1) for i in ids))
    base.update(kw)
    return Deltas(**base)


def _authorize(intent, deltas, *, tx=None, price=4000.0, entry_paused=False, halted=False,
               gate=None, rpc=None):
    return tx_guard.authorize(
        intent, tx if tx is not None else _tx(),
        holder=HOLDER, gate=gate or PolicyGate(max_per_tx_usd=1000.0, daily_cap_usd=5000.0),
        execution_context=None, simulate_fn=lambda **_: deltas,
        price_fn=lambda chain, addr: price,
        rpc_is_pinned_fn=lambda chain: True, halted_fn=lambda: halted,
        entry_paused_fn=lambda: entry_paused, forged_fn=lambda ctx, tool: False,
        account_rpc=rpc or code_rpc())


# --- the happy path -----------------------------------------------------------

def test_a_paid_mint_of_three_is_authorized_and_costs_value_plus_fee():
    d = _authorize(_intent(), _deltas(), price=100.0)
    assert d.allowed is True, d.reason
    # Value plus the signed gas limit, even when simulation consumed less.
    assert d.amount_usd == pytest.approx((0.126 + 0.0003) * 100, abs=0.001)


def test_the_value_counts_against_the_autonomous_ceiling():
    # at $4000 the mint is ~$504: above the $25 default ceiling -> the owner queue, never
    # booked as a fee-only NFT op
    d = _authorize(_intent(), _deltas())
    assert d.allowed is False and d.lane == "owner_queue", d.reason
    assert d.amount_usd == pytest.approx((0.126 + 0.0003) * 4000, abs=0.001)


def test_a_mint_of_one_is_authorized():
    intent = _intent(mint_ids=(1,), amount_raw=PRICE)
    d = _authorize(intent, _deltas(ids=(1,)), tx=_tx(qty=1, value=PRICE), price=100.0)
    assert d.allowed is True, d.reason


def test_the_old_nft_op_shape_still_refuses_the_payment():
    # F2 as found: the NFT branch refuses native movement — unchanged for is_nft_op
    intent = tx_guard.TxIntent(chain="robinhood", token=None, to=COLLECTION, amount_raw=3 * PRICE,
                               max_spend_usd=600.0, idempotency_key="k", is_nft_op=True,
                               expected_nft_in=tuple((COLLECTION, "erc721", i, 1) for i in IDS))
    d = _authorize(intent, _deltas())
    assert d.allowed is False and "NFT transfer moved" in d.reason


def test_the_entry_pause_and_the_full_pause_hold_a_mint():
    d = _authorize(_intent(), _deltas(), entry_paused=True)
    assert d.allowed is False and "PAUSED" in d.reason
    d = _authorize(_intent(), _deltas(), halted=True)
    assert d.allowed is False and "paus" in d.reason.lower()


# --- the caps bound it ----------------------------------------------------------

def test_value_plus_fee_above_max_spend_refuses():
    d = _authorize(_intent(max_spend_usd=500.0), _deltas())
    assert d.allowed is False and "max_spend_usd" in d.reason


def test_the_policy_gate_per_tx_cap_bounds_a_mint():
    d = _authorize(_intent(), _deltas(), gate=PolicyGate(max_per_tx_usd=100.0, daily_cap_usd=5000.0))
    assert d.allowed is False and "PolicyGate" in d.reason


def test_an_unpriceable_native_asset_refuses():
    d = _authorize(_intent(), _deltas(), price=None)
    assert d.allowed is False and "mint price" in d.reason


# --- the destination is pinned ------------------------------------------------

def test_no_pinned_collection_refuses(monkeypatch):
    pin(monkeypatch)
    d = _authorize(_intent(), _deltas())
    assert d.allowed is False and "no collection with the mint capability is pinned" in d.reason


def test_a_collection_pinned_on_another_chain_refuses(monkeypatch):
    pin(monkeypatch, profile(COLLECTION, chain_id=46630))
    d = _authorize(_intent(), _deltas())
    assert d.allowed is False and "pinned" in d.reason


def test_a_collection_pinned_without_the_mint_capability_refuses(monkeypatch):
    pin(monkeypatch, profile(COLLECTION, capabilities=("reveal",)))
    d = _authorize(_intent(), _deltas())
    assert d.allowed is False and "mint capability" in d.reason


def test_an_untrusted_registry_refuses(monkeypatch):
    def boom():
        raise collection_registry.CollectionRegistryError("the file is writable")
    monkeypatch.setattr(collection_registry, "profiles", boom)
    d = _authorize(_intent(), _deltas())
    assert d.allowed is False and "cannot be trusted" in d.reason


def test_the_ids_are_held_to_the_profiles_max_supply(monkeypatch):
    pin(monkeypatch, profile(COLLECTION, max_supply=6))
    d = _authorize(_intent(), _deltas())
    assert d.allowed is False and "outside 1..6" in d.reason


def test_an_intent_to_an_unpinned_contract_refuses():
    d = _authorize(_intent(to=OTHER), _deltas(), tx=_tx(to=OTHER))
    assert d.allowed is False and "not a pinned collection" in d.reason


def test_a_transaction_to_another_contract_refuses():
    d = _authorize(_intent(), _deltas(), tx=_tx(to=OTHER))
    assert d.allowed is False and "TRANSACTION is addressed" in d.reason


# --- calldata, qty, value -----------------------------------------------------

def test_a_mint_to_a_third_party_refuses():
    d = _authorize(_intent(), _deltas(), tx=_tx(recipient=OTHER))
    assert d.allowed is False and "not to the signing treasury" in d.reason


def test_another_selector_refuses():
    data = "0x42842e0e" + "0" * 192
    d = _authorize(_intent(), _deltas(), tx=_tx(data=data))
    assert d.allowed is False and "not mint(address,uint256,uint256)" in d.reason


def test_trailing_bytes_refuse():
    d = _authorize(_intent(), _deltas(), tx=_tx(data=M.encode_mint(HOLDER, 3) + "00" * 32))
    assert d.allowed is False and "canonical" in d.reason


def test_calldata_qty_other_than_the_declared_ids_refuses():
    d = _authorize(_intent(), _deltas(), tx=_tx(qty=2, value=2 * PRICE))
    assert d.allowed is False and "declares 3 ids" in d.reason


@pytest.mark.parametrize("qty", [0, 11])
def test_qty_outside_1_to_10_in_calldata_refuses(qty):
    ids = tuple(range(1, 11)) if qty == 11 else IDS
    intent = _intent(mint_ids=ids, amount_raw=len(ids) * PRICE)
    d = _authorize(intent, _deltas(ids=ids), tx=_tx(qty=qty, value=qty * PRICE))
    assert d.allowed is False and "BadQty" in d.reason


@pytest.mark.parametrize("ids", [(), tuple(range(1, 12)), (5, 7), (7, 6), (5, 5), (0, 1),
                                 (SUPPLY, SUPPLY + 1)])
def test_bad_id_declarations_refuse(ids):
    n = max(len(ids), 1)
    intent = _intent(mint_ids=ids, amount_raw=n * PRICE)
    d = _authorize(intent, _deltas(ids=ids), tx=_tx(qty=n, value=n * PRICE))
    assert d.allowed is False


@pytest.mark.parametrize("value", [3 * PRICE - 1, 3 * PRICE + 1, 0, PRICE])
def test_a_value_other_than_qty_times_price_refuses(value):
    d = _authorize(_intent(), _deltas(), tx=_tx(value=value))
    assert d.allowed is False and "exactly" in d.reason


def test_a_declared_amount_other_than_the_price_refuses():
    d = _authorize(_intent(amount_raw=3 * PRICE - 1), _deltas())
    assert d.allowed is False and "amount_raw" in d.reason


def test_mint_ids_without_the_shape_refuse():
    d = _authorize(_intent(is_collection_mint=False), _deltas())
    assert d.allowed is False and "without is_collection_mint" in d.reason


@pytest.mark.parametrize("extra", [
    dict(is_nft_op=True, expected_nft_in=((COLLECTION, "erc721", 5, 1),)),
    dict(expected_nft_in=((COLLECTION, "erc721", 5, 1),)),
    dict(is_registration=True, expected_registry=COLLECTION),
    dict(is_claim=True, min_native_inflow_wei=1),
    dict(is_collection_reveal=True, reveal_ids=(1,)),
    dict(token=USDC),
    dict(expected_allowance_grants=((USDC, OTHER, 1),)),
    dict(inflow_token=USDC, min_inflow_raw=1),
    dict(nft_out=((COLLECTION, "erc721", 1, 1),)),
])
def test_another_shape_or_declaration_alongside_refuses(extra):
    d = _authorize(_intent(**extra), _deltas())
    assert d.allowed is False


def test_via_a_token_bound_account_refuses():
    d = _authorize(_intent(via_account=OTHER, via_account_state=0), _deltas())
    assert d.allowed is False


# --- the simulation -----------------------------------------------------------

@pytest.mark.parametrize("native", [-3 * PRICE - 10 ** 15, -3 * PRICE + 10 ** 15, 0, 10 ** 15])
def test_a_native_movement_other_than_the_price_refuses(native):
    d = _authorize(_intent(), _deltas(native_delta=native))
    assert d.allowed is False and ("wei" in d.reason)


def test_native_dust_is_tolerated():
    d = _authorize(_intent(), _deltas(native_delta=-3 * PRICE - 10 ** 11), price=100.0)
    assert d.allowed is True, d.reason


@pytest.mark.parametrize("field,value", [
    ("holder_transfers", ((USDC, OTHER, 5),)),
    ("holder_approvals", ((USDC, OTHER, 5),)),
    ("holder_nft_out", ((COLLECTION, "erc721", OTHER, 3, 1),)),
    ("holder_operator_grants", ((COLLECTION, OTHER, True),)),
    ("holder_nft_approvals", ((COLLECTION, OTHER, 5),)),
    ("holder_permit2_grants", ((erc6551.PERMIT2, USDC, OTHER, 5),)),
    ("token_deltas", {USDC: -5}),
])
def test_any_other_movement_or_approval_refuses(field, value):
    d = _authorize(_intent(), _deltas(**{field: value}))
    assert d.allowed is False


def test_fewer_ids_than_declared_arrive_refuses():
    d = _authorize(_intent(), _deltas(holder_nft_in=tuple(
        (COLLECTION, "erc721", ZERO, i, 1) for i in (5, 6))))
    assert d.allowed is False and "exactly ids" in d.reason


def test_other_ids_arrive_refuses():
    d = _authorize(_intent(), _deltas(holder_nft_in=tuple(
        (COLLECTION, "erc721", ZERO, i, 1) for i in (6, 7, 8))))
    assert d.allowed is False


def test_an_extra_nft_from_another_contract_refuses():
    extra = tuple((COLLECTION, "erc721", ZERO, i, 1) for i in IDS) + ((OTHER, "erc721", ZERO, 1, 1),)
    d = _authorize(_intent(), _deltas(holder_nft_in=extra))
    assert d.allowed is False


def test_an_existing_token_transferred_in_is_not_a_mint():
    d = _authorize(_intent(), _deltas(holder_nft_in=tuple(
        (COLLECTION, "erc721", OTHER, i, 1) for i in IDS)))
    assert d.allowed is False


def test_a_log_from_another_contract_naming_the_signer_refuses():
    logs = _mint_logs() + (_log("0x" + "cd" * 32, _addr_word(HOLDER), address=OTHER),)
    d = _authorize(_intent(), _deltas(logs=logs))
    assert d.allowed is False and "names the signer" in d.reason


def test_an_unrelated_collection_event_naming_the_signer_refuses():
    logs = _mint_logs() + (_log("0x" + "cd" * 32, _addr_word(HOLDER)),)
    d = _authorize(_intent(), _deltas(logs=logs))
    assert d.allowed is False and "not part of a mint" in d.reason


def test_a_missing_minted_event_refuses():
    logs = tuple(l for l in _mint_logs() if not (l["topics"][0] == M.TOPIC_MINTED
                                                  and l["topics"][1] == _word(7)))
    d = _authorize(_intent(), _deltas(logs=logs))
    assert d.allowed is False and "Minted" in d.reason


def test_a_missing_transfer_log_refuses():
    logs = tuple(l for l in _mint_logs() if not (l["topics"][0] == M.TOPIC_TRANSFER
                                                  and l["topics"][3:] == [_word(7)]))
    d = _authorize(_intent(), _deltas(logs=logs))
    assert d.allowed is False and "Transfer" in d.reason


def test_the_codec_round_trips_and_pins_the_selector_and_price():
    assert M.MINT_SELECTOR == "0x156e29f6"          # the collection ABI's methodIdentifiers
    to, qty, min_out = M.decode_mint(M.encode_mint(HOLDER, 3, 7))
    assert to.lower() == HOLDER and (qty, min_out) == (3, 7)
    assert M.MINT_PRICE_WEI == PRICE and M.MAX_MINT_QTY == 10


def test_a_changed_collection_runtime_refuses():
    """069 v4 A3: the pinned runtime_sha256 is re-checked against the live code."""
    d = _authorize(_intent(), _deltas(), rpc=code_rpc(code="0x6080deadbeef"))
    assert d.allowed is False and "not the pinned" in d.reason, d.reason
    d = _authorize(_intent(), _deltas(), rpc=code_rpc(code="0x"))
    assert d.allowed is False and "could not be read" in d.reason, d.reason
