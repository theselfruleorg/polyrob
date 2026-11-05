"""Two sends from one wallet at the same time both get to the chain.

A verb builds its transaction BEFORE it holds ``gate.reserve()``. Two card confirms
at once read one nonce; the second waited for the first, then died at the node with
"nonce too low" and a real payment was reported as failed. These tests run the real
``EvmRail`` and the real ``transfer`` verb against a node that enforces the nonce.
"""
import asyncio
import contextlib
import json
from types import SimpleNamespace

import pytest

from core.wallet import tx_guard, tx_guard_steps
from core.wallet.broadcast import evm
from core.wallet.broadcast.evm import BuiltTx, EvmRail, Receipt
from core.wallet.tx_guard import Decision
from core.wallet.tx_guard_steps import shape
from tools.defi.trade_tool import DefiTradeTool, TransferParams

HOLDER = "0x2222222222222222222222222222222222222222"
TOKEN = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PAYEE = "0x" + "3" * 40


class _Node:
    """A chain that enforces the nonce the way the real one does."""

    def __init__(self, next_nonce=7):
        self.next_nonce = next_nonce
        self.mined = []

    def rpc(self, chain, method, params, timeout=8.0):
        if method == "eth_chainId":
            return hex(8453)
        if method == "eth_getTransactionCount":
            return hex(self.next_nonce)
        if method == "eth_maxPriorityFeePerGas":
            return hex(1)
        if method == "eth_getBlockByNumber":
            return {"baseFeePerGas": hex(1)}
        if method == "eth_sendRawTransaction":
            tx = json.loads(bytes.fromhex(params[0][2:]))
            if tx["nonce"] < self.next_nonce:
                raise RuntimeError("nonce too low")
            self.next_nonce = tx["nonce"] + 1
            self.mined.append(tx["nonce"])
            from eth_utils import keccak
            return "0x" + keccak(bytes.fromhex(params[0][2:])).hex()
        raise AssertionError(f"unexpected RPC {method}")


class _Signer:
    address = HOLDER

    def sign_transaction(self, tx):
        return json.dumps({"nonce": tx["nonce"]}).encode()


class _Gate:
    """The real reservation's shape: one send at a time, held until the receipt."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.recorded = []

    @contextlib.asynccontextmanager
    async def reserve(self):
        async with self.lock:
            yield

    def record(self, **kw):
        # What the real ledger does for the journal row the rail wrote.
        from core.wallet import submission_journal
        submission_journal.mark_booked(kw["result_ref"])
        self.recorded.append(kw)


class _Rail(EvmRail):
    def await_receipt(self, tx_hash, **kw):
        return Receipt(tx_hash=tx_hash, status="success", block_number=1, gas_used=21000)


@pytest.fixture
def node(monkeypatch):
    n = _Node()
    monkeypatch.setattr(evm, "_rpc_call", n.rpc)
    return n


def _tool(gate):
    def _guard(intent, tx, **kw):
        # The one step this file is about, run where the real guard runs it.
        shape.step_4b_nonce_under_reservation(SimpleNamespace(outer_tx=tx))
        return Decision(True, "test", lane="autonomous", amount_usd=1.0)

    wallet = SimpleNamespace(policy=gate, operational_signer=lambda: _Signer())
    return DefiTradeTool(wallet=wallet, rail_factory=_Rail, guard_fn=_guard,
                         price_fn=lambda c, a: 1.0, fallback_price_fn=lambda c, a: None,
                         balance_fn=lambda c, h, t: None)


@pytest.mark.asyncio
async def test_two_transfers_at_once_both_reach_the_chain(node, monkeypatch):
    from core.wallet import tokens
    monkeypatch.setattr(tokens, "get_token_identity",
                        lambda c, t: SimpleNamespace(decimals=6, symbol="USDC"))
    tool = _tool(_Gate())

    def send():
        return tool.transfer(TransferParams(chain="base", token=TOKEN, to=PAYEE, amount=1.0,
                                            max_spend_usd=2.0, dry_run=False))

    first, second = await asyncio.gather(send(), send())
    assert first.error is None, first.error
    assert second.error is None, second.error
    assert sorted(node.mined) == [7, 8]


def test_the_refresh_moves_the_nonce_forward_only(node):
    tx = EvmRail("base", _Signer()).build_native_transfer(to=PAYEE, amount_wei=1)
    assert tx["nonce"] == 7

    node.next_nonce = 9
    tx.refresh_nonce()
    assert tx["nonce"] == 9

    node.next_nonce = 4          # a lagging node never hands back a used nonce
    tx.refresh_nonce()
    assert tx["nonce"] == 9


def test_a_failed_read_keeps_the_built_nonce(node, monkeypatch):
    tx = EvmRail("base", _Signer()).build_native_transfer(to=PAYEE, amount_wei=1)

    def down(*a, **k):
        raise RuntimeError("rpc down")
    monkeypatch.setattr(evm, "_rpc_call", down)
    tx.refresh_nonce()
    assert tx["nonce"] == 7


def test_the_signed_fields_are_only_the_transaction():
    """A subclass with a reader, never an extra key: the digest and the signature
    see the same fields as before."""
    tx = BuiltTx({"nonce": 1, "chainId": 8453}, lambda: 2)
    assert dict(tx) == {"nonce": 1, "chainId": 8453}
    assert json.dumps(tx) == '{"nonce": 1, "chainId": 8453}'


def test_a_plain_dict_keeps_its_nonce():
    """What polyrob-signer judges crossed a socket: it is a plain dict, and the step
    never replaces the nonce the agent asked for."""
    tx = {"nonce": 3}
    assert shape.step_4b_nonce_under_reservation(SimpleNamespace(outer_tx=tx)) is None
    assert tx == {"nonce": 3}


def test_the_refresh_runs_before_the_simulation_and_attestation_reads_it(node, monkeypatch):
    """Through the real runner: what is simulated and what the caller signs carry
    the fresh nonce, because a CREATE's predicted address derives from it."""
    tx = EvmRail("base", _Signer()).build_native_transfer(to=PAYEE, amount_wei=1)
    node.next_nonce = 12
    seen = []

    def simulate(c):
        seen.append(c.outer_tx["nonce"])
        return Decision(False, "stop here")

    names = [s.__name__ for s in tx_guard_steps.STEPS]
    assert names.index("step_4b_nonce_under_reservation") < names.index("step_5_simulate")
    monkeypatch.setattr(tx_guard_steps, "STEPS",
                        (lambda c: setattr(c, "outer_tx", c.tx),
                         shape.step_4b_nonce_under_reservation, simulate))
    decision = tx_guard.authorize(intent=None, tx=tx, holder=HOLDER, gate=None)
    assert decision.reason == "stop here"
    assert seen == [12]
    assert tx["nonce"] == 12
