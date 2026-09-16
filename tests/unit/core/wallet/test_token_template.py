"""The pinned fixed-supply ERC-20 (042).

These tests need no compiler. They pin the artifact by hash, so a silent edit to
the bytes — the one change that would break every claim `deploy_token` makes —
fails here rather than on-chain.
"""
import pytest
from eth_utils import keccak

from core.wallet import token_template as T


def test_the_pinned_hashes_describe_the_pinned_bytes():
    assert T.INIT_CODE_HASH == "0x" + keccak(bytes.fromhex(T.INIT_CODE[2:])).hex()
    assert T.RUNTIME_HASH == "0x" + keccak(bytes.fromhex(T.RUNTIME[2:])).hex()


def test_the_artifact_is_within_both_eip_limits():
    from core.wallet import deploy_guard
    assert len(bytes.fromhex(T.INIT_CODE[2:])) <= deploy_guard.MAX_INIT_BYTES
    assert len(bytes.fromhex(T.RUNTIME[2:])) <= deploy_guard.MAX_RUNTIME_BYTES


def test_the_immutable_slots_are_zero_in_the_artifact():
    """The compiler emits them zeroed. A non-zero slot would mean the pinned
    runtime already carries somebody's numbers."""
    runtime = bytes.fromhex(T.RUNTIME[2:])
    for (offset, length) in T.IMMUTABLE_SLOTS:
        assert runtime[offset:offset + length] == bytes(length)


def _opcodes(code: bytes):
    """Every OPCODE in *code*, skipping PUSH immediates.

    A plain byte scan is wrong here and it matters: 0xff and 0xf4 both occur
    inside PUSH data in this very artifact (panic selectors, revert tails), so a
    naive `0xff in code` reports a SELFDESTRUCT that is not there. Walking the
    stream is nine lines and is the difference between a check and a superstition.
    """
    i = 0
    while i < len(code):
        op = code[i]
        yield op
        i += 1 + (op - 0x5F if 0x60 <= op <= 0x7F else 0)


@pytest.mark.parametrize("op,label", [
    (0xFF, "SELFDESTRUCT — the token could be made to vanish"),
    (0xF4, "DELEGATECALL — the token would be a proxy for someone else's code"),
    (0xF0, "CREATE — a token has no business deploying contracts"),
    (0xF5, "CREATE2"),
])
def test_the_runtime_contains_no_escape_hatch(op, label):
    assert op not in set(_opcodes(bytes.fromhex(T.RUNTIME[2:]))), label


def test_the_opcode_walker_skips_push_data():
    """PUSH1 0xff STOP is one PUSH and one STOP, not a SELFDESTRUCT."""
    assert list(_opcodes(bytes([0x60, 0xFF, 0x00]))) == [0x60, 0x00]
    assert 0xFF in set(_opcodes(bytes([0xFF])))


# -- constructor encoding --------------------------------------------------

def _decode(args_hex: str):
    """Minimal ABI decoder for (string,string,uint8,uint256), for the round-trip."""
    raw = bytes.fromhex(args_hex)

    def word(i):
        return int.from_bytes(raw[i * 32:(i + 1) * 32], "big")

    def read_string(offset):
        length = int.from_bytes(raw[offset:offset + 32], "big")
        return raw[offset + 32:offset + 32 + length].decode()

    return (read_string(word(0)), read_string(word(1)), word(2), word(3))


def test_constructor_args_round_trip():
    args = T.encode_constructor_args(name="Rob Coin", symbol="ROB",
                                     decimals=18, supply_raw=10 ** 27)
    assert _decode(args) == ("Rob Coin", "ROB", 18, 10 ** 27)


def test_a_multibyte_name_round_trips():
    args = T.encode_constructor_args(name="\u5c71\u7f8a\u30b3\u30a4\u30f3",
                                     symbol="\u5c71\u7f8a",
                                     decimals=6, supply_raw=1_000)
    assert _decode(args) == ("\u5c71\u7f8a\u30b3\u30a4\u30f3", "\u5c71\u7f8a", 6, 1_000)


def test_build_init_code_is_the_pinned_code_plus_the_args():
    code = T.build_init_code(name="A", symbol="B", decimals=0, supply_raw=1)
    assert code.startswith(T.INIT_CODE)
    assert _decode(code[len(T.INIT_CODE):]) == ("A", "B", 0, 1)


# -- refusals --------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "hides\u200bitself",      # zero-width space
    "flips\u202eeman",        # right-to-left override
    "bell\x07",
    "null\x00byte",
    "line\nbreak",
])
def test_a_name_built_to_be_misread_refuses(name):
    with pytest.raises(T.TokenTemplateError):
        T.encode_constructor_args(name=name, symbol="OK", decimals=18, supply_raw=1)


@pytest.mark.parametrize("kwargs", [
    dict(name="", symbol="OK"),
    dict(name="   ", symbol="OK"),
    dict(name="x" * 65, symbol="OK"),
    dict(name="OK", symbol="y" * 17),
])
def test_empty_or_oversized_text_refuses(kwargs):
    with pytest.raises(T.TokenTemplateError):
        T.encode_constructor_args(decimals=18, supply_raw=1, **kwargs)


@pytest.mark.parametrize("decimals", [-1, 19, 255])
def test_out_of_range_decimals_refuse(decimals):
    with pytest.raises(T.TokenTemplateError):
        T.encode_constructor_args(name="A", symbol="B", decimals=decimals, supply_raw=1)


def test_zero_supply_refuses_because_the_constructor_reverts():
    with pytest.raises(T.TokenTemplateError):
        T.encode_constructor_args(name="A", symbol="B", decimals=18, supply_raw=0)


def test_a_boolean_is_not_a_number():
    with pytest.raises(T.TokenTemplateError):
        T.encode_constructor_args(name="A", symbol="B", decimals=True, supply_raw=1)


# -- whole -> raw ----------------------------------------------------------

def test_whole_to_raw_scales():
    assert T.whole_to_raw(1_000_000_000, 18) == 10 ** 9 * 10 ** 18
    assert T.whole_to_raw(1.5, 18) == 15 * 10 ** 17


@pytest.mark.parametrize("supply,decimals,expected", [
    (1_000_000_000, 18, 10 ** 27),
    (1_000_000, 18, 10 ** 24),
    (123_456_789, 18, 123_456_789 * 10 ** 18),
    (999_999_999_999, 18, 999_999_999_999 * 10 ** 18),
    (1, 0, 1),
])
def test_the_supply_is_EXACT_not_float_scaled(supply, decimals, expected):
    """⚠️ Live, 2026-09-13: a dry run asked for 1,000,000,000 and the guard
    reported 1000000000000000013287555072 — because 1e9 * 10**18 in binary
    floating point is not 1e27, and above 2^53 the error lands in the INTEGER
    part. The token would have been minted with a supply nobody asked for, on a
    contract with no mint function, forever.
    """
    # A FLOAT is the real shape: `DeployTokenParams.supply` is `float`, which
    # is how the live run got a float into the multiplication in the first place.
    got = T.whole_to_raw(float(supply), decimals)
    assert got == expected, f"{supply}e{decimals} -> {got}, wanted {expected}"
    assert T.whole_to_raw(supply, decimals) == expected
    # And the float path it replaced was wrong for the headline case.
    if supply == 1_000_000_000 and decimals == 18:
        assert int(round(float(supply) * (10 ** decimals))) != expected


def test_a_fractional_supply_at_zero_decimals_refuses_rather_than_truncating():
    with pytest.raises(T.TokenTemplateError):
        T.whole_to_raw(1.5, 0)


def test_an_absurd_supply_is_read_as_a_typo():
    with pytest.raises(T.TokenTemplateError):
        T.whole_to_raw(T.MAX_WHOLE_SUPPLY * 10, 18)
