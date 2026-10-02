"""CR-M01: the approval card shows every money-relevant field of every money
verb. The grant hashes the FULL params, so a field the card drops is a field the
owner approves blind."""
import hashlib

import pytest

from tools.controller.grant_card import render_grant_card
from tools.defi.trade_tool import (BridgeParams, CallParams,
                                   DeployContractParams, LpAddParams,
                                   SwapParams)

A = "0x" + "11" * 20
B = "0x" + "22" * 20
SPENDER = "0x" + "33" * 20
WORDS = ["00" * 31 + f"{i:02x}" for i in range(30)]
CALLDATA = "0xa9059cbb" + "".join(WORDS)

CASES = [
    ("defi_trade_call", CallParams(
        chain="base", to=A, calldata=CALLDATA, value=0.25, spend_token=B,
        spend_max_raw=123456789, receive_token=A, receive_min_raw=987654,
        allow_spender=SPENDER, allow_max_raw=555555, max_spend_usd=42.5,
        dry_run=False)),
    ("defi_trade_lp_add", LpAddParams(
        chain="robinhood", token_a=A, token_b=B, amount_a=1.75, amount_b=3200.5,
        fee=500, range="1500,2500", initial_price=1800.0, slippage_bps=77,
        max_spend_usd=99.0, dry_run=False)),
    ("defi_trade_deploy_contract", DeployContractParams(
        chain="base", bytecode="0x6080" + "ab" * 200,
        constructor_args="".join(WORDS[:3]), value=0.5, max_spend_usd=12.0,
        salt="beef", dry_run=False)),
    ("defi_trade_bridge", BridgeParams(
        from_chain="solana", to_chain="robinhood", amount=0.036,
        token_out="usdc", dry_run=False)),
    ("defi_trade_swap", SwapParams(
        chain="base", token_in=A, token_out=B, amount_in=1.5,
        max_spend_usd=20.0, slippage_bps=150, dry_run=False)),
]

HEX_KEYS = {"calldata", "bytecode", "constructor_args"}


@pytest.mark.parametrize("action,model", CASES, ids=[c[0] for c in CASES])
def test_no_money_verb_field_is_dropped(action, model):
    params = model.model_dump()
    card = render_grant_card(action, params, "tap-1", timeout_sec=300)
    for key, value in params.items():
        if value in (None, ""):
            continue
        if key in HEX_KEYS:
            body = str(value)[2:] if str(value).startswith("0x") else str(value)
            digest = hashlib.sha256(bytes.fromhex(body)).hexdigest()
            assert digest in card, f"{action}: no sha256 for {key}"
            continue
        assert str(value) in card, f"{action}: {key}={value!r} dropped:\n{card}"
    assert "max_spend_usd" in params or action == "defi_trade_bridge"


def test_calldata_renders_selector_decoded_words_and_more_marker():
    card = render_grant_card("defi_trade_call", {
        "to": A, "calldata": CALLDATA, "max_spend_usd": 5}, "tap-2")
    assert "selector 0xa9059cbb" in card
    for w in WORDS[:24]:
        assert w in card
    assert "+6 more word(s)" in card
    assert hashlib.sha256(bytes.fromhex(CALLDATA[2:])).hexdigest() in card


def test_extras_past_the_cap_carry_a_more_marker():
    params = {f"k{i}": i + 1 for i in range(12)}
    card = render_grant_card("t", params, "tap-3")
    assert "+4 more param(s)" in card


def test_truncated_value_names_how_much_is_hidden():
    card = render_grant_card("shell_run", {"command": "x" * 500}, "tap-4")
    assert "(+441 chars)" in card
