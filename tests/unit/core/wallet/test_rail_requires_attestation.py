"""125 S3/S4 (B-M1, B-M4; D3): the EVM rail signs only an authorized transaction.

``tx_guard.authorize`` records its allowed (or owner-queue) decision in
``core.signer.attest`` in EVERY signer mode, and ``EvmRail.sign_and_send``
refuses a transaction with no record before it signs or reaches a node — in
``local`` and ``shadow`` mode too, not only ``remote``. A dry run records
nothing, so it cannot leave an authorization behind for a later send.
"""
import json

import pytest
from eth_utils import keccak

from core.signer import attest
from tests.unit.core.signer.conftest import (  # noqa: F401 — fixtures
    ETH_PRICE, FakeRail, LoopbackClient, _clean_signer_latches, make_rig, native_deltas,
    native_intent, native_tx, rig, signer_home)


def _authorize(intent, tx, *, dry_run=False):
    from core.wallet import tx_guard
    from core.wallet.policy import PolicyGate
    return tx_guard.authorize(
        intent, tx, holder="0x" + "2" * 40,
        gate=PolicyGate(max_per_tx_usd=500.0, daily_cap_usd=1000.0),
        execution_context=None, simulate_fn=lambda **_: native_deltas(),
        price_fn=lambda c, a: ETH_PRICE, rpc_is_pinned_fn=lambda c: True,
        halted_fn=lambda: False, entry_paused_fn=lambda: False,
        forged_fn=lambda ctx, tool: False, dry_run=dry_run)


class _Signer:
    address = "0x" + "1" * 40

    def __init__(self):
        self.signed = []

    def sign_transaction(self, tx):
        self.signed.append(dict(tx))
        return b"synthetic signed bytes"


def _rail(signer, rpc_log):
    from core.wallet.broadcast.evm import EvmRail
    rail = EvmRail("base", signer)

    def rpc(method, params, timeout=8.0):
        rpc_log.append(method)
        if method == "eth_chainId":
            return hex(8453)
        if method == "eth_sendRawTransaction":
            return "0x" + keccak(b"synthetic signed bytes").hex()
        raise AssertionError(method)
    rail._rpc = rpc
    return rail


def test_local_mode_without_a_record_refuses_and_calls_no_rpc(signer_home):
    from core.wallet.broadcast.evm import BroadcastError
    signer, rpc = _Signer(), []
    with pytest.raises(BroadcastError, match="no tx_guard authorization"):
        _rail(signer, rpc).sign_and_send(native_tx())
    assert signer.signed == [] and rpc == []


def test_local_mode_after_an_allowed_authorize_sends_and_consumes_the_record(signer_home):
    tx = native_tx()
    assert _authorize(native_intent(), tx).allowed
    assert attest.peek(tx) is not None                     # recorded in local mode (D3)
    signer, rpc = _Signer(), []
    rail = _rail(signer, rpc)
    assert rail.sign_and_send(tx)
    assert "eth_sendRawTransaction" in rpc and len(signer.signed) == 1
    assert attest.peek(tx) is None                         # taken once: no replay
    from core.wallet.broadcast.evm import BroadcastError
    with pytest.raises(BroadcastError, match="no tx_guard authorization"):
        rail.sign_and_send(tx)


def test_a_dry_run_records_no_authorization(signer_home):
    tx = native_tx()
    assert _authorize(native_intent(), tx, dry_run=True).allowed
    assert attest.peek(tx) is None


def test_shadow_mode_logs_agree_and_never_no_intent(rig, signer_home, monkeypatch):
    from core.signer.shadow import ShadowEvmSigner, shadow_log_path
    from core.wallet.broadcast.evm import BroadcastError
    monkeypatch.setenv("WALLET_SIGNER", "shadow")
    client = LoopbackClient(rig.service)
    signer = ShadowEvmSigner(rig.service.wallet.operational_signer(), client=client)
    rail = _rail(signer, [])
    with pytest.raises(BroadcastError, match="no tx_guard authorization"):
        rail.sign_and_send(native_tx(nonce=6))             # skipped the guard: not signed
    tx = native_tx()
    assert _authorize(native_intent(), tx).allowed
    rail._rpc = lambda m, p, timeout=8.0: hex(8453) if m == "eth_chainId" else "0x" + "ab" * 32
    assert rail.sign_and_send(tx)
    with open(shadow_log_path()) as fh:
        kinds = [json.loads(line)["kind"] for line in fh]
    # One process here shares the agent's submission journal with the signer, so the
    # signer may DISAGREE on the agent's own reservation; it never sees "no_intent".
    assert len(kinds) == 1 and kinds[0] in ("agree", "disagree")
    assert attest.peek(tx) is None


class _SweepEvmRail:
    """A REAL EvmRail (its sign_and_send runs) with the node faked."""

    def __new__(cls, chain, signer, **kw):
        from core.wallet.broadcast.evm import EvmRail
        rail = EvmRail(chain, signer, **kw)

        def rpc(method, params, timeout=8.0):
            return {
                "eth_chainId": hex(8453),
                "eth_getBalance": hex(10 ** 16),
                "eth_getTransactionCount": hex(0),
                "eth_maxPriorityFeePerGas": hex(10 ** 8),
                "eth_getBlockByNumber": {"baseFeePerGas": hex(10 ** 8)},
            }.get(method) if method != "eth_sendRawTransaction" else (
                "0x" + keccak(bytes.fromhex(params[0][2:])).hex())
        rail._rpc = rpc
        return rail


def test_the_signer_deposit_sweep_still_sends(make_rig):
    from eth_utils import to_checksum_address
    from modules.payments.wallet_generator import RemoteDepositWalletGenerator
    dest = to_checksum_address("0x" + "3" * 40)
    rig_ = make_rig(sweep={"destination": dest, "tokens": {}})
    rig_.service._rail_factory = _SweepEvmRail
    gen = RemoteDepositWalletGenerator(client=LoopbackClient(rig_.service))
    addr = gen.generate_deposit_address("u-1")
    tx_hash = gen.sweep(user_id="u-1", chain="base", token_symbol="ETH", deposit_address=addr)
    assert tx_hash and tx_hash.startswith("0x")


@pytest.mark.asyncio
async def test_a_dry_run_verb_leaves_no_authorization_behind(signer_home, monkeypatch):
    """S4: a money verb's dry run runs the real guard and records nothing; the same
    call with dry_run=false records the authorization its send consumes."""
    from core.wallet import simulation
    from tests.unit.tools.defi import test_trade_real_guard as real
    from tools.defi.trade_tool import ApproveParams
    monkeypatch.setenv("DEFI_EVM_RPC_BASE", "https://pinned.example/rpc")
    monkeypatch.delenv("DEFI_AUTONOMOUS_MAX_USD", raising=False)
    monkeypatch.setattr(simulation, "simulate", real._approve_sim)
    tool, _ = real._tool()
    params = dict(chain="base", token=real.USDC, spender=real.ROUTER, amount=1.0,
                  max_spend_usd=2.0)
    res = await tool.approve_token(ApproveParams(**params, dry_run=True),
                                   execution_context=real._genuine_ctx())
    assert "DRY RUN" in (res.extracted_content or "")
    assert not attest._items
    res = await tool.approve_token(ApproveParams(**params, dry_run=False),
                                   execution_context=real._genuine_ctx())
    assert real._Rail.last.sent
    assert attest.peek(real._Rail.last.sent_tx) is not None   # the fake rail took nothing
