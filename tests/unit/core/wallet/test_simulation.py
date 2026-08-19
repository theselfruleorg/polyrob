"""Delta engine — effects measured under eth_simulateV1, never assumed.

⚠️ These tests exist in this exact shape because the first implementation used a
sequence of eth_calls, which do NOT persist state: read balance, run the tx,
read balance again all returned the same number, so every delta was 0. The guard
priced a 0.25 USDC transfer at $0.00 and broadcast it live on 2026-08-09 through
a cap that should have refused it. A bundle is the only way these reads mean
anything, so the fixtures below are eth_simulateV1 bundle responses.

Bundle layout (mirrors simulation.simulate):
    [native, bal(token)…, allow(token,spender)…, THE TX, native, bal…, allow…]
The native read is Multicall3.getEthBalance — before it existed, native_delta
was hardcoded 0 and the guard's native-drain assertion could never fire.
"""
import pytest

from core.wallet import simulation

HOLDER = "0x2222222222222222222222222222222222222222"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
SPENDER = "0x1111111111111111111111111111111111111111"

ETH = 10 ** 18


def _word(n):
    return "0x" + f"{n:064x}"


def _ok(v):
    return {"status": "0x1", "returnData": _word(v)}


def _bundle(*entries):
    return [{"calls": list(entries)}]


def _tx(value=0):
    return {"to": USDC, "data": "0xa9059cbb", "value": value, "chainId": 8453}


def _sim(entries, tokens=(USDC,), spenders=(), tx=None):
    def rpc(method, params, timeout=8.0):
        assert method == "eth_simulateV1", "must use a state-persisting bundle"
        return entries
    return simulation.simulate(tx or _tx(), holder=HOLDER, chain="base",
                               tokens=list(tokens), spenders=list(spenders), rpc=rpc)


def test_uses_a_state_persisting_bundle_not_eth_call():
    """Guards the original defect directly."""
    seen = []

    def rpc(method, params, timeout=8.0):
        seen.append(method)
        return _bundle(_ok(ETH), _ok(1000),
                       {"status": "0x1", "returnData": "0x", "logs": []},
                       _ok(ETH), _ok(750))

    simulation.simulate(_tx(), holder=HOLDER, chain="base",
                        tokens=[USDC], spenders=[], rpc=rpc)
    assert seen == ["eth_simulateV1"]
    assert "eth_call" not in seen


def test_token_outflow_is_measured():
    d = _sim(_bundle(_ok(ETH), _ok(1000),
                     {"status": "0x1", "returnData": "0x", "logs": []},
                     _ok(ETH), _ok(750)))
    assert d.ok is True
    assert d.token_deltas[USDC] == -250


def test_native_outflow_is_measured_not_assumed_zero():
    """native_delta was hardcoded 0, so tx_guard's 'unexpected native balance
    change' refusal was dead code — a tx draining ETH read as clean. The
    bundle now measures the holder's native balance around the tx."""
    d = _sim(_bundle(_ok(ETH), _ok(1000),
                     {"status": "0x1", "returnData": "0x", "logs": []},
                     _ok(ETH // 2), _ok(750)))
    assert d.ok is True
    assert d.native_delta == -(ETH - ETH // 2)


def test_bundle_reads_native_balance_via_multicall3():
    """The native read must target the pinned Multicall3 aggregator with
    getEthBalance(holder) — eth_getBalance cannot appear inside a bundle."""
    captured = {}

    def rpc(method, params, timeout=8.0):
        captured["calls"] = params[0]["blockStateCalls"][0]["calls"]
        return _bundle(_ok(ETH), _ok(1000),
                       {"status": "0x1", "returnData": "0x", "logs": []},
                       _ok(ETH), _ok(1000))

    simulation.simulate(_tx(), holder=HOLDER, chain="base",
                        tokens=[USDC], spenders=[], rpc=rpc)
    from core.wallet.onchain import MULTICALL3
    first = captured["calls"][0]
    assert first["to"] == MULTICALL3
    assert first["data"].startswith("0x4d2301cc")          # getEthBalance(address)
    assert HOLDER[2:].lower() in first["data"]


def test_unreadable_native_balance_refuses():
    d = _sim(_bundle({"status": "0x0", "returnData": "0x"}, _ok(1000),
                     {"status": "0x1", "returnData": "0x", "logs": []},
                     _ok(ETH), _ok(750)))
    assert d.ok is False
    assert "unknown" in (d.error or "").lower()


def test_a_reverting_transaction_is_not_ok():
    d = _sim(_bundle(_ok(ETH), _ok(1000),
                     {"status": "0x0", "error": "execution reverted"},
                     _ok(ETH), _ok(1000)))
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
    d = _sim(_bundle(_ok(ETH), _ok(1000),
                     {"status": "0x1", "returnData": "0x", "logs": []},
                     _ok(ETH), {"status": "0x0", "returnData": "0x"}))
    assert d.ok is False
    assert "unknown" in (d.error or "").lower()


def test_allowance_increase_is_captured():
    # layout: [native, bal, allow, TX, native, bal, allow]
    d = _sim(_bundle(_ok(ETH), _ok(1000), _ok(0),
                     {"status": "0x1", "returnData": "0x", "logs": []},
                     _ok(ETH), _ok(1000), _ok(5_000_000)),
             spenders=(SPENDER,))
    assert d.ok is True
    assert d.allowance_deltas[(USDC, SPENDER)] == 5_000_000
    assert d.grants_allowance is True


def test_a_plain_transfer_grants_no_allowance():
    d = _sim(_bundle(_ok(ETH), _ok(1000), _ok(0),
                     {"status": "0x1", "returnData": "0x", "logs": []},
                     _ok(ETH), _ok(750), _ok(0)),
             spenders=(SPENDER,))
    assert d.allowance_deltas[(USDC, SPENDER)] == 0
    assert d.grants_allowance is False


def test_default_transport_resolves_the_pinned_endpoint_for_the_chain(monkeypatch):
    """The default transport hardcoded "base"; it now resolves the endpoint
    for the chain being simulated, honoring the operator pin."""
    seen = {}

    def fake_rpc(url, method, params, timeout=8.0):
        seen["url"] = url
        raise RuntimeError("stop after capturing the endpoint")

    monkeypatch.setattr(simulation.onchain, "_rpc", fake_rpc)
    monkeypatch.setenv("DEFI_EVM_RPC_BASE", "https://pinned.example/rpc")
    d = simulation.simulate(_tx(), holder=HOLDER, chain="base",
                            tokens=[], spenders=[])
    assert seen["url"] == "https://pinned.example/rpc"
    assert d.ok is False   # the transport raised; refusal, never a fallback


# --- gas measurement --------------------------------------------------------
# The rail sizes the broadcast gas limit from the simulation's gasUsed (§3a,
# 2026-08-15): a fixed limit out-of-gas-reverts a swap on-chain and burns the
# fee. The measurement must therefore leave the delta engine with the deltas.

def test_gas_used_is_captured_from_the_tx_entry():
    d = _sim(_bundle(_ok(ETH), _ok(1000),
                     {"status": "0x1", "returnData": "0x", "logs": [],
                      "gasUsed": hex(150_000)},
                     _ok(ETH), _ok(750)))
    assert d.ok is True
    assert d.gas_used == 150_000


def test_missing_gas_used_reads_none_not_zero():
    """No measurement is None — a zero would size a 21k limit and burn gas."""
    d = _sim(_bundle(_ok(ETH), _ok(1000),
                     {"status": "0x1", "returnData": "0x", "logs": []},
                     _ok(ETH), _ok(750)))
    assert d.ok is True
    assert d.gas_used is None


# --- event-log extraction ---------------------------------------------------
# The balance/allowance READS only cover declared tokens/spenders. The tx's
# own event log is what covers an approve to an undeclared spender or a drain
# of an undeclared token (2026-08-14 T4 review).

OTHER_TOKEN = "0x4444444444444444444444444444444444444444"
_T_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_T_APPROVAL = "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"


def _topic_addr(a):
    return "0x" + a[2:].lower().rjust(64, "0")


def _log(topic0, contract, frm, to, amount):
    return {"address": contract, "data": _word(amount),
            "topics": [topic0, _topic_addr(frm), _topic_addr(to)]}


def test_holder_events_are_extracted_from_the_tx_log():
    tx_entry = {"status": "0x1", "returnData": "0x", "logs": [
        _log(_T_TRANSFER, OTHER_TOKEN, HOLDER, SPENDER, 777),
        _log(_T_APPROVAL, USDC, HOLDER, SPENDER, 5_000_000),
        # Someone ELSE's events must be ignored — only owner/from == holder.
        _log(_T_TRANSFER, USDC, SPENDER, HOLDER, 123),
        _log(_T_APPROVAL, USDC, SPENDER, HOLDER, 456),
    ]}
    d = _sim(_bundle(_ok(ETH), _ok(1000), tx_entry, _ok(ETH), _ok(1000)))
    assert d.ok is True
    assert d.holder_transfers == ((OTHER_TOKEN.lower(), SPENDER.lower(), 777),)
    assert d.holder_approvals == ((USDC.lower(), SPENDER.lower(), 5_000_000),)


def test_a_missing_log_set_refuses():
    """"No logs key" and "no hidden approve" are not the same thing."""
    d = _sim(_bundle(_ok(ETH), _ok(1000),
                     {"status": "0x1", "returnData": "0x"},   # no "logs"
                     _ok(ETH), _ok(1000)))
    assert d.ok is False
    assert "log" in (d.error or "").lower()


def test_a_malformed_log_is_skipped_not_fatal():
    tx_entry = {"status": "0x1", "returnData": "0x", "logs": [
        {"address": USDC, "topics": ["0xdead"], "data": "garbage"},
        {"topics": None},
    ]}
    d = _sim(_bundle(_ok(ETH), _ok(1000), tx_entry, _ok(ETH), _ok(1000)))
    assert d.ok is True
    assert d.holder_transfers == ()
    assert d.holder_approvals == ()
