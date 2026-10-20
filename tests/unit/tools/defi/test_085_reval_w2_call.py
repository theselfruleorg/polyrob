"""085 W2 revalidation at the call verb: the ERC-20 payee decode binds the
address the token would really pay, a too-short transfer refuses, and the
owner's approval card names the payee of a payout-shaped call."""
import pytest

from tests.unit.tools.defi.test_085_w2_payout_verbs import (  # noqa: F401 (fixture)
    HOLDER, PAYEE, TARGET, TOKEN, TOKEN_OUT, _env, _tool, _transfer_data)
from tools.defi.trade_tool import CallParams


def test_a_dirty_address_word_decodes_to_the_low_20_bytes():
    from tools.defi.call_verb import _erc20_payee
    raw = bytes.fromhex("a9059cbb" + "ff" * 12 + PAYEE[2:] + "00" * 32)
    assert _erc20_payee(raw) == PAYEE.lower()


@pytest.mark.asyncio
async def test_a_too_short_transfer_on_the_spend_token_refuses():
    captured = []
    res = await _tool(captured).call(CallParams(
        chain="base", to=TOKEN, calldata="0xa9059cbb" + "0" * 24 + PAYEE[2:].lower(),
        spend_token=TOKEN, spend_max_raw=1, max_spend_usd=1.0))
    assert "too short" in (res.error or "")
    assert captured == []


def test_the_approval_card_names_the_erc20_payee():
    from tools.controller.grant_card import card_context_lines
    lines = card_context_lines("defi_trade_call", {
        "chain": "base", "to": TOKEN, "calldata": _transfer_data(PAYEE, 5),
        "spend_token": TOKEN, "spend_max_raw": 5, "max_spend_usd": 1.0})
    assert len(lines) == 1 and "PAYOUT" in lines[0] and PAYEE.lower() in lines[0]


def test_the_approval_card_names_the_native_target():
    from tools.controller.grant_card import card_context_lines
    lines = card_context_lines("defi_trade_call", {
        "chain": "base", "to": PAYEE, "calldata": "0xdeadbeef", "value": 0.005,
        "max_spend_usd": 25.0})
    assert len(lines) == 1 and PAYEE in lines[0]


def test_a_fair_inflow_call_card_carries_no_payout_line(monkeypatch):
    from core.config_policy import spend_lane
    from tools.controller.grant_card import card_context_lines
    usdc, weth = ("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                  "0x4200000000000000000000000000000000000006")
    monkeypatch.setattr(spend_lane, "_SPEND_PRICE", lambda c, a: (
        {usdc.lower(): 1.0, weth.lower(): 2500.0}.get(a.lower())))
    assert card_context_lines("defi_trade_call", {
        "chain": "base", "to": TARGET, "calldata": "0x12345678", "value": 0.004,
        "receive_token": usdc, "receive_min_raw": 9_000_000, "max_spend_usd": 11.0}) == []
