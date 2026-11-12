"""tx_guard's collection paid-mint shape: ``mint(address to, uint256 qty, uint256 minPnlOutPerToken)``
or ``mintWL(…, allowance, proof)`` on the pinned SALE of a collection pinned in the owner's
collection registry with the ``mint`` capability (testnet-run 2026-09-29 F2; 050 §7.3; 115 W8).

⚠️ The one shape where native value LEAVES and NFTs ARRIVE. Before it existed ``agent_nft_collection_mint``
declared ``is_nft_op`` and the NFT branch refused the payment ("an NFT transfer moved
-42000000000000000 wei") on every chain.
⚠️ The destination is the PINNED sale (``core.wallet.collection_registry``), never the collection;
the base price is a pinned constant, the public cap the profile's ``sale.max_price_wei``.
⚠️ The receipt per id is ``Transfer(0x0 → collection)`` then ``Transfer(collection → signer)``
(polyrob-desk ``AgentIdentity._mintWithAccountAndIdentity``): the ids arrive FROM the collection.
"""
import pytest

from core.wallet import collection_mint as M
from core.wallet import collection_registry, erc6551, tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas
from tests.collection_pins import MAX_PRICE_WEI, SUPPLY, code_rpc, pin, profile, sale, sale_address

COLLECTION = "0xc011000000000000000000000000000000000001"
SALE = sale_address(COLLECTION)
OTHER = "0x3333333333333333333333333333333333333333"
HOLDER = "0x2222222222222222222222222222222222222222"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ZERO = "0x" + "0" * 40
IDS = (5, 6, 7)
PRICE = 33 * 10 ** 15


@pytest.fixture(autouse=True)
def _pinned(monkeypatch):
    pin(monkeypatch, COLLECTION)


def _tx(to=SALE, recipient=HOLDER, qty=3, value=None, data=None, min_out=0):
    return {"to": to, "data": data if data is not None else M.encode_mint(recipient, qty, min_out),
            "value": 3 * PRICE if value is None else value,
            "chainId": 4663, "nonce": 1, "gas": 3_000_000, "maxFeePerGas": 10 ** 8}


def _intent(**kw):
    base = dict(chain="robinhood", token=None, to=SALE, amount_raw=3 * PRICE, max_spend_usd=600.0,
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
    """The real receipt: per id the collection mints to ITSELF, then moves the token to `to`."""
    out = []
    for i in ids:
        out += [_log(M.TOPIC_TRANSFER, _word(0), _addr_word(COLLECTION), _word(i)),
                _log(M.TOPIC_TRANSFER, _addr_word(COLLECTION), _addr_word(to), _word(i)),
                _log(M.TOPIC_MINTED, _word(i), _addr_word(to))]
    # the rest of a real mint: account creation, the TKN buy — none names the signer
    out.append(_log("0x" + "ab" * 32, _word(1), address=OTHER))
    return tuple(out)


def _deltas(ids=IDS, **kw):
    base = dict(ok=True, native_delta=-len(ids) * PRICE, token_deltas={}, allowance_deltas={},
                gas_used=1_000_000, logs=_mint_logs(ids),
                holder_nft_in=tuple((COLLECTION, "erc721", COLLECTION, i, 1) for i in ids))
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
    assert d.amount_usd == pytest.approx((0.099 + 0.0003) * 100, abs=0.001)


def test_the_value_counts_against_the_autonomous_ceiling():
    # at $4000 the mint is ~$397: above the $25 default ceiling -> the owner queue, never
    # booked as a fee-only NFT op
    d = _authorize(_intent(), _deltas())
    assert d.allowed is False and d.lane == "owner_queue", d.reason
    assert d.amount_usd == pytest.approx((0.099 + 0.0003) * 4000, abs=0.001)


def test_a_mint_of_one_is_authorized():
    intent = _intent(mint_ids=(1,), amount_raw=PRICE)
    d = _authorize(intent, _deltas(ids=(1,)), tx=_tx(qty=1, value=PRICE), price=100.0)
    assert d.allowed is True, d.reason


def test_the_old_nft_op_shape_still_refuses_the_payment():
    # F2 as found: the NFT branch refuses native movement — unchanged for is_nft_op
    intent = tx_guard.TxIntent(chain="robinhood", token=None, to=COLLECTION, amount_raw=3 * PRICE,
                               max_spend_usd=600.0, idempotency_key="k", is_nft_op=True,
                               expected_nft_in=tuple((COLLECTION, "erc721", i, 1) for i in IDS))
    d = _authorize(intent, _deltas(), tx=_tx(to=COLLECTION))
    assert d.allowed is False and "NFT transfer moved" in d.reason


def test_the_entry_pause_and_the_full_pause_hold_a_mint():
    d = _authorize(_intent(), _deltas(), entry_paused=True)
    assert d.allowed is False and "PAUSED" in d.reason
    d = _authorize(_intent(), _deltas(), halted=True)
    assert d.allowed is False and "paus" in d.reason.lower()


# --- the caps bound it ----------------------------------------------------------

def test_value_plus_fee_above_max_spend_refuses():
    d = _authorize(_intent(max_spend_usd=250.0), _deltas())
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
    assert d.allowed is False and "not the pinned sale" in d.reason


def test_a_mint_addressed_to_the_collection_itself_refuses():
    """M1: the collection has no public mint (mintFromSale accepts only its SALE())."""
    d = _authorize(_intent(to=COLLECTION), _deltas(), tx=_tx(to=COLLECTION))
    assert d.allowed is False and "no public mint" in d.reason and SALE in d.reason


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
    assert d.allowed is False and "neither mint(address,uint256,uint256)" in d.reason


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


@pytest.mark.parametrize("value", [3 * PRICE - 1, 1, PRICE, 3 * MAX_PRICE_WEI + 1])
def test_a_public_value_outside_base_and_the_pinned_cap_refuses(value):
    d = _authorize(_intent(amount_raw=value), _deltas(), tx=_tx(value=value))
    assert d.allowed is False and "at least" in d.reason and "max_price_wei" in d.reason


@pytest.mark.parametrize("value", [3 * PRICE, 3 * PRICE + 1, 3 * 66 * 10 ** 15, 3 * MAX_PRICE_WEI])
def test_a_public_value_from_base_to_the_pinned_cap_is_authorized(value):
    """M3: the auction takes qty × priceNow() .. qty × START_PRICE; the guard bounds it by the
    base and the profile's cap, and the sale keeps all of it inside the transaction."""
    d = _authorize(_intent(amount_raw=value), _deltas(native_delta=-value), tx=_tx(value=value),
                   price=50.0)
    assert d.allowed is True, d.reason


def test_the_public_cap_is_the_profiles_max_price(monkeypatch):
    pin(monkeypatch, profile(COLLECTION, sale_pin=sale(COLLECTION, max_price_wei=PRICE)))
    d = _authorize(_intent(amount_raw=3 * PRICE + 1), _deltas(native_delta=-3 * PRICE - 1),
                   tx=_tx(value=3 * PRICE + 1))
    assert d.allowed is False and "max_price_wei" in d.reason
    d = _authorize(_intent(), _deltas(), price=100.0)
    assert d.allowed is True, d.reason


def test_a_declared_amount_other_than_the_value_refuses():
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
        (COLLECTION, "erc721", COLLECTION, i, 1) for i in (5, 6))))
    assert d.allowed is False and "exactly ids" in d.reason


def test_other_ids_arrive_refuses():
    d = _authorize(_intent(), _deltas(holder_nft_in=tuple(
        (COLLECTION, "erc721", COLLECTION, i, 1) for i in (6, 7, 8))))
    assert d.allowed is False


def test_an_extra_nft_from_another_contract_refuses():
    extra = tuple((COLLECTION, "erc721", COLLECTION, i, 1) for i in IDS) + ((OTHER, "erc721", ZERO, 1, 1),)
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
    assert M.MINT_SELECTOR == "0x156e29f6"          # the sale ABI's methodIdentifiers
    assert M.MINT_WL_SELECTOR == "0x1e29517f"
    to, qty, min_out = M.decode_mint(M.encode_mint(HOLDER, 3, 7))
    assert to.lower() == HOLDER and (qty, min_out) == (3, 7)
    assert M.BASE_WEI == PRICE and M.MAX_MINT_QTY == 10 and M.MAX_PROOF_WORDS == 32


def test_a_changed_collection_runtime_refuses():
    """069 v4 A3: the pinned runtime_sha256 is re-checked against the live code."""
    d = _authorize(_intent(), _deltas(), rpc=code_rpc(code="0x6080deadbeef"))
    assert d.allowed is False and "not the pinned" in d.reason, d.reason
    d = _authorize(_intent(), _deltas(), rpc=code_rpc(code="0x"))
    assert d.allowed is False and "could not be read" in d.reason, d.reason


def _code_at(address, code):
    """A fake RPC whose eth_getCode of *address* is *code* (every other read: code_rpc)."""
    def inner(method, params):
        if method == "eth_getCode" and str(params[0]).lower() == address.lower():
            return code
        return NotImplemented
    return code_rpc(inner)


def test_the_collection_and_the_sale_are_each_hashed_against_their_own_pin():
    d = _authorize(_intent(), _deltas(), rpc=_code_at(SALE, "0x6080deadbeef"))
    assert d.allowed is False and "pinned sale" in d.reason and "not the pinned" in d.reason, d.reason
    d = _authorize(_intent(), _deltas(), rpc=_code_at(SALE, "0x"))
    assert d.allowed is False and "pinned sale" in d.reason and "could not be read" in d.reason
    d = _authorize(_intent(), _deltas(), rpc=_code_at(COLLECTION, "0x6080deadbeef"))
    assert d.allowed is False and "pinned collection" in d.reason, d.reason


def test_a_sale_pinned_with_another_hash_refuses(monkeypatch):
    pin(monkeypatch, profile(COLLECTION, sale_pin=sale(COLLECTION, runtime_sha256="00" * 32)))
    d = _authorize(_intent(), _deltas())
    assert d.allowed is False and "pinned sale" in d.reason and "not the pinned" in d.reason


# --- the real receipt: two Transfers per id (115 M2) ---------------------------

def test_the_real_two_transfer_receipt_is_accepted():
    """The blocker of 2026-10-22: the contract mints each id to ITSELF, then moves it to the
    signer. The guard that expected Transfer(0x0 → signer) refused every real mint."""
    assert M.assert_simulation(_intent(), _deltas(), HOLDER) is None
    d = _authorize(_intent(), _deltas(), price=100.0)
    assert d.allowed is True, d.reason


def test_the_old_one_transfer_receipt_is_not_a_mint():
    logs = tuple(l for i in IDS for l in (
        _log(M.TOPIC_TRANSFER, _word(0), _addr_word(HOLDER), _word(i)),
        _log(M.TOPIC_MINTED, _word(i), _addr_word(HOLDER))))
    d = _authorize(_intent(), _deltas(logs=logs, holder_nft_in=tuple(
        (COLLECTION, "erc721", ZERO, i, 1) for i in IDS)))
    assert d.allowed is False


def test_ids_that_arrive_from_another_address_refuse():
    d = _authorize(_intent(), _deltas(holder_nft_in=tuple(
        (COLLECTION, "erc721", OTHER, i, 1) for i in IDS)))
    assert d.allowed is False and "exactly ids" in d.reason


def test_a_transfer_to_the_signer_from_another_address_refuses():
    logs = _mint_logs()[:-1] + (_log(M.TOPIC_TRANSFER, _addr_word(OTHER), _addr_word(HOLDER), _word(9)),)
    d = _authorize(_intent(), _deltas(logs=logs))
    assert d.allowed is False and "not part of a mint" in d.reason


def test_the_two_transfers_from_another_emitter_refuse():
    logs = tuple(dict(l, address=OTHER) if l["topics"][0] == M.TOPIC_TRANSFER else l
                 for l in _mint_logs())
    d = _authorize(_intent(), _deltas(logs=logs))
    assert d.allowed is False and "names the signer" in d.reason


def test_an_id_the_collection_did_not_mint_in_the_transaction_refuses():
    """The collection→signer move without the 0x0→collection mint is an existing token."""
    logs = tuple(l for l in _mint_logs() if not (l["topics"][0] == M.TOPIC_TRANSFER
                                                  and l["topics"][1] == _word(0)
                                                  and l["topics"][3] == _word(6)))
    d = _authorize(_intent(), _deltas(logs=logs))
    assert d.allowed is False and "Transfer(0x0 → collection)" in d.reason


def test_the_mint_to_the_collection_must_come_before_the_move():
    logs = list(_mint_logs())
    logs[0], logs[1] = logs[1], logs[0]          # id 5: moved, then minted
    d = _authorize(_intent(), _deltas(logs=tuple(logs)))
    assert d.allowed is False and "before the move" in d.reason


def test_a_repeated_transfer_to_the_signer_refuses():
    logs = _mint_logs() + (_log(M.TOPIC_TRANSFER, _addr_word(COLLECTION), _addr_word(HOLDER), _word(5)),)
    d = _authorize(_intent(), _deltas(logs=logs))
    assert d.allowed is False and "Transfer(collection → signer)" in d.reason


def test_the_sales_own_events_name_the_buyer_in_data_only_and_pass():
    """No sale event indexes an address (PolyrobSale: WLMinted, Bid, Owed, … all plain): the
    buyer is in the data, so nothing of the sale is allowlisted."""
    bid = "0x76b552d768564e2bf5325e2d83fe7184b2838e6e58fc33173aef30980e0269c8"
    owed = "0x7393c924ac30f9256e6cebcfb78841b1e7ef11bf8b09d87c2257b32b8063c979"
    logs = _mint_logs() + ({"address": SALE, "topics": [bid], "data": "0x" + _addr_word(HOLDER)[2:]},
                           {"address": SALE, "topics": [owed], "data": "0x" + _addr_word(HOLDER)[2:]})
    d = _authorize(_intent(), _deltas(logs=logs), price=100.0)
    assert d.allowed is True, d.reason
    forged = _mint_logs() + (_log(bid, _addr_word(HOLDER), address=SALE),)
    d = _authorize(_intent(), _deltas(logs=forged))
    assert d.allowed is False and "names the signer" in d.reason


def test_a_refund_inside_the_mint_refuses():
    """PolyrobSale.mint keeps all of msg.value (the excess comes back at settle; the dust is
    booked in owed): ETH back to the signer inside the mint is not this transaction."""
    d = _authorize(_intent(), _deltas(native_delta=-3 * PRICE + 10 ** 15))
    assert d.allowed is False and "wei" in d.reason


# --- the whitelist mint (115 M4) -----------------------------------------------

PROOF = ("0x" + "ab" * 32, "0x" + "cd" * 32)


def _wl_tx(qty=3, allowance=3, proof=PROOF, value=None, recipient=HOLDER):
    return _tx(data=M.encode_mint_wl(recipient, qty, 0, allowance, proof),
               value=qty * PRICE if value is None else value)


def test_a_whitelist_mint_at_exactly_qty_times_base_is_authorized():
    d = _authorize(_intent(), _deltas(), tx=_wl_tx(), price=100.0)
    assert d.allowed is True, d.reason


@pytest.mark.parametrize("value", [3 * PRICE - 1, 3 * PRICE + 1, 3 * MAX_PRICE_WEI])
def test_a_whitelist_value_other_than_qty_times_base_refuses(value):
    d = _authorize(_intent(amount_raw=value), _deltas(native_delta=-value), tx=_wl_tx(value=value))
    assert d.allowed is False and "whitelist mint of 3 is exactly" in d.reason


def test_a_whitelist_mint_above_its_allowance_refuses():
    d = _authorize(_intent(), _deltas(), tx=_wl_tx(allowance=2))
    assert d.allowed is False and "OverAllowance" in d.reason


def test_a_large_allowance_is_the_contracts_business():
    """PolyrobSale bounds the allowance only by wlMinted + qty <= allowance (any uint256 leaf)."""
    d = _authorize(_intent(), _deltas(), tx=_wl_tx(allowance=1000), price=100.0)
    assert d.allowed is True, d.reason


def test_a_whitelist_mint_to_a_third_party_refuses():
    d = _authorize(_intent(), _deltas(), tx=_wl_tx(recipient=OTHER))
    assert d.allowed is False and "not to the signing treasury" in d.reason


def test_a_proof_of_more_than_32_words_refuses():
    ok = M.encode_mint_wl(HOLDER, 3, 0, 3, ["0x" + "11" * 32] * 32)
    assert M.decode_mint_wl(ok)[4] == ("0x" + "11" * 32,) * 32
    d = _authorize(_intent(), _deltas(), tx=_tx(data=M.encode_mint_wl(
        HOLDER, 3, 0, 3, ["0x" + "11" * 32] * 33)))
    assert d.allowed is False and "at most 32" in d.reason


@pytest.mark.parametrize("mutate", [
    lambda d: d + "00" * 32,                                          # trailing bytes
    lambda d: d[:10 + 256] + format(0xc0, "064x") + d[10 + 320:],     # proof offset moved
    lambda d: d[:10 + 320] + format(10 ** 9, "064x") + d[10 + 384:],  # forged proof length
    lambda d: d[:-64],                                                # truncated
])
def test_a_non_canonical_whitelist_calldata_refuses(mutate):
    data = mutate(M.encode_mint_wl(HOLDER, 3, 0, 3, PROOF))
    with pytest.raises(ValueError):
        M.decode_mint_wl(data)
    d = _authorize(_intent(), _deltas(), tx=_tx(data=data))
    assert d.allowed is False


# --- the sale is paid only by the mint -----------------------------------------

def test_a_native_send_to_the_pinned_sale_outside_the_mint_refuses():
    intent = tx_guard.TxIntent(chain="robinhood", token=None, to=SALE, amount_raw=PRICE,
                               max_spend_usd=600.0, idempotency_key="k")
    d = _authorize(intent, _deltas(holder_nft_in=(), logs=()),
                   tx={"to": SALE, "data": "0x", "value": PRICE, "chainId": 4663, "nonce": 1,
                       "gas": 21_000, "maxFeePerGas": 10 ** 8})
    assert d.allowed is False and "paid only by the guarded mint" in d.reason


def test_a_zero_value_call_to_the_sale_is_not_a_payment():
    intent = tx_guard.TxIntent(chain="robinhood", token=None, to=SALE, amount_raw=0,
                               max_spend_usd=1.0, idempotency_key="k", is_claim=True)
    assert M.sale_payment_refusal(intent, {"to": SALE, "value": 0}) is None
    paying = tx_guard.TxIntent(chain="robinhood", token=USDC, to=SALE, amount_raw=5,
                               max_spend_usd=1.0, idempotency_key="k")
    assert "pinned sale" in M.sale_payment_refusal(paying, {"to": USDC, "value": 0})
