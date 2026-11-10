"""The verb call sites of ``book_after_receipt`` on a real SpendLedger.

Each receipt shape a money verb can see after its wait books one way:
success counts against the caps; ``pending`` is left open for the receipt
resolver; ``failed`` is voided at once. A swap writes its position only on a
succeeded receipt, and stores it (MW-5) for the resolver on a pending one."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from core.exec_identity import reset_exec_identity, set_exec_identity
from core.money.ledger import SpendLedger
from core.money.receipt_state import open_receipts, receipt_states, voided_refs
from core.wallet.broadcast.evm import Receipt
from core.wallet.tx_guard import Decision
from tools.defi.trade_tool import DefiTradeTool, TransferParams

TX = "0x" + "cd" * 32
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
TOKEN = "0x" + "77" * 20
ROUTER = "0x" + "26" * 20


class _Journal:
    def unresolved(self):
        return []

    def mark_booked(self, *a, **k):
        pass


class _Signer:
    address = "0x" + "22" * 20


class _Wallet:
    def __init__(self, gate):
        self.policy = gate

    def operational_signer(self):
        return _Signer()


def _rail(status, *, block=False):
    class Rail:
        def __init__(self, chain=None, signer=None, **kw):
            pass

        def build_erc20_transfer(self, *, token, to, amount_raw):
            return {"to": token, "data": "0xa9059cbb", "value": 0}

        def sign_and_send(self, tx):
            return TX

        def await_receipt(self, tx_hash, **kw):
            if block:
                time.sleep(0.5)
            return Receipt(tx_hash=tx_hash, status=status,
                           block_number=(1 if status != "pending" else None))
    return Rail


@pytest.fixture
def env(monkeypatch):
    import core.money.hooks as hooks
    book = []
    monkeypatch.setattr(hooks, "submission_journal", lambda: _Journal())
    monkeypatch.setattr(hooks, "position_book",
                        lambda: (lambda uid, deltas: book.append((uid, list(deltas)))))
    monkeypatch.setattr("core.config_policy.AutonomyConfig.autonomy_halted",
                        staticmethod(lambda: False))
    token = set_exec_identity("owner-1", "s1")
    gate = SpendLedger(max_per_tx_usd=100, daily_cap_usd=50)
    yield SimpleNamespace(gate=gate, book=book)
    reset_exec_identity(token)


def _guard(intent, tx, **kw):
    return Decision(allowed=True, reason="test", lane="autonomous", amount_usd=40.0,
                    simulated_token_deltas={TOKEN: 5 * 10 ** 18, USDC: -40 * 10 ** 6})


def _tool(gate, status, **kw):
    return DefiTradeTool(wallet=_Wallet(gate), rail_factory=_rail(status, **kw),
                         guard_fn=_guard, price_fn=lambda c, a: 1.0)


# -- perform_transfer --------------------------------------------------------

async def _transfer(env, status, monkeypatch):
    from core.wallet import tokens
    monkeypatch.setattr(tokens, "get_token_identity",
                        lambda c, t: SimpleNamespace(decimals=6, symbol="USDC"))
    tool = _tool(env.gate, status)
    return await tool.transfer(TransferParams(chain="base", token=USDC,
        to="0x" + "3" * 40, amount=40.0, max_spend_usd=50.0, dry_run=False))


@pytest.mark.asyncio
async def test_transfer_pending_receipt_is_left_for_the_resolver(env, monkeypatch):
    await _transfer(env, "pending", monkeypatch)
    assert [e["result_ref"] for e in open_receipts(env.gate.audit_log)] == [TX]
    assert voided_refs(env.gate.audit_log) == set()
    assert env.gate.rolling_24h_spend_usd() == 40.0


@pytest.mark.asyncio
async def test_transfer_failed_receipt_is_voided_at_once(env, monkeypatch):
    await _transfer(env, "failed", monkeypatch)
    assert open_receipts(env.gate.audit_log) == []
    assert voided_refs(env.gate.audit_log) == {TX}
    assert env.gate.rolling_24h_spend_usd() == 0.0


@pytest.mark.asyncio
async def test_transfer_success_counts_and_is_not_open(env, monkeypatch):
    await _transfer(env, "success", monkeypatch)
    assert receipt_states(env.gate.audit_log) == {}
    assert env.gate.rolling_24h_spend_usd() == 40.0


# -- _run_guarded (a swap) ---------------------------------------------------

_CTX = {"chain": "base", "token_in": USDC, "token_out": TOKEN, "in_native": False,
        "in_symbol": "USDC", "out_symbol": "TKN", "in_qty": 40.0, "out_qty": 5.0,
        "in_decimals": 6, "out_decimals": 18}


async def _swap(env, status, *, block=False):
    tool = _tool(env.gate, status, block=block)
    return await tool._run_guarded(
        intent=SimpleNamespace(chain="base", is_registration=False), tx={"to": ROUTER},
        rail=_rail(status, block=block)(), gate=env.gate, signer=_Signer(),
        execution_context=None, header="", dry_run=False, venue_action="swap",
        idem="k-swap", counterparty=ROUTER, position_ctx=dict(_CTX))


@pytest.mark.asyncio
async def test_swap_success_writes_the_position_once_and_stores_no_copy(env):
    await _swap(env, "success")
    assert len(env.book) == 1 and env.book[0][0] == "owner-1"
    assert env.book[0][1][0].address == TOKEN and env.book[0][1][0].qty == 5.0
    entry = env.gate.audit_log[0]
    assert "deferred_positions" not in entry and "receipt_state" not in entry


@pytest.mark.asyncio
async def test_swap_pending_stores_the_position_for_the_resolver(env):
    await _swap(env, "pending")
    assert env.book == []
    assert [e["result_ref"] for e in open_receipts(env.gate.audit_log)] == [TX]
    assert voided_refs(env.gate.audit_log) == set()
    entry = env.gate.audit_log[0]
    assert entry["position_uid"] == "owner-1"
    assert [d["address"] for d in entry["deferred_positions"]] == [TOKEN]

    from core.wallet.receipt_resolver import resolve_pending_receipts

    def call(chain, method, params):
        if method == "eth_chainId":
            return hex(8453)
        return {"transactionHash": TX, "status": "0x1", "blockNumber": "0x10"}
    await resolve_pending_receipts(env.gate, call=call)
    await resolve_pending_receipts(env.gate, call=call)
    assert len(env.book) == 1 and env.book[0][1][0].qty == 5.0


@pytest.mark.asyncio
async def test_swap_failed_is_voided_and_writes_no_position(env):
    await _swap(env, "failed")
    assert env.book == []
    assert open_receipts(env.gate.audit_log) == []
    assert voided_refs(env.gate.audit_log) == {TX}
    assert "deferred_positions" not in env.gate.audit_log[0]


@pytest.mark.asyncio
async def test_swap_cancelled_in_the_wait_stores_the_position_for_the_resolver(env):
    task = asyncio.ensure_future(_swap(env, "success", block=True))
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert env.book == []
    entry = env.gate.audit_log[0]
    assert entry["receipt_state"] == "pending_receipt"
    assert [d["address"] for d in entry["deferred_positions"]] == [TOKEN]
