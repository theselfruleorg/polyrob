"""The contract call's function + args encoding never guesses an integer argument."""
import pytest

from tools.defi.call_verb import encode_call

ADDR = "0x" + "ab" * 20


def test_an_exact_integer_encodes():
    assert encode_call("mint(address,uint256)", [ADDR, 3]) == \
        encode_call("mint(address,uint256)", [ADDR, "3"]) == \
        encode_call("mint(address,uint256)", [ADDR, 3.0])


@pytest.mark.parametrize("bad", [1.5, 1234567890123456789.0, float("inf")])
def test_a_float_that_is_not_an_exact_integer_refuses(bad):
    with pytest.raises(ValueError, match="exact integer"):
        encode_call("mint(address,uint256)", [ADDR, bad])


def test_a_large_amount_as_a_decimal_string_is_exact():
    data = encode_call("mint(address,uint256)", [ADDR, "1234567890123456789"])
    assert int(data[-64:], 16) == 1234567890123456789
