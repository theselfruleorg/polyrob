"""The injected wallet's Python half (042) — where the policy actually is.

Nearly every test is a refusal, for the same reason the tx_guard suite is: a
wallet inside a page we do not control is only as good as what it says no to.
"""
import contextlib
import json

import pytest

from core.wallet.tx_guard import Decision
from tools.dapp_browser import bridge as B

HOLDER = "0xcAda546f6A6ddDE31B71aB21eF63d3EBF09Fa553"
POOL = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
OTHER = "0x1111111111111111111111111111111111111111"


class _Gate:
    """Faithful to PolicyGate: `reserve()` is a REAL lock.

    A stub that yields without locking makes every concurrency test pass
    vacuously — a stub that does not match its subject tests the stub (the
    lesson the bridge CLI's `.content`-vs-`.extracted_content` bug taught).
    """

    def __init__(self):
        self.recorded = []
        self._lock = None

    @contextlib.asynccontextmanager
    async def reserve(self):
        import asyncio
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            yield

    def record(self, **kw):
        self.recorded.append(kw)


class _Signer:
    address = HOLDER


class _Wallet:
    def __init__(self, gate):
        self.policy = gate

    def operational_signer(self):
        return _Signer()


class _Rail:
    last = None

    def __init__(self, chain, signer, **kw):
        self.sent = False
        self.built = None
        _Rail.last = self

    def build_call(self, *, to, data, value=0):
        self.built = {"to": to, "data": data, "value": value, "nonce": 1}
        return self.built

    def size_gas(self, tx, sim_gas_used):
        return tx

    def sign_and_send(self, tx):
        self.sent = True
        return "0x" + "ee" * 32

    def await_receipt(self, tx_hash, **kw):
        from core.wallet.broadcast.evm import Receipt
        return Receipt(tx_hash=tx_hash, status="success", block_number=3)


def _bridge(*, allow=True, lane="autonomous", budget=100.0, per_tx=50.0,
            allow_contracts=(), approver=None, captured=None, usd=2.0):
    def _guard(intent, tx, **kw):
        if captured is not None:
            captured.append(intent)
        return Decision(allowed=allow, reason="test", lane=lane,
                        amount_usd=usd, sim_gas_used=None)

    envelope = B.Envelope(chain="base", max_spend_usd=per_tx,
                          session_budget_usd=budget,
                          allow_contracts=tuple(allow_contracts),
                          approval_timeout_sec=0.5)
    gate = _Gate()
    return B.WalletBridge(
        envelope=envelope, wallet=_Wallet(gate), execution_context=None,
        rail_factory=_Rail, guard_fn=_guard, price_fn=lambda c, a: 2500.0,
        rpc_fn=lambda chain, method, params: "0xdeadbeef",
        approver=approver), gate


async def _ask(bridge, method, params=None):
    raw = await bridge.handle(None, json.dumps({"method": method,
                                                "params": params or []}))
    return json.loads(raw)


# ==========================================================================
# Identity and reads
# ==========================================================================

@pytest.mark.asyncio
async def test_the_page_is_told_the_agents_address():
    bridge, _ = _bridge()
    assert (await _ask(bridge, "eth_requestAccounts"))["result"] == [HOLDER]
    assert (await _ask(bridge, "eth_accounts"))["result"] == [HOLDER]


@pytest.mark.asyncio
async def test_chain_id_comes_from_the_registry_not_the_page():
    bridge, _ = _bridge()
    assert (await _ask(bridge, "eth_chainId"))["result"] == hex(8453)


@pytest.mark.asyncio
async def test_a_read_is_forwarded_to_the_pinned_rpc():
    bridge, _ = _bridge()
    assert (await _ask(bridge, "eth_call", [{"to": POOL, "data": "0x01"}]))[
        "result"] == "0xdeadbeef"


@pytest.mark.asyncio
async def test_an_unknown_method_is_refused_not_tunnelled():
    """An allowlist, not a denylist: the cost of refusing an unvetted method is
    a dapp that does not work; the cost of the reverse is a method that moves
    something."""
    out = await _ask(bridge=_bridge()[0], method="debug_traceCall")
    assert out["error"]["code"] == B.UNSUPPORTED_METHOD


# ==========================================================================
# Signatures — always refused
# ==========================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("method", sorted(B.SIGN_METHODS))
async def test_every_signing_method_is_refused(method):
    """A signature is not a transaction: a permit is submitted by someone else
    later, so no simulation can catch what it authorizes."""
    bridge, _ = _bridge()
    out = await _ask(bridge, method, [HOLDER, "0x00"])
    assert out["error"]["code"] == B.UNSUPPORTED_METHOD
    assert "off-chain signatures" in out["error"]["message"]
    assert bridge.envelope.refused


# ==========================================================================
# Chain switching
# ==========================================================================

@pytest.mark.asyncio
async def test_switching_to_the_armed_chain_is_a_no_op():
    bridge, _ = _bridge()
    out = await _ask(bridge, "wallet_switchEthereumChain", [{"chainId": "0x2105"}])
    assert out["chainId"] == hex(8453)


@pytest.mark.asyncio
async def test_a_page_cannot_move_the_budget_to_another_chain():
    """Even a perfectly money-capable chain: the budget was authorized FOR a
    chain, and moving it would spend an authorization never given."""
    bridge, _ = _bridge()
    out = await _ask(bridge, "wallet_switchEthereumChain", [{"chainId": "0x1"}])
    assert out["error"]["code"] == B.CHAIN_NOT_ADDED
    assert "re-arm with dapp_connect" in out["error"]["message"]


@pytest.mark.asyncio
async def test_a_page_cannot_ADD_a_chain():
    bridge, _ = _bridge()
    out = await _ask(bridge, "wallet_addEthereumChain", [{"chainId": "0x99"}])
    assert out["error"]["code"] == B.UNSUPPORTED_METHOD


# ==========================================================================
# eth_sendTransaction
# ==========================================================================

@pytest.mark.asyncio
async def test_an_authorized_transaction_is_signed_and_recorded():
    bridge, gate = _bridge()
    out = await _ask(bridge, "eth_sendTransaction",
                     [{"to": POOL, "data": "0xdeadbeef", "value": "0x0"}])
    assert out["result"] == "0x" + "ee" * 32
    assert gate.recorded[0]["action"] == "dapp_call"
    assert bridge.envelope.spent_usd == 2.0


@pytest.mark.asyncio
async def test_a_guard_refusal_reaches_the_page_as_a_clean_rejection():
    _Rail.last = None
    bridge, _ = _bridge(allow=False, lane="refuse")
    out = await _ask(bridge, "eth_sendTransaction", [{"to": POOL, "data": "0x01"}])
    assert out["error"]["code"] == B.USER_REJECTED
    assert _Rail.last.sent is False


@pytest.mark.asyncio
async def test_the_page_cannot_deploy_a_contract():
    bridge, _ = _bridge()
    out = await _ask(bridge, "eth_sendTransaction", [{"data": "0x6080"}])
    assert "will not deploy a contract from a page" in out["error"]["message"]


@pytest.mark.asyncio
async def test_a_contract_outside_the_allowlist_is_refused():
    bridge, _ = _bridge(allow_contracts=(POOL,))
    out = await _ask(bridge, "eth_sendTransaction", [{"to": OTHER, "data": "0x01"}])
    assert "may only transact with" in out["error"]["message"]


@pytest.mark.asyncio
async def test_a_page_cannot_send_from_another_address():
    bridge, _ = _bridge()
    out = await _ask(bridge, "eth_sendTransaction",
                     [{"from": OTHER, "to": POOL, "data": "0x01"}])
    assert "not this wallet" in out["error"]["message"]


@pytest.mark.asyncio
async def test_the_SESSION_budget_bounds_repeated_clicks():
    """A per-transaction ceiling alone is a per-CLICK ceiling, and a page can
    click as often as it likes.

    The budget is bounded by the PRICE of the next transaction, not merely by
    "is anything left": a $5 budget with $2 tickets buys two, not three. The
    weaker `remaining <= 0` test let the third through and closed at $6.
    """
    bridge, _ = _bridge(budget=5.0, usd=2.0)
    for _ in range(4):
        await _ask(bridge, "eth_sendTransaction", [{"to": POOL, "data": "0x01"}])
    assert len(bridge.envelope.sent) == 2
    assert bridge.envelope.spent_usd == 4.0
    assert bridge.envelope.spent_usd <= bridge.envelope.session_budget_usd
    assert any(r["kind"] == "budget-exhausted" for r in bridge.envelope.refused)


@pytest.mark.asyncio
async def test_an_exhausted_budget_says_so_plainly():
    bridge, _ = _bridge(budget=2.0, usd=2.0)
    await _ask(bridge, "eth_sendTransaction", [{"to": POOL, "data": "0x01"}])
    out = await _ask(bridge, "eth_sendTransaction", [{"to": POOL, "data": "0x01"}])
    assert out["error"]["code"] == B.USER_REJECTED
    assert "budget is spent" in out["error"]["message"]


@pytest.mark.asyncio
async def test_two_CONCURRENT_page_requests_cannot_overspend_the_budget():
    """The page controls the timing, and the owner-queue await is a real yield
    point mid-transaction. With the budget read OUTSIDE gate.reserve(), two
    in-flight requests both pass a nearly-exhausted budget and both spend
    against it — the same class as the M4 cap race the lock was added for.

    The awaiting approver is what makes this a genuine interleave rather than
    two coroutines that never yield.
    """
    import asyncio

    class _SlowApprover:
        async def request(self, *a, **kw):
            await asyncio.sleep(0)          # a real suspension point
            return True

    bridge, _ = _bridge(budget=2.0, per_tx=50.0, usd=2.0,
                        allow=False, lane="owner_queue", approver=_SlowApprover())
    await asyncio.gather(*[
        _ask(bridge, "eth_sendTransaction", [{"to": POOL, "data": "0x01"}])
        for _ in range(4)])
    assert len(bridge.envelope.sent) == 1, (
        f"budget $2 at $2/tx allowed {len(bridge.envelope.sent)} sends")
    assert bridge.envelope.spent_usd == 2.0
    assert any(r["kind"] == "budget-exhausted" for r in bridge.envelope.refused)


@pytest.mark.asyncio
async def test_the_declared_ceiling_is_the_SMALLER_of_per_tx_and_remaining():
    captured = []
    bridge, _ = _bridge(per_tx=50.0, budget=6.0, usd=2.0, captured=captured)
    await _ask(bridge, "eth_sendTransaction", [{"to": POOL, "data": "0x01"}])
    await _ask(bridge, "eth_sendTransaction", [{"to": POOL, "data": "0x01"}])
    assert captured[0].max_spend_usd == 6.0
    assert captured[1].max_spend_usd == 4.0


@pytest.mark.asyncio
async def test_a_page_transaction_declares_a_NATIVE_outflow_only():
    """Any token movement is UNDECLARED and tx_guard refuses it — the right
    default for a dapp we are exploring."""
    captured = []
    bridge, _ = _bridge(captured=captured)
    await _ask(bridge, "eth_sendTransaction",
               [{"to": POOL, "data": "0x01", "value": "0xde0b6b3a7640000"}])
    intent = captured[0]
    assert intent.token is None
    assert intent.amount_raw == 10 ** 18


@pytest.mark.asyncio
async def test_a_revoked_session_refuses_everything_that_matters():
    bridge, _ = _bridge()
    bridge.envelope.revoked = True
    out = await _ask(bridge, "eth_sendTransaction", [{"to": POOL, "data": "0x01"}])
    assert out["error"]["code"] == B.UNAUTHORIZED
    assert (await _ask(bridge, "eth_accounts"))["error"]["code"] == B.UNAUTHORIZED


@pytest.mark.asyncio
async def test_malformed_calldata_is_refused_not_coerced():
    bridge, _ = _bridge()
    out = await _ask(bridge, "eth_sendTransaction", [{"to": POOL, "data": "oops"}])
    assert "0x-prefixed hex" in out["error"]["message"]


@pytest.mark.asyncio
async def test_the_bridge_never_raises_into_the_page():
    bridge, _ = _bridge()
    out = json.loads(await bridge.handle(None, "not json at all"))
    assert "error" in out


# ==========================================================================
# The owner queue
# ==========================================================================

class _Approver:
    def __init__(self, answer):
        self.answer = answer
        self.asked = []

    async def request(self, name, summary, ctx, hash_params=None):
        self.asked.append((name, summary))
        return self.answer


@pytest.mark.asyncio
async def test_an_above_ceiling_transaction_waits_for_the_owner_and_proceeds():
    approver = _Approver(True)
    bridge, gate = _bridge(allow=False, lane="owner_queue", approver=approver)
    out = await _ask(bridge, "eth_sendTransaction", [{"to": POOL, "data": "0x01"}])
    assert out["result"] == "0x" + "ee" * 32
    assert approver.asked[0][0] == "dapp_browser_dapp_connect"


@pytest.mark.asyncio
async def test_an_owner_decline_reaches_the_page_as_a_rejection():
    _Rail.last = None
    bridge, _ = _bridge(allow=False, lane="owner_queue", approver=_Approver(False))
    out = await _ask(bridge, "eth_sendTransaction", [{"to": POOL, "data": "0x01"}])
    assert out["error"]["code"] == B.USER_REJECTED
    assert "did not approve it" in out["error"]["message"]
    assert _Rail.last.sent is False


@pytest.mark.asyncio
async def test_a_slow_owner_times_out_rather_than_hanging_the_page():
    class _Slow:
        async def request(self, *a, **kw):
            import asyncio
            await asyncio.sleep(5)
            return True

    bridge, _ = _bridge(allow=False, lane="owner_queue", approver=_Slow())
    out = await _ask(bridge, "eth_sendTransaction", [{"to": POOL, "data": "0x01"}])
    assert out["error"]["code"] == B.USER_REJECTED


# ==========================================================================
# The owner wait must not freeze the wallet
# ==========================================================================

@pytest.mark.asyncio
async def test_the_owner_wait_does_NOT_hold_the_wallet_lock():
    """`gate.reserve()` is the wallet's ONE process-wide reservation. Holding it
    across an owner tap — which may take an hour — blocks EVERY other money
    verb, including a stop-loss exit on a position sliding down. A page would
    then be able to freeze the whole treasury's ability to act.
    """
    import asyncio

    started = asyncio.Event()
    release = asyncio.Event()

    class _SlowApprover:
        async def request(self, *a, **kw):
            started.set()
            await release.wait()
            return True

    bridge, gate = _bridge(allow=False, lane="owner_queue",
                           approver=_SlowApprover())
    bridge.envelope.approval_timeout_sec = 30

    send = asyncio.create_task(
        _ask(bridge, "eth_sendTransaction", [{"to": POOL, "data": "0x01"}]))
    await asyncio.wait_for(started.wait(), timeout=2)

    # THE ASSERTION: another money verb can take the same gate while the owner
    # is still being asked.
    async def _other_money_verb():
        async with gate.reserve():
            return "the exit ran"

    assert await asyncio.wait_for(_other_money_verb(), timeout=2) == "the exit ran"

    release.set()
    assert (await send)["result"] == "0x" + "ee" * 32   # _ask already decodes


@pytest.mark.asyncio
async def test_a_reprice_above_what_the_owner_APPROVED_is_refused():
    """He lifted the ceiling for a price. A re-simulation minutes later that
    costs materially more is not the transaction he agreed to."""
    prices = iter([2.0, 9.0])

    def _guard(intent, tx, **kw):
        return Decision(allowed=False, reason="above the ceiling",
                        lane="owner_queue", amount_usd=next(prices))

    envelope = B.Envelope(chain="base", max_spend_usd=50.0,
                          session_budget_usd=100.0, approval_timeout_sec=5)
    gate = _Gate()
    bridge = B.WalletBridge(
        envelope=envelope, wallet=_Wallet(gate), execution_context=None,
        rail_factory=_Rail, guard_fn=_guard, price_fn=lambda c, a: 2500.0,
        rpc_fn=lambda chain, method, params: "0x",
        approver=_Approver(True))
    _Rail.last = None
    out = await _ask(bridge, "eth_sendTransaction", [{"to": POOL, "data": "0x01"}])
    assert "not what he approved" in out["error"]["message"]
    assert _Rail.last.sent is False


@pytest.mark.asyncio
async def test_a_reprice_WITHIN_tolerance_proceeds():
    prices = iter([2.0, 2.1])

    def _guard(intent, tx, **kw):
        return Decision(allowed=False, reason="above the ceiling",
                        lane="owner_queue", amount_usd=next(prices))

    envelope = B.Envelope(chain="base", max_spend_usd=50.0,
                          session_budget_usd=100.0, approval_timeout_sec=5)
    gate = _Gate()
    bridge = B.WalletBridge(
        envelope=envelope, wallet=_Wallet(gate), execution_context=None,
        rail_factory=_Rail, guard_fn=_guard, price_fn=lambda c, a: 2500.0,
        rpc_fn=lambda chain, method, params: "0x",
        approver=_Approver(True))
    out = await _ask(bridge, "eth_sendTransaction", [{"to": POOL, "data": "0x01"}])
    assert out["result"] == "0x" + "ee" * 32
    assert bridge.envelope.spent_usd == 2.1


@pytest.mark.asyncio
async def test_a_guard_refusal_AFTER_approval_still_refuses():
    """Approval lifts the CEILING. It does not wave through a simulation that
    has since started disagreeing with the declaration."""
    decisions = iter([
        Decision(allowed=False, reason="above the ceiling", lane="owner_queue",
                 amount_usd=2.0),
        Decision(allowed=False, reason="refused: UNDECLARED allowance grant",
                 lane="refuse"),
    ])

    envelope = B.Envelope(chain="base", max_spend_usd=50.0,
                          session_budget_usd=100.0, approval_timeout_sec=5)
    gate = _Gate()
    bridge = B.WalletBridge(
        envelope=envelope, wallet=_Wallet(gate), execution_context=None,
        rail_factory=_Rail, guard_fn=lambda i, t, **k: next(decisions),
        price_fn=lambda c, a: 2500.0,
        rpc_fn=lambda chain, method, params: "0x", approver=_Approver(True))
    _Rail.last = None
    out = await _ask(bridge, "eth_sendTransaction", [{"to": POOL, "data": "0x01"}])
    assert "UNDECLARED allowance grant" in out["error"]["message"]
    assert _Rail.last.sent is False
