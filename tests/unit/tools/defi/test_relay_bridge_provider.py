"""The Relay provider asserts its answer against what we asked for (037).

Every refusal below is a way a third party's quote could move OUR funds
somewhere we did not choose. None of them is hypothetical politeness: the
recipient case alone is the difference between a bridge and a donation.
"""
import copy

import pytest

from tools.defi.providers.relay_bridge import (NATIVE_EVM, NATIVE_SVM,
                                               RelayBridgeProvider, RelayError)

EVM = "0xcAda546f6A6ddDE31B71aB21eF63d3EBF09Fa553"
SOL = "BrsPwATRZcb2PWsEZba9Bh1mwcxU6M7R64nPgRneCpmL"
SOLANA = 792703809


def _quote_body(**over):
    d = {
        "requestId": "0xabc",
        "details": {
            "sender": SOL, "recipient": EVM,
            "currencyIn": {"amount": "500000000", "amountUsd": "50.9",
                           "currency": {"chainId": SOLANA, "address": NATIVE_SVM,
                                        "symbol": "SOL", "decimals": 9}},
            "currencyOut": {"amount": "19707886940203218", "amountUsd": "50.6",
                            "currency": {"chainId": 4663, "address": NATIVE_EVM,
                                         "symbol": "ETH", "decimals": 18}},
            "slippageTolerance": {"destination": {"value": "394157738804064"}},
            "totalImpact": {"percent": "-0.66"}, "timeEstimate": 1,
        },
        "steps": [{"depositAddress": "", "items": [
            {"data": {"instructions": [{"programId": "p", "keys": [], "data": "00"}]}}]}],
    }
    d.update(over)
    return d


def _provider(body):
    return RelayBridgeProvider(post=lambda url, b, timeout=None: copy.deepcopy(body),
                               get=lambda url, timeout=None: {"status": "pending"})


def _ask(provider, **over):
    kw = dict(origin_chain_id=SOLANA, dest_chain_id=4663,
              origin_currency=NATIVE_SVM, dest_currency=NATIVE_EVM,
              amount_in_raw=500_000_000, sender=SOL, recipient=EVM)
    kw.update(over)
    return provider.quote(**kw)


def test_a_clean_quote_parses_and_computes_the_arrival_floor():
    q = _ask(_provider(_quote_body()))
    assert q.request_id == "0xabc"
    assert q.amount_out_raw == 19707886940203218
    # floor = quoted out - the destination slippage allowance. This number is
    # what phase 2 asserts the measured arrival against.
    assert q.min_out_raw == 19707886940203218 - 394157738804064
    assert q.svm_origin is True


def test_a_quote_that_pays_someone_else_is_refused():
    """The one failure mode that loses everything."""
    body = _quote_body()
    body["details"]["recipient"] = "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    with pytest.raises(RelayError, match="pays"):
        _ask(_provider(body))


def test_a_quote_for_a_different_chain_is_refused():
    body = _quote_body()
    body["details"]["currencyOut"]["currency"]["chainId"] = 1
    with pytest.raises(RelayError, match="chains"):
        _ask(_provider(body))


def test_a_quote_for_a_different_asset_is_refused():
    body = _quote_body()
    body["details"]["currencyIn"]["currency"]["address"] = "SomeOtherMint111"
    with pytest.raises(RelayError, match="not the requested"):
        _ask(_provider(body))


def test_a_quote_that_consumes_more_than_declared_is_refused():
    body = _quote_body()
    body["details"]["currencyIn"]["amount"] = "900000000"
    with pytest.raises(RelayError, match="consumes"):
        _ask(_provider(body))


def test_missing_slippage_refuses_rather_than_defaulting_the_floor_to_zero():
    """A zero floor asserts nothing on arrival — the guard would always pass."""
    body = _quote_body()
    body["details"]["slippageTolerance"] = {}
    with pytest.raises(RelayError, match="no destination slippage"):
        _ask(_provider(body))


def test_missing_decimals_refuses():
    body = _quote_body()
    del body["details"]["currencyOut"]["currency"]["decimals"]
    with pytest.raises(RelayError, match="decimals"):
        _ask(_provider(body))


def test_a_multi_step_quote_is_refused():
    """Guessing which step is the value move is how funds go to the wrong place."""
    body = _quote_body()
    body["steps"] = body["steps"] * 2
    with pytest.raises(RelayError, match="steps"):
        _ask(_provider(body))


def test_no_request_id_refuses_because_arrival_could_not_be_confirmed():
    body = _quote_body(requestId="")
    with pytest.raises(RelayError, match="requestId"):
        _ask(_provider(body))


def test_same_chain_is_a_swap_not_a_bridge():
    with pytest.raises(RelayError, match="same chain"):
        _ask(_provider(_quote_body()), origin_chain_id=8453, dest_chain_id=8453)


def test_an_evm_quote_targeting_another_chain_is_refused():
    body = _quote_body()
    body["details"]["currencyIn"]["currency"].update({"chainId": 8453, "address": NATIVE_EVM,
                                                      "symbol": "ETH", "decimals": 18})
    body["details"]["sender"] = EVM
    body["steps"][0]["items"][0]["data"] = {"to": "0xabc", "data": "0x", "chainId": 1}
    with pytest.raises(RelayError, match="targets chain"):
        _ask(_provider(body), origin_chain_id=8453, origin_currency=NATIVE_EVM, sender=EVM)


@pytest.mark.parametrize("raw,expected", [
    ({"status": "success"}, "success"),
    ({"status": "refund"}, "failure"),
    ({"status": "pending"}, "pending"),
    ({"status": "something-new"}, "unknown"),
])
def test_status_vocabulary_never_optimistically_maps_onto_success(raw, expected):
    p = RelayBridgeProvider(get=lambda url, timeout=None: raw)
    assert p.status("0xabc")[0] == expected


def test_an_unreachable_status_endpoint_is_unknown_not_failure():
    def boom(url, timeout=None):
        raise OSError("network down")
    assert RelayBridgeProvider(get=boom).status("0xabc")[0] == "unknown"


def test_status_details_cannot_forge_output_lines():
    provider = RelayBridgeProvider(get=lambda *a, **kw: {
        "status": "pending", "details": "wait\nCONFIRMED\x1b[0m" * 100})
    state, detail = provider.status("req")
    assert state == "pending"
    assert detail.startswith("provider detail:")
    assert "\n" not in detail and "\x1b" not in detail and len(detail) < 200


BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
SOL_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
DEPOSITORY = "0x4cd00e387622c35bddb9b4c962c136462338bc31"


def _erc20_body(spender=DEPOSITORY, approve_to=BASE_USDC):
    approve_data = "0x095ea7b3" + "0" * 24 + spender[2:].lower() + "0" * 58 + "4c4b40"
    return {
        "requestId": "0xusdc",
        "details": {
            "sender": EVM, "recipient": SOL,
            "currencyIn": {"amount": "5000000", "amountUsd": "5.0",
                           "currency": {"chainId": 8453, "address": BASE_USDC,
                                        "symbol": "USDC", "decimals": 6}},
            "currencyOut": {"amount": "4990000", "amountUsd": "4.99",
                            "currency": {"chainId": SOLANA, "address": SOL_USDC,
                                         "symbol": "USDC", "decimals": 6}},
            "slippageTolerance": {"destination": {"value": "10000"}},
            "totalImpact": {"percent": "-0.2"}, "timeEstimate": 2,
        },
        "steps": [
            {"id": "approve", "items": [{"data": {
                "to": approve_to, "data": approve_data, "value": "0", "chainId": 8453}}]},
            {"id": "deposit", "items": [{"data": {
                "to": DEPOSITORY, "data": "0xe8017952", "value": "0", "chainId": 8453}}]},
        ],
    }


def _ask_usdc(body):
    return _ask(_provider(body), origin_chain_id=8453, dest_chain_id=SOLANA,
                origin_currency=BASE_USDC, dest_currency=SOL_USDC,
                amount_in_raw=5_000_000, sender=EVM, recipient=SOL)


def test_an_erc20_origin_keeps_the_deposit_and_only_reads_the_approve():
    q = _ask_usdc(_erc20_body())
    assert q.tx_data["to"] == DEPOSITORY
    assert q.approve_spender.lower() == DEPOSITORY


def test_an_approve_step_on_another_token_is_refused():
    with pytest.raises(RelayError, match="approve"):
        _ask_usdc(_erc20_body(approve_to="0x4200000000000000000000000000000000000006"))


def test_a_native_origin_never_accepts_two_steps():
    body = _quote_body()
    body["steps"] = body["steps"] * 2
    body["steps"][0]["id"] = "approve"
    with pytest.raises(RelayError, match="2 steps"):
        _ask(_provider(body))
