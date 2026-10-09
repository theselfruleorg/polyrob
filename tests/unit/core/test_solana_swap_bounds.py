"""Verify signed Jupiter bounds, including suffix forgery and recipient binding."""
from dataclasses import replace
from hashlib import sha256
import struct
from unittest.mock import Mock

import pytest

from core.wallet.solana_swap_bounds import SwapBounds, V6, refusal
from core.wallet.solana_tx_inspect import DecodedIx, candidate_atas, simulate

OWNER = "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"
BOUNDS = SwapBounds(USDC, WSOL, 1_000_000, 990_000, 100)


def route(*, shared=False, quoted=1_000_000, slippage=100, amount=1_000_000,
          variant=7, payload=b"", extra=b"", fee=0):
    name = b"shared_accounts_route" if shared else b"route"
    data = sha256(b"global:" + name).digest()[:8] + (b"\x00" if shared else b"")
    data += struct.pack("<I", 1) + bytes([variant]) + payload + bytes([100, 0, 1])
    data += struct.pack("<QQHB", amount, quoted, slippage, fee) + extra
    destination = candidate_atas(OWNER, WSOL)[0]
    accounts = ([V6, V6, OWNER, V6, V6, V6, destination, USDC, WSOL] if shared
                else [V6, OWNER, V6, destination, V6, WSOL])
    return DecodedIx(V6, data, tuple(accounts))


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("variant,payload", [(7, b""), (17, b"\x01"),
                                             (29, b"\x00" * 16), (33, b"\x00" * 4)])
def test_understood_routes_decode_exactly(shared, variant, payload):
    assert refusal([route(shared=shared, variant=variant, payload=payload)], BOUNDS, owner=OWNER) is None


@pytest.mark.parametrize("changes", [dict(quoted=1), dict(slippage=101), dict(amount=2),
                                     dict(variant=255), dict(fee=1), dict(extra=b"x"),
                                     dict(variant=17, payload=b"\xff")])
def test_weaker_or_unknown_instructions_refuse(changes):
    assert refusal([route(**changes)], BOUNDS, owner=OWNER)


def test_protective_suffix_cannot_hide_the_executed_zero_floor():
    forged = route(quoted=0, extra=struct.pack("<QQHB", 1_000_000, 1_000_000, 100, 0))
    assert "trailing" in refusal([forged], BOUNDS, owner=OWNER)


@pytest.mark.parametrize("index,value", [(1, V6), (3, V6), (4, OWNER), (5, USDC), (5, None)])
def test_authority_mint_and_recipient_must_be_resolved_and_match(index, value):
    ix = route()
    accounts = list(ix.accounts)
    accounts[index] = value
    assert refusal([replace(ix, accounts=tuple(accounts))], BOUNDS, owner=OWNER)


def test_two_routes_cannot_mask_each_others_minimum():
    assert refusal([route(), route()], BOUNDS, owner=OWNER)


def test_real_transaction_refused_before_any_simulation_rpc():
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.signature import Signature
    from solders.transaction import VersionedTransaction
    ix = route(quoted=1)
    accounts = [AccountMeta(Pubkey.from_string(a), a == OWNER, False) for a in ix.accounts]
    msg = MessageV0.try_compile(Pubkey.from_string(OWNER), [
        Instruction(Pubkey.from_string(V6), ix.data, accounts)], [], Hash.default())
    raw = bytes(VersionedTransaction.populate(msg, [Signature.default()]))
    rpc = Mock(side_effect=AssertionError("must not call RPC for a bad instruction floor"))
    result = simulate(raw, owner=OWNER, rpc=rpc, swap_bounds=BOUNDS)
    assert not result.ok and "minimum output" in result.reason
    rpc.assert_not_called()


def _alt_route_tx(table_key):
    """A v0 transaction whose route loads the output mint and destination from a lookup
    table — the shape Jupiter's /swap builds."""
    from solders.address_lookup_table_account import AddressLookupTableAccount
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.signature import Signature
    from solders.transaction import VersionedTransaction
    ix = route()
    destination = candidate_atas(OWNER, WSOL)[0]
    accounts = [AccountMeta(Pubkey.from_string(a), a == OWNER, a == destination) for a in ix.accounts]
    table = AddressLookupTableAccount(Pubkey.from_string(table_key),
                                      [Pubkey.from_string(destination), Pubkey.from_string(WSOL)])
    msg = MessageV0.try_compile(Pubkey.from_string(OWNER), [
        Instruction(Pubkey.from_string(V6), ix.data, accounts)], [table], Hash.default())
    assert msg.address_table_lookups, "the fixture must actually load accounts from the table"
    return bytes(VersionedTransaction.populate(msg, [Signature.default()])), table


def _table_rpc(table, *, owner="AddressLookupTab1e1111111111111111111111111"):
    import base64
    data = b"\x01" + b"\x00" * 55 + b"".join(bytes(a) for a in table.addresses)

    def rpc(method, params):
        assert method == "getAccountInfo" and params[0] == str(table.key)
        return {"value": {"owner": owner, "data": [base64.b64encode(data).decode(), "base64"]}}
    return rpc


TABLE = "7ZQoGzVJcYqHuTsmyTSkuNNP4P5ubyXnXuiJb8Pq2a4H"


def test_a_route_that_loads_its_mint_from_a_lookup_table_is_resolved_and_passes():
    from core.wallet.solana_tx_inspect import inspect_transaction
    raw, table = _alt_route_tx(TABLE)
    unresolved = inspect_transaction(raw, owner=OWNER)
    assert refusal(list(unresolved.instructions), BOUNDS, owner=OWNER)      # None accounts refuse
    resolved = inspect_transaction(raw, owner=OWNER, rpc=_table_rpc(table))
    assert resolved.ok, resolved.reason
    assert refusal(list(resolved.instructions), BOUNDS, owner=OWNER) is None


def test_an_unreadable_or_foreign_lookup_table_refuses():
    from core.wallet.solana_tx_inspect import inspect_transaction
    raw, table = _alt_route_tx(TABLE)
    bad = inspect_transaction(raw, owner=OWNER, rpc=_table_rpc(table, owner=V6))
    assert not bad.ok and "lookup tables could not be read" in bad.reason

    def down(method, params):
        raise OSError("rpc down")
    assert not inspect_transaction(raw, owner=OWNER, rpc=down).ok


# --- every pinned Jupiter v6 variant decodes; only a truly unknown one refuses -----

from core.wallet.jupiter_v6_schema import SWAP_VARIANTS, skip_swap  # noqa: E402


def _step(variant, payload, *, v2=False):
    share = struct.pack("<HBB", 10000, 0, 1) if v2 else bytes([100, 0, 1])
    return bytes([variant]) + payload + share


def route_plan(steps, *, shared=False, v2=False, quoted=1_000_000, slippage=100,
               amount=1_000_000, positive=0, extra=b""):
    if v2:
        name = b"shared_accounts_route_v2" if shared else b"route_v2"
        data = sha256(b"global:" + name).digest()[:8] + (b"\x00" if shared else b"")
        data += struct.pack("<QQHHH", amount, quoted, slippage, 0, positive)
        data += struct.pack("<I", len(steps)) + b"".join(steps) + extra
        destination = candidate_atas(OWNER, WSOL)[0]
        accounts = ([V6, OWNER, V6, V6, V6, destination, USDC, WSOL] if shared
                    else [OWNER, V6, destination, USDC, WSOL, V6, V6, V6])
        return DecodedIx(V6, data, tuple(accounts))
    ix = route(shared=shared, quoted=quoted, slippage=slippage, amount=amount)
    head = 9 if shared else 8
    data = ix.data[:head] + struct.pack("<I", len(steps)) + b"".join(steps)
    data += struct.pack("<QQHB", amount, quoted, slippage, 0) + extra
    return replace(ix, data=data)


# Hand-encoded payloads for the high-index variants a live route actually uses,
# written from the IDL field lists (not derived from the table under test).
HIGH_VARIANTS = [
    (47, b"\x01" + b"\x01" + struct.pack("<I", 2) + b"\x00\x05\x06\x03"),  # WhirlpoolSwapV2
    (47, b"\x00\x00"),                                                       # ... with None
    (61, b"\x01"),                                                           # SolFi
    (72, b""),                                                               # PumpSwapBuy
    (75, struct.pack("<I", 1) + b"\x00\x02"),                                # MeteoraDlmmSwapV2
    (81, struct.pack("<Q", 7)),                                              # RaydiumLaunchlabBuy
    (87, struct.pack("<Q", 9) + b"\x01"),                                    # HumidiFi
    (111, struct.pack("<I", 2) + b"\x03" + b"\x05\x01" + b"\x01\x02"),       # DynamicV1
    (120, b"\x01" + struct.pack("<I", 3) + b"abc"),                          # JupiterRfqV2
    (122, b"\x00" * 16),                                                     # Scorch
    (123, b"\x00" * 48),                                                     # VaultLiquidUnstake
    (132, b"\x07"),                                                          # Hylo
    (146, struct.pack("<I", 1) + b"\x03" + struct.pack("<I", 5000) + b"\x02\x03"),  # DynamicV2
    (185, struct.pack("<Q", 1) + b"\x00" * 48 + b"\x01"),                    # HumidiFiRouter
    (190, struct.pack("<QQQQ", 1, 2, 3, 4) + b"\x00" * 48 + b"\x00"),        # HumidiFiRouterV3
    (196, b"\x01"),                                                          # DenaliV2 (last)
]


def test_the_pinned_table_is_the_reviewed_idl_for_the_first_39_variants():
    assert len(SWAP_VARIANTS) == 197
    one_byte = {8, 12, 15, 16, 17, 18, 21, 23, 24, 27, 28}
    for tag in range(39):
        width = 1 if tag in one_byte else 16 if tag == 29 else 4 if tag == 33 else 0
        payload = b"\x01" * width
        assert skip_swap(bytes([tag]) + payload + b"tail", 0) == 1 + width, tag


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("variant,payload", HIGH_VARIANTS)
def test_high_index_variants_decode_exactly(shared, variant, payload):
    ix = route_plan([_step(variant, payload)], shared=shared)
    assert refusal([ix], BOUNDS, owner=OWNER) is None


def test_a_multi_hop_route_mixing_old_and_new_variants_decodes():
    steps = [_step(v, p) for v, p in HIGH_VARIANTS] + [_step(17, b"\x01"), _step(7, b"")]
    assert refusal([route_plan(steps)], BOUNDS, owner=OWNER) is None


def test_a_high_variant_still_cannot_hide_a_forged_floor():
    forged = route_plan([_step(87, struct.pack("<Q", 9) + b"\x01")], quoted=0,
                        extra=struct.pack("<QQHB", 1_000_000, 1_000_000, 100, 0))
    assert "trailing" in refusal([forged], BOUNDS, owner=OWNER)


def test_an_invalid_field_in_a_high_variant_refuses():
    bad_bool = route_plan([_step(87, struct.pack("<Q", 9) + b"\x05")])
    assert "invalid swap step" in refusal([bad_bool], BOUNDS, owner=OWNER)
    bad_enum = route_plan([_step(132, b"\x08")])   # HyloSwapType has 8 values
    assert "invalid swap step" in refusal([bad_enum], BOUNDS, owner=OWNER)


def test_a_truly_unknown_variant_refuses_and_says_so_plainly():
    why = refusal([route_plan([_step(len(SWAP_VARIANTS), b"")])], BOUNDS, owner=OWNER)
    assert why and f"swap variant {len(SWAP_VARIANTS)}" in why
    assert "newer than the pinned" in why and "regenerated" in why


@pytest.mark.parametrize("shared", [False, True])
def test_route_v2_layouts_decode_and_check_bounds(shared):
    good = route_plan([_step(87, struct.pack("<Q", 9) + b"\x01", v2=True)], shared=shared, v2=True)
    assert refusal([good], BOUNDS, owner=OWNER) is None
    low = route_plan([_step(72, b"", v2=True)], shared=shared, v2=True, quoted=1)
    assert "minimum output" in refusal([low], BOUNDS, owner=OWNER)
    fee = route_plan([_step(72, b"", v2=True)], shared=shared, v2=True, positive=50)
    assert "platform fee" in refusal([fee], BOUNDS, owner=OWNER)
    tail = route_plan([_step(72, b"", v2=True)], shared=shared, v2=True, extra=b"x")
    assert "trailing" in refusal([tail], BOUNDS, owner=OWNER)


def test_route_v2_recipient_must_be_the_wallet():
    ix = route_plan([_step(72, b"", v2=True)], v2=True)
    accounts = list(ix.accounts)
    accounts[2] = V6
    assert "associated account" in refusal([replace(ix, accounts=tuple(accounts))], BOUNDS, owner=OWNER)
