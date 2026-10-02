"""G0.2 regression on a FORK of Robinhood Chain 4663 (core handoff W1; G0 review).

Opt-in: ``AGENT_NFT_FORK_E2E=1`` and an anvil fork at ``AGENT_NFT_FORK_RPC`` (default
``http://127.0.0.1:8550``)::

    anvil --fork-url https://rpc.mainnet.chain.robinhood.com --port 8550

⚠️ Start a FRESH fork per run: the public node is not an archive node, so after some minutes the
fork block's state is gone and every send hangs pending ("historical state … is not available").

Nothing is signed: every transaction on the fork is sent with ``anvil_impersonateAccount``.
The fork state is snapshotted first and reverted at the end.

The measured bypass (at 6d2254770): a signer that controls a REAL AccountV3 clone
sends ``to = forwarder, data = aggregate3([(account, false, execute(0xbeef, 0.05 ETH, "", 0))]),
value = 1 wei`` and ``tx_guard.authorize`` answered ``allowed=True lane=autonomous``; the replay
moved 0.05 ETH out of the account. This test runs that exact transaction through the real
``authorize`` + the real ``simulation.simulate`` and asserts refusal — and proves on the same
fork that the transaction really does drain the account, so the refusal is not vacuous.
"""
import json
import os
import time
import urllib.request

import pytest

from core.wallet import abi, erc6551, tx_guard
from core.wallet.erc8004 import _IDENTITY_MAINNET
from core.wallet.policy import PolicyGate

pytestmark = pytest.mark.skipif(os.environ.get("AGENT_NFT_FORK_E2E") != "1",
                                reason="fork e2e: set AGENT_NFT_FORK_E2E=1 and run anvil (see module doc)")

RPC = os.environ.get("AGENT_NFT_FORK_RPC", "http://127.0.0.1:8550")
FORWARDER = "0xcA1167915584462449EE5b4Ea51c37fE81eCDCCD"
TREASURY = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
ATTACKER = "0x000000000000000000000000000000000000bEEF"
DRAIN = 5 * 10 ** 16


def _rpc(method, params):
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
                                      "gas": hex(1_000_000)}])
    rc = None
    for _ in range(50):
        rc = _rpc("eth_getTransactionReceipt", [h])
        if rc:
            break
        time.sleep(0.1)
    assert rc and int(rc["status"], 16) == 1, rc
    return rc


def _balance(a):
    return int(_rpc("eth_getBalance", [a, "latest"]), 16)


def _aggregate3(calls):
    from eth_abi import encode
    return abi.selector("aggregate3((address,bool,bytes)[])") + encode(
        ["(address,bool,bytes)[]"], [[(a, f, bytes.fromhex(d[2:])) for a, f, d in calls]]).hex()


@pytest.fixture
def fork(monkeypatch, tmp_path):
    try:
        if int(_rpc("eth_chainId", []), 16) != 4663:
            pytest.skip(f"{RPC} is not a 4663 fork")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no fork at {RPC}: {exc}")
    monkeypatch.setenv("DEFI_EVM_RPC_ROBINHOOD", RPC)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("POLYROB_INSTANCE_ID", raising=False)
    snap = _rpc("evm_snapshot", [])
    # A REAL AccountV3 clone from the REAL registry, parented by ERC-8004 agent #1 on 4663.
    parent, token_id = _IDENTITY_MAINNET, 1
    owner = abi.decode([{"type": "address"}], _rpc("eth_call", [{"to": parent, "data": abi.encode_call(
        "ownerOf", [{"type": "uint256"}], [token_id])}, "latest"]))[0]
    for a in (owner, TREASURY):
        _rpc("anvil_setBalance", [a, hex(10 ** 18)])
    _send(owner, erc6551.REGISTRY, erc6551.encode_create_account(4663, parent, token_id))
    account = erc6551.account_address(4663, parent, token_id)
    # 069 v4 (the simple model): the treasury OWNS the NFT — no grant exists.
    _send(owner, parent, abi.encode_call(
        "transferFrom", [{"type": "address"}, {"type": "address"}, {"type": "uint256"}],
        [owner, TREASURY, token_id]))
    _rpc("anvil_setBalance", [account, hex(10 ** 17)])
    yield {"account": account, "owner": owner, "tmp": tmp_path}
    _rpc("evm_revert", [snap])


def _authorize(tx, intent):
    return tx_guard.authorize(
        intent, tx, holder=TREASURY, gate=PolicyGate(max_per_tx_usd=100.0, daily_cap_usd=1000.0),
        execution_context=None, price_fn=lambda chain, addr: 4000.0,
        rpc_is_pinned_fn=lambda c: True, halted_fn=lambda: False,
        entry_paused_fn=lambda: False, forged_fn=lambda ctx, tool: False)


def _g0_2(account, value):
    data = _aggregate3([(account, False, erc6551.encode_execute(ATTACKER, DRAIN, "0x", 0))])
    tx = {"to": FORWARDER, "from": TREASURY, "value": value, "data": data, "chainId": 4663}
    intent = tx_guard.TxIntent(chain="robinhood", token=None, to=FORWARDER, amount_raw=value,
                               max_spend_usd=5.0, idempotency_key=f"g0-2-{value}")
    return tx, intent


def test_the_fork_facts_the_fix_relies_on(fork):
    rpc = lambda m, p: _rpc(m, p)  # noqa: E731
    acct = fork["account"]
    assert erc6551.read_implementation(rpc, acct).lower() == erc6551.ACCOUNT_V3_IMPL.lower()
    assert erc6551.read_is_trusted_forwarder(rpc, acct, FORWARDER) is True
    assert erc6551.read_is_trusted_forwarder(rpc, acct, ATTACKER) is False


@pytest.mark.parametrize("value", [0, 1])
def test_g0_2_the_exact_transaction_is_refused(fork, value):
    tx, intent = _g0_2(fork["account"], value)
    d = _authorize(tx, intent)
    assert d.allowed is False and d.lane == "refuse", d.reason
    assert "ERC-2771 forwarder" in d.reason, d.reason


def test_g0_2_the_refusal_is_not_vacuous_the_transaction_drains_the_account(fork):
    acct = fork["account"]
    before = _balance(acct)
    tx, _ = _g0_2(acct, 1)
    _send(TREASURY, FORWARDER, tx["data"], value=1)
    assert before - _balance(acct) == DRAIN
