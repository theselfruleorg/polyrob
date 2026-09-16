"""The ABI encoder/decoder (042).

The headline test is `test_live_pons_calldata_round_trips_byte_for_byte`: an ABI
shape that is subtly wrong produces calldata a node ACCEPTS and the contract
reads as different arguments, so unit tests over synthetic values cannot catch
it. Two real mainnet transactions can.
"""
import json
import pathlib

import pytest

from core.wallet import abi

FIXTURE = (pathlib.Path(__file__).parents[3] / "fixtures"
           / "pons_live_calldata.json")


# ==========================================================================
# The acceptance test
# ==========================================================================

@pytest.mark.parametrize("label", ["launchToken", "launchAndBuy"])
def test_live_pons_calldata_round_trips_byte_for_byte(label):
    from tools.launchpad import pons_abi as P

    spec = {"launchToken": P.LAUNCH_TOKEN, "launchAndBuy": P.LAUNCH_AND_BUY}[label]
    live = json.loads(FIXTURE.read_text())[label]["input"]

    # 1. The selector must DERIVE from our ABI shape, not be pasted beside it.
    derived = abi.selector(abi.signature_of(spec["name"], spec["inputs"]))
    assert derived == live[:10], (
        f"our {label} shape hashes to {derived}, but the live transaction "
        f"carries {live[:10]} — the parameter tuple is wrong")

    # 2. Decode the real arguments and re-encode them.
    args = abi.decode(spec["inputs"], "0x" + live[10:])
    assert abi.encode_call(spec["name"], spec["inputs"], list(args)) == live


def test_the_fixture_is_the_transaction_it_claims_to_be():
    """A fixture that drifted from its tx hash would silently stop testing Pons."""
    from tools.launchpad import pons_abi as P
    data = json.loads(FIXTURE.read_text())
    assert data["launchToken"]["to"].lower() == P.FACTORY.lower()
    assert data["launchAndBuy"]["to"].lower() == P.ROUTER.lower()
    # Both carry the launch fee, which is exact-equality checked on-chain.
    assert data["launchToken"]["value"] == 500_000_000_000_000


# ==========================================================================
# Shapes
# ==========================================================================

def _round(inputs, values):
    encoded = abi.encode(inputs, values)
    return list(abi.decode(inputs, "0x" + encoded.hex()))


def test_static_types_round_trip():
    inputs = [{"name": "a", "type": "uint256"}, {"name": "b", "type": "bool"},
              {"name": "c", "type": "address"}, {"name": "d", "type": "bytes32"}]
    values = [2 ** 200, True, "0x" + "11" * 20, "0x" + "ab" * 32]
    got = _round(inputs, values)
    assert got[0] == values[0] and got[1] is True
    assert got[2].lower() == values[2] and got[3] == values[3]


def test_dynamic_types_round_trip():
    inputs = [{"name": "s", "type": "string"}, {"name": "b", "type": "bytes"},
              {"name": "xs", "type": "uint256[]"}]
    assert _round(inputs, ["héllo", "0xdeadbeef", [1, 2, 3]]) == \
        ["héllo", "0xdeadbeef", [1, 2, 3]]


def test_an_empty_string_and_an_empty_array_round_trip():
    """The zero-length cases are where a hand-rolled encoder usually breaks."""
    inputs = [{"name": "s", "type": "string"}, {"name": "xs", "type": "address[]"}]
    assert _round(inputs, ["", []]) == ["", []]


def test_a_nested_tuple_with_dynamic_members_round_trips():
    """The Pons shape in miniature: a struct inside a struct, with strings."""
    inner = {"name": "inner", "type": "tuple", "components": [
        {"name": "a", "type": "string"}, {"name": "b", "type": "string"}]}
    outer = {"name": "outer", "type": "tuple", "components": [
        {"name": "n", "type": "string"}, inner,
        {"name": "who", "type": "address"}, {"name": "k", "type": "uint16"}]}
    inputs = [outer, {"name": "tail", "type": "address[]"}]
    values = [["top", ["x", ""], "0x" + "22" * 20, 100], ["0x" + "33" * 20]]
    got = _round(inputs, values)
    assert got[0][0] == "top" and got[0][1] == ("x", "")
    assert got[0][3] == 100
    assert [a.lower() for a in got[1]] == ["0x" + "33" * 20]


def test_a_fixed_size_array_round_trips():
    inputs = [{"name": "xs", "type": "uint256[3]"}]
    assert _round(inputs, [[7, 8, 9]]) == [[7, 8, 9]]


def test_a_negative_int_round_trips():
    inputs = [{"name": "t", "type": "int24"}]
    assert _round(inputs, [-200]) == [-200]


def test_the_canonical_signature_flattens_nested_tuples():
    inner = {"name": "i", "type": "tuple", "components": [
        {"name": "a", "type": "string"}]}
    outer = {"name": "o", "type": "tuple", "components": [
        {"name": "n", "type": "uint8"}, inner]}
    assert abi.signature_of("f", [outer, {"name": "x", "type": "address[]"}]) == \
        "f((uint8,(string)),address[])"


# ==========================================================================
# Refusals
# ==========================================================================

@pytest.mark.parametrize("kind,value", [
    ("uint8", 256),
    ("uint256", -1),
    ("int8", 128),
    ("address", "0xdeadbeef"),
    ("bytes32", "0x1234"),
    ("bool", 1),
    ("uint256", "7"),
    ("uint256", True),
])
def test_a_value_that_does_not_fit_its_type_refuses(kind, value):
    with pytest.raises(abi.AbiError):
        abi.encode([{"name": "v", "type": kind}], [value])


def test_a_wrong_argument_count_refuses():
    with pytest.raises(abi.AbiError):
        abi.encode([{"name": "a", "type": "uint256"}], [1, 2])


def test_an_unsupported_type_raises_rather_than_guessing():
    with pytest.raises(abi.AbiError):
        abi.encode([{"name": "v", "type": "ufixed128x18"}], [1])


def test_truncated_data_refuses_rather_than_decoding_zeros():
    """A short RPC reply must not become a confident number — the same class as
    a failed balance read rendered as $0.00."""
    with pytest.raises(abi.AbiError) as exc:
        abi.decode([{"name": "a", "type": "uint256"},
                    {"name": "b", "type": "uint256"}], "0x" + "00" * 32)
    assert "truncated" in str(exc.value)


# ==========================================================================
# Signed integers must be SIGN-EXTENDED (042b)
# ==========================================================================

@pytest.mark.parametrize("kind,value", [
    ("int24", -200), ("int24", 200), ("int8", -128), ("int8", 127),
    ("int256", -(2 ** 255)), ("int256", 2 ** 255 - 1), ("int24", 0),
])
def test_signed_integers_round_trip_across_the_sign_boundary(kind, value):
    inputs = [{"name": "v", "type": kind}]
    assert _round(inputs, [value]) == [value]


def test_a_value_that_is_not_sign_extended_REFUSES():
    """An int24 of -200 sent unextended reads as a perfectly plausible 65336.
    Returning it would be the same class as treating a truncated response as
    zero: a wrong number that looks like an answer."""
    unextended = "0x" + "00" * 29 + "ff" + "38" + "00"
    with pytest.raises(abi.AbiError) as exc:
        abi.decode([{"name": "tick", "type": "int24"}], unextended)
    assert "sign-extended" in str(exc.value)


def test_a_correctly_sign_extended_negative_decodes():
    extended = "0x" + "ff" * 29 + "ff" + "ff" + "38"
    assert abi.decode([{"name": "tick", "type": "int24"}], extended) == (-200,)
