"""The collection revealer end to end on a FRESH anvil fork of Robinhood Chain 4663.

Opt-in: ``AGENT_NFT_FORK_E2E=1``; needs ``anvil`` and ``forge`` on PATH, network access to the public
4663 RPC, and the collection's Foundry project (``AGENT_NFT_CONTRACTS_DIR``, default
``../polyrob-drop/contracts`` beside this repo; contract ``AGENT_NFT_CONTRACT``, default
``Polyrob``). The fixture starts its OWN anvil on port 8554
(the one port this test may use) and kills it after — the public node is not an archive node,
so an old fork stalls after some minutes.

Flow: compile the collection into a temp dir (its tree is not written) → deploy it
through the Arachnid CREATE2 deployer (impersonated owner) → ``openMint`` → a minter mints 3 in
one call → mine past the reveal block → ``AgentNftTool.agent_nft_collection_reveal`` with a real ``LocalEoaSigner``,
the real ``tx_guard.authorize`` and the real ``simulation.simulate`` (``eth_simulateV1``): a dry
run, then a live send → all 3 ids revealed on-chain, and the treasury's only cost is gas.
"""
import asyncio
import json
import os
import shutil
import subprocess
import time
import types
import urllib.request
from pathlib import Path

import pytest

from tests.collection_pins import live_profile, pin

pytestmark = pytest.mark.skipif(os.environ.get("AGENT_NFT_FORK_E2E") != "1",
                                reason="fork e2e: set AGENT_NFT_FORK_E2E=1 (see module doc)")

PORT = 8554
URL = f"http://127.0.0.1:{PORT}"
FORK_URL = os.environ.get("AGENT_NFT_FORK_URL", "https://rpc.mainnet.chain.robinhood.com")
#: The collection's Foundry project and contract name (the drop repo's checkout).
CONTRACTS = Path(os.environ["AGENT_NFT_CONTRACTS_DIR"]) if os.environ.get("AGENT_NFT_CONTRACTS_DIR") \
    else Path(__file__).resolve().parents[3] / "polyrob-drop" / "contracts"
CONTRACT = os.environ.get("AGENT_NFT_CONTRACT", "Polyrob")
ARACHNID = "0x4e59b44847b379578588920cA78FbF26c0B4956C"
PRICE = 42 * 10 ** 15


def _rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(URL, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        out = json.loads(r.read())
    if "error" in out:
        raise RuntimeError(f"{method}: {out['error']}")
    return out["result"]


def _send(frm, to, data, value=0):
    _rpc("anvil_impersonateAccount", [frm])
    tx = {"from": frm, "data": data, "value": hex(value), "gas": hex(8_000_000)}
    if to:
        tx["to"] = to
    h = _rpc("eth_sendTransaction", [tx])
    for _ in range(600):
        rc = _rpc("eth_getTransactionReceipt", [h])
        if rc:
            assert int(rc["status"], 16) == 1, rc
            return rc
        time.sleep(0.1)
    raise AssertionError(f"no receipt for {h}")


def _call(to, data):
    return _rpc("eth_call", [{"to": to, "data": data}, "latest"])


@pytest.fixture
def fork():
    if not (shutil.which("anvil") and shutil.which("forge")):
        pytest.skip("anvil/forge not installed")
    if not (CONTRACTS / "src" / f"{CONTRACT}.sol").exists():
        pytest.skip(f"no {CONTRACT} source at {CONTRACTS}")
    # 0.024 gwei: the base fee measured on 4663 (core/wallet/chains.py robinhood row). anvil's
    # own default (~1 gwei) prices a reveal ~40x above the chain's fee market.
    proc = subprocess.Popen(["anvil", "--fork-url", FORK_URL, "--port", str(PORT), "--silent",
                             "--block-base-fee-per-gas", "24000000"])
    try:
        for _ in range(120):
            try:
                if int(_rpc("eth_chainId", []), 16) == 4663:
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)
        else:
            pytest.skip("the anvil fork did not come up")
        yield URL
    finally:
        proc.kill()
        proc.wait(timeout=10)


def _artifact(tmp_path):
    out, cache = tmp_path / "out", tmp_path / "cache"
    subprocess.run(["forge", "build", "--root", str(CONTRACTS), "--out", str(out),
                    "--cache-path", str(cache)], check=True, capture_output=True, text=True)
    return json.loads((out / f"{CONTRACT}.sol" / f"{CONTRACT}.json").read_text())


def test_collection_reveal_reveals_three_minted_ids_through_the_real_guard(fork, tmp_path, monkeypatch):
    from eth_abi import encode
    from eth_account import Account
    from eth_utils import keccak, to_checksum_address

    from core.wallet import abi
    from core.wallet.policy import PolicyGate
    from core.wallet.signer import LocalEoaSigner
    from tools.agent_nft.tool import AgentNftTool, RevealParams

    monkeypatch.setenv("DEFI_EVM_RPC_ROBINHOOD", fork)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_NFT_ENABLED", "true")
    monkeypatch.delenv("AGENT_NFT_REVEAL_MAX_GAS_USD", raising=False)
    for k in ("AUTONOMY_HALT", "DATA_ROOT", "POLYROB_INSTANCE_ID"):
        monkeypatch.delenv(k, raising=False)

    owner = "0x000000000000000000000000000000000000d35c"
    minter = "0x000000000000000000000000000000000000a11c"
    treasury = Account.create()
    for a in (owner, minter, treasury.address):
        _rpc("anvil_setBalance", [a, hex(10 ** 19)])

    # deploy the collection(owner, proceeds=owner) through the Arachnid CREATE2 deployer
    art = _artifact(tmp_path)
    init = art["bytecode"]["object"] + encode(["address", "address"], [owner, owner]).hex()
    init_b = bytes.fromhex(init.removeprefix("0x"))
    salt = os.urandom(32)
    collection = to_checksum_address(keccak(b"\xff" + bytes.fromhex(ARACHNID[2:]) + salt + keccak(init_b))[12:])
    _send(owner, ARACHNID, "0x" + salt.hex() + init_b.hex())
    assert len(_rpc("eth_getCode", [collection, "latest"])) > 2
    _send(owner, collection, abi.selector("openMint()"))

    # a minter mints 3 in one call (blind: every id commits to a future block)
    _send(minter, collection, abi.encode_call(
        "mint", [{"type": "address"}, {"type": "uint256"}, {"type": "uint256"}], [minter, 3, 0]),
        value=3 * PRICE)
    rb = [int(_call(collection, abi.encode_call("revealBlock", [{"type": "uint256"}], [i])), 16)
          for i in (1, 2, 3)]
    assert all(rb) and len(set(rb)) == 1, rb
    is_revealed = lambda i: int(_call(collection, abi.encode_call(  # noqa: E731
        "isRevealed", [{"type": "uint256"}], [i])), 16) == 1
    assert not any(is_revealed(i) for i in (1, 2, 3))
    _rpc("anvil_mine", [hex(4)])

    # the treasury reveals through the real guard; the collection is pinned for the fork only
    pin(monkeypatch, live_profile(collection, _rpc))
    signer = LocalEoaSigner(bytes(treasury.key))
    gate = PolicyGate(max_per_tx_usd=100.0, daily_cap_usd=1000.0)
    wallet = types.SimpleNamespace(operational_signer=lambda: signer, policy=gate)
    tool = AgentNftTool(wallet=wallet, price_fn=lambda chain, addr: 4000.0)

    def go(dry):
        return asyncio.new_event_loop().run_until_complete(
            tool.agent_nft_collection_reveal(RevealParams(dry_run=dry)))

    dry = go(True)
    assert dry.error is None, dry.error
    assert "DRY RUN" in dry.extracted_content and "[1, 2, 3]" in dry.extracted_content, \
        dry.extracted_content
    assert "lane:     autonomous" in dry.extracted_content, dry.extracted_content
    print("\n" + dry.extracted_content)

    before = int(_rpc("eth_getBalance", [treasury.address, "latest"]), 16)
    live = go(False)
    assert live.error is None, live.error
    print(live.extracted_content)
    assert "confirmed" in live.extracted_content and "revealed 3 [1, 2, 3]" in live.extracted_content
    assert "RE-COMMITTED" not in live.extracted_content
    assert all(is_revealed(i) for i in (1, 2, 3))
    spent = before - int(_rpc("eth_getBalance", [treasury.address, "latest"]), 16)
    assert 0 < spent < 10 ** 15, spent                     # gas only
    assert gate.audit_log[-1]["action"] == "agent_nft_collection_reveal"

    # nothing is due any more: no transaction
    again = go(False)
    assert "nothing to reveal" in again.extracted_content, again.extracted_content
