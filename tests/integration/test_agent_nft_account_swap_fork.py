"""C18 (polyrob-desk handoff-core) — an ERC-20-in swap FROM a token-bound account through a REAL
router, as the W8 approve → swap → reset batch (``via_account_batch``), on a fork of Base.

Opt-in: ``AGENT_NFT_FORK_E2E=1`` and a FRESH anvil fork of Base at ``AGENT_NFT_BASE_FORK_RPC``
(default ``http://127.0.0.1:8551``)::

    anvil --fork-url https://mainnet.base.org --port 8551

Base, not 4663: Robinhood Chain pins no swap router (it routes through an aggregator), while Base
pins Uniswap's SwapRouter02. Nothing is broadcast to a real chain (impersonation on the fork,
snapshotted and reverted); the treasury is a FRESH key. The account is the real AccountV3 clone of
ERC-8004 identity #1 on Base, pinned as a collection (C4) for the test. Its swap: 0.001 WETH →
USDC through ``exactInputSingle`` (0.05% pool), recipient the account, judged by the REAL guard
(``eth_simulateV1`` on the fork): the spend is the batch's middle leg, USDC must arrive at the
account, and the final allowance must read 0.
"""
import json
import os
import time
import urllib.request

import pytest
from eth_account import Account

from core.wallet import abi, collection_registry, erc6551, tx_guard
from core.wallet.erc8004 import _IDENTITY_MAINNET
from core.wallet.policy import PolicyGate

pytestmark = pytest.mark.skipif(os.environ.get("AGENT_NFT_FORK_E2E") != "1",
                                reason="fork e2e: set AGENT_NFT_FORK_E2E=1 and run anvil (see module doc)")

RPC = os.environ.get("AGENT_NFT_BASE_FORK_RPC", "http://127.0.0.1:8551")
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"   # SwapRouter02 on Base


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
    tx = {"from": frm, "data": data, "value": hex(value), "gas": hex(3_000_000)}
    if to:
        tx["to"] = to
    h = _rpc("eth_sendTransaction", [tx])
    for _ in range(100):
        rc = _rpc("eth_getTransactionReceipt", [h])
        if rc:
            break
        time.sleep(0.1)
    assert rc and int(rc["status"], 16) == 1, rc
    return rc


def _uint(to, sig, types, values):
    return int(_rpc("eth_call", [{"to": to, "data": abi.encode_call(sig, types, values)}, "latest"]), 16)


def _balance(token, who):
    return _uint(token, "balanceOf", [{"type": "address"}], [who])


@pytest.fixture
def fork(monkeypatch, tmp_path):
    try:
        if int(_rpc("eth_chainId", []), 16) != 8453:
            pytest.skip(f"{RPC} is not a Base fork")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no fork at {RPC}: {exc}")
    monkeypatch.setenv("DEFI_EVM_RPC_BASE", RPC)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    snap = _rpc("evm_snapshot", [])
    parent, token_id = _IDENTITY_MAINNET, 1
    owner = abi.decode([{"type": "address"}], _rpc("eth_call", [{"to": parent, "data": abi.encode_call(
        "ownerOf", [{"type": "uint256"}], [token_id])}, "latest"]))[0]
    treasury = Account.create().address
    for a in (owner, treasury):
        _rpc("anvil_setBalance", [a, hex(10 ** 18)])
    account = erc6551.account_address(8453, parent, token_id)
    if len(_rpc("eth_getCode", [account, "latest"])) <= 2:
        _send(owner, erc6551.REGISTRY, erc6551.encode_create_account(8453, parent, token_id))
    _send(owner, parent, abi.encode_call(
        "transferFrom", [{"type": "address"}, {"type": "address"}, {"type": "uint256"}],
        [owner, treasury, token_id]))
    _rpc("anvil_setBalance", [account, hex(10 ** 17)])
    _send(account, WETH, "0xd0e30db0", value=10 ** 16)          # the account wraps 0.01 ETH
    pin = collection_registry.CollectionProfile(
        spec=collection_registry.SPEC_V1, capabilities=(), chain_id=8453, address=parent.lower(),
        runtime_sha256=collection_registry.runtime_sha256_of(_rpc, parent), deploy_block=0,
        max_supply=token_id, accounts=(collection_registry.AccountVersion(
            erc6551.REGISTRY.lower(), erc6551.ACCOUNT_V3_IMPL.lower(), erc6551.ACCOUNT_SALT),))
    monkeypatch.setattr(collection_registry, "profiles", lambda: (pin,))
    yield {"account": account, "treasury": treasury}
    _rpc("evm_revert", [snap])


def _exact_input_single(amount_in, recipient, min_out=1):
    return abi.encode_call(
        "exactInputSingle",
        [{"type": "tuple", "components": [
            {"type": "address"}, {"type": "address"}, {"type": "uint24"}, {"type": "address"},
            {"type": "uint256"}, {"type": "uint256"}, {"type": "uint160"}]}],
        [(WETH, USDC, 500, recipient, amount_in, min_out, 0)])


def test_c18_an_erc20_in_swap_from_the_account_through_swaprouter02(fork):
    acct, treasury = fork["account"], fork["treasury"]
    amt = 10 ** 15
    approve = lambda n: abi.encode_call("approve", [{"type": "address"}, {"type": "uint256"}],  # noqa: E731
                                        [ROUTER, n])
    legs = [(WETH, 0, approve(amt), 0), (ROUTER, 0, _exact_input_single(amt, acct), 0),
            (WETH, 0, approve(0), 0)]
    data = erc6551.encode_execute_batch(legs)
    tx = {"to": acct, "from": treasury, "value": 0, "chainId": 8453, "data": data}
    intent = tx_guard.TxIntent(
        chain="base", token=WETH, to=ROUTER, amount_raw=amt, max_spend_usd=20.0,
        idempotency_key="c18", watch_spenders=(ROUTER,), inflow_token=USDC, min_inflow_raw=1,
        via_account=acct, via_account_state=erc6551.read_state(_rpc, acct), via_account_batch=True)
    d = tx_guard.authorize(
        intent, tx, holder=treasury, gate=PolicyGate(max_per_tx_usd=100.0, daily_cap_usd=1000.0),
        execution_context=None, price_fn=lambda chain, addr: 1.0 if addr.lower() == USDC.lower() else 3000.0,
        rpc_is_pinned_fn=lambda c: True, halted_fn=lambda: False, entry_paused_fn=lambda: False,
        forged_fn=lambda ctx, tool: False, account_rpc=_rpc)
    assert d.allowed is True, d.reason
    weth0, usdc0 = _balance(WETH, acct), _balance(USDC, acct)
    _send(treasury, acct, data)
    assert weth0 - _balance(WETH, acct) == amt
    assert _balance(USDC, acct) > usdc0
    assert _uint(WETH, "allowance", [{"type": "address"}, {"type": "address"}], [acct, ROUTER]) == 0


def test_c18_a_batch_paying_the_swap_out_elsewhere_refuses(fork):
    """The same batch with the router's recipient set to a stranger: USDC never reaches the
    account, so the declared minimum inflow is unproven — the guard refuses."""
    acct, treasury = fork["account"], fork["treasury"]
    amt = 10 ** 15
    stranger = Account.create().address
    approve = lambda n: abi.encode_call("approve", [{"type": "address"}, {"type": "uint256"}],  # noqa: E731
                                        [ROUTER, n])
    legs = [(WETH, 0, approve(amt), 0), (ROUTER, 0, _exact_input_single(amt, stranger), 0),
            (WETH, 0, approve(0), 0)]
    tx = {"to": acct, "from": treasury, "value": 0, "chainId": 8453,
          "data": erc6551.encode_execute_batch(legs)}
    intent = tx_guard.TxIntent(
        chain="base", token=WETH, to=ROUTER, amount_raw=amt, max_spend_usd=20.0,
        idempotency_key="c18b", watch_spenders=(ROUTER,), inflow_token=USDC, min_inflow_raw=1,
        via_account=acct, via_account_state=erc6551.read_state(_rpc, acct), via_account_batch=True)
    d = tx_guard.authorize(
        intent, tx, holder=treasury, gate=PolicyGate(max_per_tx_usd=100.0, daily_cap_usd=1000.0),
        execution_context=None, price_fn=lambda chain, addr: 1.0 if addr.lower() == USDC.lower() else 3000.0,
        rpc_is_pinned_fn=lambda c: True, halted_fn=lambda: False, entry_paused_fn=lambda: False,
        forged_fn=lambda ctx, tool: False, account_rpc=_rpc)
    assert d.allowed is False, d.reason
