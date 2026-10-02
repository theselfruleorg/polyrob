"""v4 lp_add on the Pons PNL PoolKey, on a FORK of Robinhood Chain 4663 (core
handoff W6; 048 phase 3; 090 R1/R2).

Opt-in: ``LP_V4_FORK_E2E=1`` and an anvil fork at ``LP_V4_FORK_RPC`` (default
``http://127.0.0.1:8551``)::

    anvil --fork-url https://rpc.mainnet.chain.robinhood.com --port 8551 --chain-id 4663

Nothing is signed. The fork-only setup (the owner's ERC-20 allowance to Permit2,
then the Permit2 grant) is sent with ``anvil_impersonateAccount``; the fork is
snapshotted first and reverted at the end. The tests run the REAL
``tx_guard.authorize`` + ``simulation.simulate`` on:

1. the Permit2 grant ``approve_token(via='permit2')`` builds — allowed; the same
   grant to another spender — refused;
2. the lp_add ``prepare_add_v4`` builds from the live pool — allowed, with the
   predicted position id; a tampered declaration — refused;
3. the same lp_add sent on the fork — it mints the predicted id, so the
   authorization is not vacuous (090 §9: the Pons hook accepts the add).
"""
import json
import os
import time
import urllib.request
from types import SimpleNamespace

import pytest

from core.wallet import dex_registry, tx_guard
from core.wallet.policy import PolicyGate
from tools.defi import lp_reads, lp_v4 as V

pytestmark = pytest.mark.skipif(os.environ.get("LP_V4_FORK_E2E") != "1",
                                reason="fork e2e: set LP_V4_FORK_E2E=1 and run anvil (see module doc)")

RPC = os.environ.get("LP_V4_FORK_RPC", "http://127.0.0.1:8551")
TREASURY = "0xcAda546F6A6DdDe31b71Ab21ef63D3EbF09fA553"
PNL = "0xbBa60AB93Fc409b1A34371CBF6c3173795Ed2c7e"
POOL_ID = "0x43b7b259007600ad19df15a23b620fd77c8c121c8ecc8788d5116a833d031e94"
ROW = dex_registry.row_for("robinhood", "v4")
ETH_USD = 2690.0


def _rpc(method, params, timeout=None):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read())
    if "error" in out:
        raise RuntimeError(f"{method}: {out['error']}")
    return out["result"]


def _send(frm, to, data, value=0):
    _rpc("anvil_impersonateAccount", [frm])
    h = _rpc("eth_sendTransaction", [{"from": frm, "to": to, "data": data, "value": hex(value),
                                      "gas": hex(2_000_000)}])
    for _ in range(50):
        rc = _rpc("eth_getTransactionReceipt", [h])
        if rc:
            assert int(rc["status"], 16) == 1, rc
            return rc
        time.sleep(0.1)
    raise AssertionError("no receipt")


def _price(state):
    s = state.sqrt_price_x96 / 2 ** 96
    pnl_per_eth = s * s
    return lambda chain, addr: ETH_USD if addr.lower() != PNL.lower() else ETH_USD / pnl_per_eth


@pytest.fixture
def fork(monkeypatch, tmp_path):
    try:
        if int(_rpc("eth_chainId", []), 16) != 4663:
            pytest.skip(f"{RPC} is not a 4663 fork")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no fork at {RPC}: {exc}")
    monkeypatch.setenv("DEFI_EVM_RPC_ROBINHOOD", RPC)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    # The deposits here are ~$50; below this ceiling the guard's lane is
    # autonomous. The default $25 sends them to the owner queue (also allowed=False).
    monkeypatch.setenv("DEFI_AUTONOMOUS_MAX_USD", "300")
    snap = _rpc("evm_snapshot", [])
    _rpc("anvil_setBalance", [TREASURY, hex(10 * 10 ** 18)])
    state = V.pool_state(_rpc, "robinhood", POOL_ID)
    yield {"state": state, "price": _price(state)}
    _rpc("evm_revert", [snap])


def _authorize(intent, tx, price, **kw):
    return tx_guard.authorize(
        intent, tx, holder=TREASURY, gate=PolicyGate(max_per_tx_usd=500.0, daily_cap_usd=5000.0),
        execution_context=None, price_fn=price, rpc_is_pinned_fn=lambda c: True,
        halted_fn=lambda: False, entry_paused_fn=lambda: False,
        forged_fn=lambda ctx, tool: False, liquidity_rpc=_rpc, **kw)


def _grant(amount, spender=ROW.position_manager):
    exp = int(time.time()) + 900
    tx = {"to": ROW.permit2, "from": TREASURY, "value": 0, "chainId": 4663,
          "data": V.encode_permit2_approve(PNL, spender, amount, exp)}
    intent = tx_guard.TxIntent(chain="robinhood", token=PNL, to=spender, amount_raw=0,
                               max_spend_usd=200.0, is_allowance_op=True,
                               expected_allowance_grants=((PNL, spender, amount),),
                               idempotency_key=f"p2-{spender}-{amount}")
    return intent, tx


def test_the_native_key_is_the_live_pool(fork):
    key = V.pons_key_for(_rpc, PNL)
    assert key["currency0"] == V.ZERO and lp_reads.pool_id(key) == POOL_ID
    assert fork["state"].sqrt_price_x96 > 0 and fork["state"].liquidity > 0
    dex_registry.verify_pins(_rpc, "robinhood", "v4")
    dex_registry.verify_hook(_rpc, "robinhood", key["hooks"])


def test_permit2_grant_to_the_pinned_posm_is_allowed_and_another_is_refused(fork):
    amount = 10 ** 24
    _send(TREASURY, PNL, "0x095ea7b3" + ROW.permit2[2:].lower().rjust(64, "0")
          + format(amount, "064x"))          # the owner's one ERC-20 allowance to Permit2
    d = _authorize(*_grant(amount), fork["price"])
    assert d.allowed and d.lane == "autonomous", d.reason
    other = "0x" + "3" * 40
    d = _authorize(*_grant(amount, spender=other), fork["price"])
    assert not d.allowed and "Permit2 Approval" in d.reason, d.reason


def _plan(eth=0.01, pnl=1_000_000):
    from tools.defi.lp_v4_verbs import prepare_add_v4
    p = SimpleNamespace(chain="robinhood", token_a="native", token_b=PNL, amount_a=eth,
                        amount_b=pnl, range="full", initial_price=None, token_id=None,
                        slippage_bps=100, max_spend_usd=200.0)
    return prepare_add_v4(p, _rpc, TREASURY, ROW.position_manager, price_fn=None)


def test_lp_add_through_the_guard_and_on_the_fork(fork):
    amount = 10 ** 24
    _send(TREASURY, PNL, "0x095ea7b3" + ROW.permit2[2:].lower().rjust(64, "0")
          + format(amount, "064x"))
    _send(TREASURY, ROW.permit2, _grant(amount)[1]["data"])
    plan = _plan()
    tx = {"to": ROW.position_manager, "from": TREASURY, "value": plan.value,
          "data": plan.data, "chainId": 4663}
    next_id = int(lp_reads.view(_rpc, ROW.position_manager, V.POSM_NEXT_TOKEN_ID))
    d = _authorize(plan.intent, tx, fork["price"])
    assert d.allowed and d.lane == "autonomous", d.reason
    assert d.position_token_id == next_id
    assert d.amount_usd and d.amount_usd < 200.0

    # a declaration that under-states the calldata's PNL maximum refuses
    import dataclasses
    low = tuple((t, n - 1 if t else n) for t, n in plan.intent.lp_outflows)
    d2 = _authorize(dataclasses.replace(plan.intent, lp_outflows=low), tx, fork["price"])
    assert not d2.allowed and "may pay" in d2.reason, d2.reason

    # not vacuous: the same bytes mint that id on the fork, through the Pons hook
    rc = _send(TREASURY, ROW.position_manager, plan.data, value=plan.value)
    owner = lp_reads.view(_rpc, ROW.position_manager,
                          {"name": "ownerOf", "inputs": [{"type": "uint256"}],
                           "outputs": [{"type": "address"}]}, [next_id])
    assert str(owner).lower() == TREASURY.lower()
    from core.wallet import liquidity_guard
    liquidity_guard.assert_position_events(plan.intent, rc["logs"], next_id)
