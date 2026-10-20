"""Solana balance reads. Phase 2 — reads only, no rail.

Same honesty contract as the EVM reader: a failed read is UNKNOWN (``None``),
never a confident zero. On Solana that distinction is sharper than on EVM,
because an address with no token account is genuinely different from an address
whose RPC call failed, and only one of those means "you hold none".
"""
import pytest

from core.wallet import solana_onchain as so

ME = "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def test_lamports_convert_to_sol():
    assert so.lamports_to_sol(1_000_000_000) == 1.0
    assert so.lamports_to_sol(0) == 0.0
    assert so.lamports_to_sol(None) is None


def test_a_failed_native_read_is_unknown_not_zero():
    def _rpc(method, params):
        raise RuntimeError("rpc down")
    assert so.native_balance(ME, rpc=_rpc) is None


def test_a_genuine_zero_native_balance_is_zero():
    assert so.native_balance(ME, rpc=lambda m, p: {"value": 0}) == 0.0


def test_a_native_balance_is_returned_in_sol():
    assert so.native_balance(ME, rpc=lambda m, p: {"value": 2_500_000_000}) == 2.5


def _classic_only(payload):
    """An RPC that answers *payload* for the classic token program, nothing for Token-2022."""
    def _rpc(method, params):
        return payload if params[1]["programId"] == so.SPL_TOKEN_PROGRAM else {"value": []}
    return _rpc


def test_token_balances_parse_amount_and_decimals():
    payload = {"value": [{"account": {"data": {"parsed": {"info": {
        "mint": USDC,
        "tokenAmount": {"amount": "11910340", "decimals": 6},
    }}}}}]}
    out = so.token_balances(ME, rpc=_classic_only(payload))
    assert out == {USDC: 11_910_340}


def test_a_failed_token_read_is_unknown_not_an_empty_wallet():
    """`{}` would read as 'you hold no tokens', which is a claim we cannot make
    from a failed call."""
    def _rpc(method, params):
        raise RuntimeError("429")
    assert so.token_balances(ME, rpc=_rpc) is None


def test_a_genuinely_empty_wallet_is_an_empty_mapping():
    assert so.token_balances(ME, rpc=lambda m, p: {"value": []}) == {}


def test_a_malformed_account_entry_is_skipped_not_fatal():
    payload = {"value": [
        {"account": {"data": {"parsed": {"info": {"mint": USDC,
            "tokenAmount": {"amount": "5", "decimals": 6}}}}}},
        {"nonsense": True},
        {"account": {"data": {"parsed": {"info": {"mint": "x"}}}}},
    ]}
    assert so.token_balances(ME, rpc=_classic_only(payload)) == {USDC: 5}


def test_zero_balance_token_accounts_are_dropped():
    """A closed-out position leaves an empty ATA behind; reporting it as a
    holding would put a permanent phantom row in the portfolio."""
    payload = {"value": [{"account": {"data": {"parsed": {"info": {
        "mint": USDC, "tokenAmount": {"amount": "0", "decimals": 6}}}}}}]}
    assert so.token_balances(ME, rpc=lambda m, p: payload) == {}


def test_the_rpc_url_is_operator_pinnable(monkeypatch):
    monkeypatch.setenv("DEFI_SOLANA_RPC", "https://pinned.example/sol")
    assert so.rpc_url() == "https://pinned.example/sol"


def test_the_rpc_url_falls_back_to_the_registry(monkeypatch):
    monkeypatch.delenv("DEFI_SOLANA_RPC", raising=False)
    from core.wallet import chains
    assert so.rpc_url() == chains.get("solana").public_rpc


# -- 085 W8 solana L4: Token-2022 holdings are held -------------------------

T22_MINT = "So11111111111111111111111111111111111111112"


def _entry(pubkey, mint, amount):
    return {"pubkey": pubkey, "account": {"data": {"parsed": {"info": {
        "mint": mint, "tokenAmount": {"amount": str(amount), "decimals": 6}}}}}}


def test_token_balances_scans_both_token_programs():
    asked = []

    def _rpc(method, params):
        program = params[1]["programId"]
        asked.append(program)
        if program == so.SPL_TOKEN_PROGRAM:
            return {"value": [_entry("acctA", USDC, 7)]}
        if program == so.TOKEN_2022_PROGRAM:
            return {"value": [_entry("acctB", T22_MINT, 9)]}
        raise AssertionError(program)

    assert so.token_balances(ME, rpc=_rpc) == {USDC: 7, T22_MINT: 9}
    assert asked == [so.SPL_TOKEN_PROGRAM, so.TOKEN_2022_PROGRAM]


def test_a_failed_token_2022_read_is_unknown_not_a_classic_only_wallet():
    def _rpc(method, params):
        if params[1]["programId"] == so.TOKEN_2022_PROGRAM:
            raise RuntimeError("429")
        return {"value": [_entry("acctA", USDC, 7)]}

    assert so.token_balances(ME, rpc=_rpc) is None


def test_one_account_reported_twice_is_counted_once():
    def _rpc(method, params):
        return {"value": [_entry("acctA", USDC, 7)]}

    assert so.token_balances(ME, rpc=_rpc) == {USDC: 7}
