"""The Pons V2 provider (042) — pins, terms, pricing, and the refusals.

The pricing tests use figures MEASURED against on-chain simulation on
2026-09-13 (delta 0 across six trades). If the formula drifts, these fail with
the real numbers in hand rather than with a synthetic approximation.
"""
import pytest

from core.wallet import abi
from tools.launchpad import pons, pons_abi as P

HOLDER = "0xcAda546f6A6ddDE31B71aB21eF63d3EBF09Fa553"

#: Live curve state of 0xd8c32c…3ab2's bonding curve, read 2026-09-13.
LIVE_CURVE = dict(
    curve="0x526fce0f274615695073fd3a09a54f2646DF1E00",
    token="0xD8c32C1585758Bd7505F9ceA9C977a4294873ab2",
    quote_reserve=1_697_385_058_099_346_349,
    token_reserve=989_757_740_580_789_077_404_031_820,
    reserved_tokens=285_714_285_714_285_714_285_714_285,
    fee_bps=100, creator_tax_bps=0, snipe_tax_bps=0, graduated=False)


def _state(**kw):
    return pons.CurveState(**{**LIVE_CURVE, **kw})


class _Rpc:
    """A scripted eth_call responder keyed on the 4-byte selector."""

    def __init__(self, code=None, answers=None):
        self.code = code
        self.answers = answers or {}
        self.calls = []

    def __call__(self, method, params):
        if method == "eth_getCode":
            return self.code
        if method == "eth_call":
            data = params[0]["data"]
            self.calls.append((params[0]["to"], data[:10]))
            try:
                return self.answers[data[:10]]
            except KeyError:
                raise AssertionError(f"unscripted eth_call {data[:10]}")
        raise AssertionError(f"unexpected rpc {method}")


def _encoded(spec, values):
    return "0x" + abi.encode(spec["outputs"], values).hex()


# ==========================================================================
# Pins
# ==========================================================================

def test_pins_verify_when_the_code_hashes_match():
    """A stub whose code hashes to the pin passes; nothing else does."""
    from eth_utils import keccak

    body = b"\x60\x80\x60\x40"
    digest = "0x" + keccak(body).hex()
    rpc = _Rpc(code="0x" + body.hex())
    pinned = dict(P.CODE_HASHES)
    try:
        P.CODE_HASHES.clear()
        P.CODE_HASHES[P.FACTORY] = digest
        pons.verify_pins(rpc)
    finally:
        P.CODE_HASHES.clear()
        P.CODE_HASHES.update(pinned)


def test_a_moved_pin_REFUSES_rather_than_warning():
    rpc = _Rpc(code="0x6080")
    with pytest.raises(pons.PonsError) as exc:
        pons.verify_pins(rpc)
    assert "un-reviewed code" in str(exc.value)


def test_an_address_with_no_code_refuses():
    rpc = _Rpc(code="0x")
    with pytest.raises(pons.PonsError) as exc:
        pons.verify_pins(rpc)
    assert "has NO code" in str(exc.value)


def test_the_pinned_addresses_are_checksummed():
    """A failed EIP-55 checksum is how a swapped address usually announces
    itself, and `onchain` refuses one by design."""
    from eth_utils import to_checksum_address
    for addr in (P.FACTORY, P.ROUTER, P.DEPLOYER, P.MEME_HOOK, P.LOCKER):
        assert to_checksum_address(addr) == addr, addr


# ==========================================================================
# Terms
# ==========================================================================

def _terms_rpc(**over):
    cfg = P.GET_LAUNCH_CONFIG
    answers = {
        abi.selector(abi.signature_of("launchEnabled", []))[:10]:
            _encoded(P.LAUNCH_ENABLED, [over.get("enabled", True)]),
        abi.selector(abi.signature_of("launchFee", []))[:10]:
            _encoded(P.LAUNCH_FEE, [500_000_000_000_000]),
        abi.encode_call(cfg["name"], cfg["inputs"], [0])[:10]:
            _encoded(cfg, [(10 ** 27, 100, 168 * 10 ** 16,
                            42 * 10 ** 17, 0, 200,
                            over.get("cfg_enabled", True))]),
        abi.encode_call(P.PREVIEW_ECONOMICS["name"], P.PREVIEW_ECONOMICS["inputs"],
                        [0, P.NATIVE_PAIR])[:10]:
            _encoded(P.PREVIEW_ECONOMICS, ["0x" + "a9" * 32]),
    }
    return _Rpc(answers=answers)


def test_terms_are_read_live_not_assumed():
    terms = pons.read_terms(_terms_rpc())
    assert terms.launch_fee_wei == 500_000_000_000_000
    assert terms.supply_raw == 10 ** 27
    assert terms.graduation_threshold == 42 * 10 ** 17
    assert terms.economics == "0x" + "a9" * 32
    assert terms.pair_decimals == 18


def test_a_disabled_launch_config_refuses():
    with pytest.raises(pons.PonsError) as exc:
        pons.read_terms(_terms_rpc(cfg_enabled=False))
    assert "disabled" in str(exc.value)


def test_an_empty_view_response_refuses_rather_than_defaulting():
    rpc = _Rpc(answers={abi.selector("launchEnabled()")[:10]: "0x"})
    with pytest.raises(pons.PonsError) as exc:
        pons.read_terms(rpc)
    assert "returned nothing" in str(exc.value)


# ==========================================================================
# Pricing — measured, delta 0 against on-chain simulation
# ==========================================================================

def test_buy_matches_the_live_simulation_exactly():
    out, legs = pons.quote_buy(_state(), 12_345_678_901_234_567)
    assert out == 7_075_916_839_317_965_652_716_244
    assert legs["fee"] == 123_456_789_012_345
    assert legs["clamped_at_graduation"] == 0


def test_each_fee_leg_floors_SEPARATELY():
    """`quote_in * (10000 - fee - tax) // 10000` is off by one wei, which is
    enough to fire a min-out assertion on a correct trade."""
    state = _state(fee_bps=100, creator_tax_bps=77)
    quote_in = 12_345_678_901_234_567
    fee = quote_in * 100 // 10_000
    tax = quote_in * 77 // 10_000
    combined = quote_in * (10_000 - 100 - 77) // 10_000
    assert quote_in - fee - tax != combined          # the bug this guards
    _out, legs = pons.quote_buy(state, quote_in)
    assert legs["net"] == quote_in - fee - tax


def test_sell_fees_come_off_the_OUTPUT_not_the_input():
    state = _state(fee_bps=100, creator_tax_bps=0)
    tokens_in = 792_798_143_229_495_467_855_104
    out, legs = pons.quote_sell(state, tokens_in)
    gross = state.quote_reserve * tokens_in // (state.token_reserve + tokens_in)
    assert legs["gross"] == gross
    assert out == gross - gross // 100


def test_the_output_clamps_at_the_graduation_reserve():
    """Near graduation the curve fills only the sellable remainder; the plain
    constant-product formula over-estimates there."""
    state = _state(token_reserve=300_000_000_000_000_000_000_000_000)
    out, legs = pons.quote_buy(state, 10 ** 18)
    assert legs["clamped_at_graduation"] == 1
    assert out == state.token_reserve - state.reserved_tokens


def test_fees_that_consume_the_whole_input_refuse():
    """The opening snipe rate is 9900 bps. Paying it is never the answer."""
    with pytest.raises(pons.PonsError) as exc:
        pons.quote_buy(_state(snipe_tax_bps=9_900, fee_bps=100), 1000)
    assert "consume the whole input" in str(exc.value)


@pytest.mark.parametrize("amount", [0, -1])
def test_a_non_positive_amount_refuses(amount):
    with pytest.raises(pons.PonsError):
        pons.quote_buy(_state(), amount)
    with pytest.raises(pons.PonsError):
        pons.quote_sell(_state(), amount)


# ==========================================================================
# Calldata
# ==========================================================================

def _terms():
    return pons.LaunchTerms(
        enabled=True, launch_fee_wei=500_000_000_000_000, supply_raw=10 ** 27,
        curve_fee_bps=100, phantom_quote=168 * 10 ** 16,
        graduation_threshold=42 * 10 ** 17, economics="0x" + "a9" * 32,
        pair_decimals=18)


def test_a_plain_launch_carries_EXACTLY_the_launch_fee():
    """`if (msg.value != launchFee) revert LaunchFeeNotPaid()` — exact equality,
    not a minimum."""
    built = pons.build_launch(terms=_terms(), name="Rob", symbol="ROB",
                              creator=HOLDER)
    assert built["to"] == P.FACTORY
    assert built["value_wei"] == 500_000_000_000_000
    assert built["calldata"][:10] == abi.selector(
        abi.signature_of(P.LAUNCH_TOKEN["name"], P.LAUNCH_TOKEN["inputs"]))


def test_launch_and_buy_carries_the_fee_PLUS_the_buy_on_a_native_pair():
    built = pons.build_launch_and_buy(
        terms=_terms(), name="Rob", symbol="ROB", creator=HOLDER,
        recipient=HOLDER, quote_in=10 ** 17, min_tokens_out=1)
    assert built["to"] == P.ROUTER
    assert built["value_wei"] == 500_000_000_000_000 + 10 ** 17
    assert built["needs_erc20_approval"] is False


def test_an_erc20_pair_carries_only_the_fee_and_says_it_needs_an_approval():
    built = pons.build_launch_and_buy(
        terms=_terms(), name="Rob", symbol="ROB", creator=HOLDER,
        recipient=HOLDER, quote_in=10 ** 6, min_tokens_out=1,
        pair_token="0x5FC5360d0400a0fd4F2Af552aDD042d716f1d168")
    assert built["value_wei"] == 500_000_000_000_000
    assert built["needs_erc20_approval"] is True


def test_a_third_party_recipient_is_AUTO_EXEMPTED_from_the_snipe_tax():
    """The factory exempts only the deployer and the creator-fee recipient. An
    un-exempted opening buy pays the 99% opening rate."""
    other = "0x1111111111111111111111111111111111111111"
    built = pons.build_launch_and_buy(
        terms=_terms(), name="Rob", symbol="ROB", creator=HOLDER,
        recipient=other, quote_in=10 ** 17, min_tokens_out=1)
    args = abi.decode(P.LAUNCH_AND_BUY["inputs"], "0x" + built["calldata"][10:])
    assert [a.lower() for a in args[6]] == [other.lower()]


def test_the_creator_is_not_double_exempted():
    built = pons.build_launch_and_buy(
        terms=_terms(), name="Rob", symbol="ROB", creator=HOLDER,
        recipient=HOLDER, quote_in=10 ** 17, min_tokens_out=1)
    args = abi.decode(P.LAUNCH_AND_BUY["inputs"], "0x" + built["calldata"][10:])
    assert args[6] == []


def test_the_economics_commitment_is_CARRIED_not_waived():
    """bytes32(0) waives the check — i.e. accepts whatever the terms became
    between the quote and the broadcast. We always commit."""
    built = pons.build_launch(terms=_terms(), name="Rob", symbol="ROB",
                              creator=HOLDER)
    args = abi.decode(P.LAUNCH_TOKEN["inputs"], "0x" + built["calldata"][10:])
    assert args[0][8] == "0x" + "a9" * 32
    assert args[0][8] != "0x" + "00" * 32


def test_the_router_refuses_a_zero_opening_buy_with_the_reason():
    with pytest.raises(pons.PonsError) as exc:
        pons.build_launch_and_buy(terms=_terms(), name="R", symbol="R",
                                  creator=HOLDER, recipient=HOLDER,
                                  quote_in=0, min_tokens_out=0)
    assert "always performs an opening buy" in str(exc.value)


def test_a_creator_tax_above_the_factory_maximum_refuses():
    with pytest.raises(pons.PonsError) as exc:
        pons.build_launch(terms=_terms(), name="R", symbol="R", creator=HOLDER,
                          creator_tax_bps=1001)
    assert "maxCreatorTaxBps" in str(exc.value)


def test_too_many_snipe_exemptions_refuse():
    many = tuple(f"0x{i:040x}" for i in range(1, P.MAX_SNIPE_EXEMPTIONS + 2))
    with pytest.raises(pons.PonsError):
        pons.build_launch(terms=_terms(), name="R", symbol="R", creator=HOLDER,
                          snipe_exemptions=many)


def test_each_launch_gets_a_FRESH_salt():
    """Reusing a salt on identical terms reverts, and a persisted counter that
    drifts reverts a launch for a reason nobody can see."""
    a = pons.build_launch(terms=_terms(), name="R", symbol="R", creator=HOLDER)
    b = pons.build_launch(terms=_terms(), name="R", symbol="R", creator=HOLDER)
    assert a["salt"] != b["salt"]
    assert len(a["salt"]) == 66


def test_a_sell_needs_no_value_and_no_approval():
    built = pons.build_sell(curve=LIVE_CURVE["curve"], tokens_in=10,
                            min_quote_out=1, recipient=HOLDER)
    assert built["value_wei"] == 0
    assert built["to"] == LIVE_CURVE["curve"]


# ==========================================================================
# Receipt reading
# ==========================================================================

def test_the_launched_address_is_read_from_the_RECEIPT():
    """Pons deploys through CREATE2 inside its own factory, so the deploy verb's
    nonce-based prediction does not apply and guessing would name the wrong
    contract."""
    logs = [{"topics": ["0xdeadbeef"], "data": "0x"},
            {"topics": [P.TOPIC_TOKEN_LAUNCHED,
                        "0x" + "00" * 12 + "11" * 20,
                        "0x" + "00" * 12 + "22" * 20,
                        "0x" + "00" * 12 + "33" * 20], "data": "0x"}]
    found = pons.parse_token_launched(logs)
    assert found["token"].lower() == "0x" + "11" * 20
    assert found["curve"].lower() == "0x" + "22" * 20


def test_no_launch_event_returns_None_rather_than_inventing_an_address():
    assert pons.parse_token_launched([{"topics": ["0x00"], "data": "0x"}]) is None
    assert pons.parse_token_launched([]) is None
    assert pons.parse_token_launched(None) is None
