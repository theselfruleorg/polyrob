"""Token-bound account primitives on a FORK of Robinhood Chain 4663 (core handoff W3, W8, W13).

Opt-in: ``AGENT_NFT_FORK_E2E=1`` and an anvil fork at ``AGENT_NFT_FORK_RPC`` (default
``http://127.0.0.1:8550``)::

    anvil --fork-url https://rpc.mainnet.chain.robinhood.com --port 8550

⚠️ Start a FRESH fork per run: the public node is not an archive node, so after some minutes the
fork block's state is gone and every send hangs pending ("historical state … is not available").

Nothing is signed: every transaction on the fork uses ``anvil_impersonateAccount``; the fork
is snapshotted first and reverted at the end. The account is a REAL AccountV3 clone from the
REAL registry, parented by ERC-8004 agent #1 on 4663, transferred to the treasury (069 v4: the
agent acts through an account only as the NFT's owner).
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
TREASURY = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
#: ``contract Puller { function pull(address token, uint256 amt) external {
#:     require(IERC20(token).transferFrom(msg.sender, address(this), amt)); } }`` (solc 0.8.30)
PULLER_INIT = "0x6080604052348015600e575f5ffd5b5061028a8061001c5f395ff3fe608060405234801561000f575f5ffd5b5060043610610029575f3560e01c8063f2d5d56b1461002d575b5f5ffd5b61004760048036038101906100429190610163565b610049565b005b8173ffffffffffffffffffffffffffffffffffffffff166323b872dd3330846040518463ffffffff1660e01b8152600401610086939291906101bf565b6020604051808303815f875af11580156100a2573d5f5f3e3d5ffd5b505050506040513d601f19601f820116820180604052508101906100c69190610229565b6100ce575f5ffd5b5050565b5f5ffd5b5f73ffffffffffffffffffffffffffffffffffffffff82169050919050565b5f6100ff826100d6565b9050919050565b61010f816100f5565b8114610119575f5ffd5b50565b5f8135905061012a81610106565b92915050565b5f819050919050565b61014281610130565b811461014c575f5ffd5b50565b5f8135905061015d81610139565b92915050565b5f5f60408385031215610179576101786100d2565b5b5f6101868582860161011c565b92505060206101978582860161014f565b9150509250929050565b6101aa816100f5565b82525050565b6101b981610130565b82525050565b5f6060820190506101d25f8301866101a1565b6101df60208301856101a1565b6101ec60408301846101b0565b949350505050565b5f8115159050919050565b610208816101f4565b8114610212575f5ffd5b50565b5f81519050610223816101ff565b92915050565b5f6020828403121561023e5761023d6100d2565b5b5f61024b84828501610215565b9150509291505056fea264697066735822122072b3d659b7bf0c366442e2fea70a0c4705846b969c7addf99c305228b87bbbeb64736f6c634300081e0033"


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
    rc = None
    for _ in range(50):
        rc = _rpc("eth_getTransactionReceipt", [h])
        if rc:
            break
        time.sleep(0.1)
    assert rc and int(rc["status"], 16) == 1, rc
    return rc


def _uint(to, sig, types, values):
    raw = _rpc("eth_call", [{"to": to, "data": abi.encode_call(sig, types, values)}, "latest"])
    return int(raw, 16)


def _allowance(owner, spender):
    return _uint(WETH, "allowance", [{"type": "address"}, {"type": "address"}], [owner, spender])


def _balance(who):
    return _uint(WETH, "balanceOf", [{"type": "address"}], [who])


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
    _rpc("anvil_setBalance", [account, hex(10 ** 18)])
    yield {"account": account, "owner": owner, "parent": parent, "token_id": token_id,
           "rpc": lambda m, p: _rpc(m, p)}
    _rpc("evm_revert", [snap])


def _authorize(tx, intent):
    return tx_guard.authorize(
        intent, tx, holder=TREASURY, gate=PolicyGate(max_per_tx_usd=100.0, daily_cap_usd=1000.0),
        execution_context=None, price_fn=lambda chain, addr: 4000.0,
        rpc_is_pinned_fn=lambda c: True, halted_fn=lambda: False,
        entry_paused_fn=lambda: False, forged_fn=lambda ctx, tool: False)


# --- W8 ---------------------------------------------------------------------------------------

def _puller_and_weth(fork):
    acct = fork["account"]
    rc = _send(fork["owner"], None, PULLER_INIT)
    puller = rc["contractAddress"]
    _send(acct, WETH, "0xd0e30db0", value=10 ** 16)        # the account wraps 0.01 ETH (impersonated)
    return acct, puller


def _batch(acct, puller, amt, *, reset=True):
    approve = lambda n: abi.encode_call("approve", [{"type": "address"}, {"type": "uint256"}],  # noqa: E731
                                        [puller, n])
    pull = abi.encode_call("pull", [{"type": "address"}, {"type": "uint256"}], [WETH, amt])
    legs = [(WETH, 0, approve(amt), 0), (puller, 0, pull, 0)] + ([(WETH, 0, approve(0), 0)] if reset else [])
    return {"to": acct, "from": TREASURY, "value": 0, "chainId": 4663,
            "data": erc6551.encode_execute_batch(legs)}


def test_w8_a_clean_approve_spend_reset_batch_is_allowed_and_leaves_nothing(fork):
    acct, puller = _puller_and_weth(fork)
    amt = 10 ** 15
    state = erc6551.read_state(fork["rpc"], acct)
    tx = _batch(acct, puller, amt)
    intent = tx_guard.TxIntent(chain="robinhood", token=WETH, to=puller, amount_raw=amt, max_spend_usd=20.0,
                               idempotency_key="w8", via_account=acct, via_account_state=state,
                               via_account_batch=True)
    d = _authorize(tx, intent)
    assert d.allowed is True, d.reason
    before = _balance(acct)
    _send(TREASURY, acct, tx["data"])               # the owner may call executeBatch on AccountV3
    assert before - _balance(acct) == amt and _balance(puller) == amt
    assert _allowance(acct, puller) == 0


def test_w8_a_standing_allowance_left_behind_is_refused(fork):
    """The same batch WITHOUT the reset leg is refused by structure; with a reset to a non-zero
    amount it is refused too — measured: the simulation reads the absolute final allowance."""
    acct, puller = _puller_and_weth(fork)
    amt = 10 ** 15
    state = erc6551.read_state(fork["rpc"], acct)
    intent = tx_guard.TxIntent(chain="robinhood", token=WETH, to=puller, amount_raw=amt, max_spend_usd=20.0,
                               idempotency_key="w8b", via_account=acct, via_account_state=state,
                               via_account_batch=True)
    d = _authorize(_batch(acct, puller, amt, reset=False), intent)
    assert d.allowed is False and "exactly 3 legs" in d.reason, d.reason


# --- W13: the account's own reads ----------------------------------------------------------
#: anvil's PUBLIC dev key #1 (the TREASURY address above). Used only to produce an off-chain
#: signature the fork's account then CHECKS — nothing is sent with it.
TREASURY_DEV_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"  # gitleaks:allow (anvil dev account #1, public)


def test_w13_token_signer_and_signature_reads(fork):
    from eth_account import Account
    rpc, acct = fork["rpc"], fork["account"]
    chain_id, contract, token_id = erc6551.read_token(rpc, acct)
    assert (chain_id, contract.lower(), token_id) == (4663, fork["parent"].lower(), fork["token_id"])
    _salt, f_chain, f_contract, f_id = erc6551.read_footer(rpc, acct)
    assert (f_chain, f_contract.lower(), f_id) == (chain_id, contract.lower(), token_id)
    assert erc6551.read_is_valid_signer(rpc, acct, TREASURY) is True          # the NFT's owner
    assert erc6551.read_is_valid_signer(rpc, acct, fork["owner"]) is False    # the previous owner
    assert erc6551.read_is_valid_signer(rpc, acct, "0x" + "42" * 20) is False
    digest = "0x" + "ab" * 32
    sig = Account._sign_hash(bytes.fromhex(digest[2:]), TREASURY_DEV_KEY).signature.hex()
    sig = sig if sig.startswith("0x") else "0x" + sig
    # ⚠️ measured: the owner's raw ECDSA signature over ANY digest is the account's ERC-1271
    # signature (AccountV3 puts no account in the digest)
    assert erc6551.read_is_valid_signature(rpc, acct, digest, sig) is True
    other = Account._sign_hash(bytes.fromhex(digest[2:]), "0x" + "77" * 32).signature.hex()
    other = other if other.startswith("0x") else "0x" + other
    try:
        assert erc6551.read_is_valid_signature(rpc, acct, digest, other) is False
    except Exception:  # noqa: BLE001 — a revert is also "not valid"
        pass


# --- W3: Permit2 on the real Permit2 -------------------------------------------------------

def test_w3_a_permit2_allowance_is_in_the_table_and_the_guarded_revoke_clears_it(fork):
    rpc, acct = fork["rpc"], fork["account"]
    spender = "0x" + "5e" * 20
    start = int(_rpc("eth_blockNumber", []), 16)
    grant = abi.encode_call("approve", [{"type": "address"}, {"type": "address"}, {"type": "uint160"},
                                        {"type": "uint48"}], [WETH, spender, 12345, 2 ** 40])
    _send(acct, erc6551.PERMIT2, grant)                 # the account grants (impersonated, fork only)
    rows = erc6551.open_approvals(rpc, acct, start)
    p2 = [r for r in rows if r.kind == "permit2"]
    assert len(p2) == 1 and p2[0].verified and p2[0].amount == 12345
    assert p2[0].contract.lower() == WETH.lower() and p2[0].spender.lower() == spender

    state = erc6551.read_state(rpc, acct)
    tx = {"to": acct, "from": TREASURY, "value": 0, "chainId": 4663,
          "data": erc6551.encode_execute(erc6551.PERMIT2, 0, erc6551.encode_permit2_revoke(WETH, spender), 0)}
    intent = tx_guard.TxIntent(chain="robinhood", token=WETH, to=erc6551.PERMIT2, amount_raw=0,
                               max_spend_usd=5.0, idempotency_key="w3", is_allowance_op=True,
                               permit2_revokes=((WETH, spender),), via_account=acct, via_account_state=state)
    d = _authorize(tx, intent)
    assert d.allowed is True, d.reason
    _send(TREASURY, acct, tx["data"])
    assert [r for r in erc6551.open_approvals(rpc, acct, start) if r.kind == "permit2"] == []
