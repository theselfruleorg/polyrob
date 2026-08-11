"""Delta engine — effects measured under eth_simulateV1, never assumed.

⚠️ These tests exist in this exact shape because the first implementation used a
sequence of eth_calls, which do NOT persist state: read balance, run the tx,
read balance again all returned the same number, so every delta was 0. The guard
priced a 0.25 USDC transfer at $0.00 and broadcast it live on 2026-08-09 through
a cap that should have refused it. A bundle is the only way these reads mean
anything, so the fixtures below are eth_simulateV1 bundle responses.
"""
import pytest

from core.wallet import simulation

HOLDER = "0x2222222222222222222222222222222222222222"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
SPENDER = "0x1111111111111111111111111111111111111111"


def _word(n):
    return "0x" + f"{n:064x}"


def _ok(v):
    return {"status": "0x1", "returnData": _word(v)}


def _bundle(*entries):
    return [{"calls": list(entries)}]


def _tx():
    return {"to": USDC, "data": "0xa9059cbb", "value": 0, "chainId": 8453}


def _sim(entries, tokens=(USDC,), spenders=()):
    def rpc(method, params, timeout=8.0):
        assert method == "eth_simulateV1", "must use a state-persisting bundle"
        return entries
    return simulation.simulate(_tx(), holder=HOLDER, chain="base",
                               tokens=list(tokens), spenders=list(spenders), rpc=rpc)


def test_uses_a_state_persisting_bundle_not_eth_call():
    """Guards the original defect directly."""
    seen = []

    def rpc(method, params, timeout=8.0):
        seen.append(method)
        return _bundle(_ok(1000), {"status": "0x1", "returnData": "0x"}, _ok(750))

    simulation.simulate(_tx(), holder=HOLDER, chain="base",
                        tokens=[USDC], spenders=[], rpc=rpc)
    assert seen == ["eth_simulateV1"]
    assert "eth_call" not in seen


def test_token_outflow_is_measured():
    d = _sim(_bundle(_ok(1000), {"status": "0x1", "returnData": "0x"}, _ok(750)))
    assert d.ok is True
    assert d.token_deltas[USDC] == -250


def test_a_reverting_transaction_is_not_ok():
    d = _sim(_bundle(_ok(1000),
                     {"status": "0x0", "error": "execution reverted"},
                     _ok(1000)))
    assert d.ok is False
    assert "revert" in (d.error or "").lower()


def test_unsupported_simulate_method_refuses():
    """No fallback to the vacuous eth_call form, ever."""
    def rpc(method, params, timeout=8.0):
        raise RuntimeError("method eth_simulateV1 does not exist")

    d = simulation.simulate(_tx(), holder=HOLDER, chain="base",
                            tokens=[USDC], spenders=[], rpc=rpc)
    assert d.ok is False
    assert "unavailable" in (d.error or "").lower()


def test_unexpected_shape_refuses():
    d = _sim([{"calls": [_ok(1)]}])
    assert d.ok is False
    assert "shape" in (d.error or "").lower()


def test_failed_read_is_unknown_not_zero():
    d = _sim(_bundle(_ok(1000), {"status": "0x1", "returnData": "0x"},
                     {"status": "0x0", "returnData": "0x"}))
    assert d.ok is False
    assert "unknown" in (d.error or "").lower()


def test_allowance_increase_is_captured():
    # layout: [bal, allow, TX, bal, allow]
    d = _sim(_bundle(_ok(1000), _ok(0),
                     {"status": "0x1", "returnData": "0x"},
                     _ok(1000), _ok(5_000_000)),
             spenders=(SPENDER,))
    assert d.ok is True
    assert d.allowance_deltas[(USDC, SPENDER)] == 5_000_000
    assert d.grants_allowance is True


def test_a_plain_transfer_grants_no_allowance():
    d = _sim(_bundle(_ok(1000), _ok(0),
                     {"status": "0x1", "returnData": "0x"},
                     _ok(750), _ok(0)),
             spenders=(SPENDER,))
    assert d.allowance_deltas[(USDC, SPENDER)] == 0
    assert d.grants_allowance is False
