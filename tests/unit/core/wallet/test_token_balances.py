"""Batched balanceOf: a reverting sub-call is UNKNOWN, never zero.

Inside a Multicall3 aggregate3, a failed sub-call returns empty data that
naively decodes to 0. That is the same confident-zero defect fixed in _rpc,
one layer up: a token that reverts on balanceOf would read as "you hold none of
it". Unknown must stay unknown.
"""
import core.wallet.onchain as onchain

HOLDER = "0x2222222222222222222222222222222222222222"
A = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
B = "0x4200000000000000000000000000000000000006"


def _pairs(pairs):
    """Encode aggregate3's (bool success, bytes returnData)[] return value."""
    return onchain._encode_aggregate3_result(pairs)


def test_reverting_subcall_is_unknown_not_zero(monkeypatch):
    monkeypatch.setattr(onchain, "_rpc",
                        lambda *a, **k: _pairs([(True, 5), (False, None)]))
    out = onchain.token_balances(HOLDER, "base", [A, B])
    assert out[A] == 5
    assert out[B] is None, "a reverting balanceOf must be unknown, never 0"


def test_genuine_zero_stays_zero(monkeypatch):
    monkeypatch.setattr(onchain, "_rpc",
                        lambda *a, **k: _pairs([(True, 0), (True, 7)]))
    out = onchain.token_balances(HOLDER, "base", [A, B])
    assert out[A] == 0, "a successful call returning 0 is a real zero"
    assert out[B] == 7


def test_whole_call_failure_yields_all_unknown(monkeypatch):
    def boom(*a, **k):
        raise onchain.RpcError("rpc down")

    monkeypatch.setattr(onchain, "_rpc", boom)
    out = onchain.token_balances(HOLDER, "base", [A, B])
    assert out == {A: None, B: None}


def test_short_returndata_is_unknown(monkeypatch):
    """success=True but truncated data is not a balance."""
    monkeypatch.setattr(onchain, "_rpc",
                        lambda *a, **k: _pairs([(True, None)]))
    out = onchain.token_balances(HOLDER, "base", [A])
    assert out[A] is None


def test_empty_token_list_short_circuits(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not call the RPC for an empty list")

    monkeypatch.setattr(onchain, "_rpc", boom)
    assert onchain.token_balances(HOLDER, "base", []) == {}


def test_addresses_are_checksummed_in_the_result(monkeypatch):
    monkeypatch.setattr(onchain, "_rpc", lambda *a, **k: _pairs([(True, 1)]))
    out = onchain.token_balances(HOLDER, "base", [A.lower()])
    assert A in out, "keys are checksummed regardless of input casing"


def test_unknown_chain_yields_all_unknown(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not call the RPC for an unknown chain")

    monkeypatch.setattr(onchain, "_rpc", boom)
    assert onchain.token_balances(HOLDER, "nosuchchain", [A]) == {A: None}
