"""The collection paid mint end to end on a FRESH anvil fork of Robinhood Chain 4663
(testnet-run 2026-09-29 F2; 050 §7.3).

Opt-in: ``AGENT_NFT_FORK_E2E=1``; needs ``anvil`` and ``forge`` on PATH, network access to the public
4663 RPC, and the collection's Foundry project (``AGENT_NFT_CONTRACTS_DIR``, default
``../polyrob-drop/contracts`` beside this repo; contract ``AGENT_NFT_CONTRACT``, default
``Polyrob``). The fixture starts its OWN anvil on port 8556
(the one port this test may use) and kills it after, so every run is a fresh fork.

Flow: compile the collection into a temp dir (its tree is not written) → deploy it
through the Arachnid CREATE2 deployer (impersonated owner) → ``openMint`` → the treasury mints
qty 1, then (after the first id is due, so the second mint auto-reveals it) qty 3, each through
the real ``tx_guard.authorize`` + ``simulation.simulate`` (``eth_simulateV1``) with the
``is_collection_mint`` shape, signed by a real ``LocalEoaSigner`` and broadcast by ``EvmRail`` → the
ids are owned by the treasury, every account holds its stipend plus its PNL/ETH share, and the
treasury paid exactly qty × PRICE plus gas.

⚠️ The treasury is a FRESH key (``Account.create()``), not an anvil default: on real 4663 the
anvil default addresses carry an EIP-7702 delegation whose delegate rejects
``onERC721Received``, so ``_safeMint`` to them reverts.
"""
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

from tests.collection_pins import live_profile, pin

pytestmark = pytest.mark.skipif(os.environ.get("AGENT_NFT_FORK_E2E") != "1",
                                reason="fork e2e: set AGENT_NFT_FORK_E2E=1 (see module doc)")

PORT = 8556
URL = f"http://127.0.0.1:{PORT}"
FORK_URL = os.environ.get("AGENT_NFT_FORK_URL", "https://rpc.mainnet.chain.robinhood.com")
#: The collection's Foundry project and contract name (the drop repo's checkout).
CONTRACTS = Path(os.environ["AGENT_NFT_CONTRACTS_DIR"]) if os.environ.get("AGENT_NFT_CONTRACTS_DIR") \
    else Path(__file__).resolve().parents[3] / "polyrob-drop" / "contracts"
CONTRACT = os.environ.get("AGENT_NFT_CONTRACT", "Polyrob")
ARACHNID = "0x4e59b44847b379578588920cA78FbF26c0B4956C"
STIPEND = 5 * 10 ** 14
PNL = "0xbBa60AB93Fc409b1A34371CBF6c3173795Ed2c7e"
ETH_USD = 4000.0


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
    # 0.024 gwei: the base fee measured on 4663 (core/wallet/chains.py robinhood row).
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


def test_collection_mint_qty_1_and_3_through_the_real_guard(fork, tmp_path, monkeypatch):
    from eth_abi import encode
    from eth_account import Account
    from eth_utils import keccak, to_checksum_address

    from core.wallet import abi, collection_mint, tx_guard
    from core.wallet.broadcast.evm import EvmRail
    from core.wallet.policy import PolicyGate
    from core.wallet.signer import LocalEoaSigner

    monkeypatch.setenv("DEFI_EVM_RPC_ROBINHOOD", fork)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    for k in ("AUTONOMY_HALT", "DATA_ROOT", "POLYROB_INSTANCE_ID", "DEFI_AUTONOMOUS_MAX_USD",
              "AGENT_WALLET_MAX_PER_TX_USD"):
        monkeypatch.delenv(k, raising=False)

    owner = "0x000000000000000000000000000000000000d35c"
    treasury = Account.create()
    for a in (owner, treasury.address):
        _rpc("anvil_setBalance", [a, hex(10 ** 19)])
    assert _rpc("eth_getCode", [treasury.address, "latest"]) in ("0x", "0x0")

    art = _artifact(tmp_path)
    init = art["bytecode"]["object"] + encode(["address", "address"], [owner, owner]).hex()
    init_b = bytes.fromhex(init.removeprefix("0x"))
    salt = os.urandom(32)
    collection = to_checksum_address(keccak(b"\xff" + bytes.fromhex(ARACHNID[2:]) + salt + keccak(init_b))[12:])
    _send(owner, ARACHNID, "0x" + salt.hex() + init_b.hex())
    assert len(_rpc("eth_getCode", [collection, "latest"])) > 2
    _send(owner, collection, abi.selector("openMint()"))

    # the pinned constant IS the contract's PRICE, read on the real bytecode
    assert int(_call(collection, abi.selector("PRICE()")), 16) == collection_mint.MINT_PRICE_WEI
    assert int(_call(collection, abi.selector("MAX_PER_TX()")), 16) == collection_mint.MAX_MINT_QTY

    pin(monkeypatch, live_profile(collection, _rpc))
    signer = LocalEoaSigner(bytes(treasury.key))
    rail = EvmRail(chain="robinhood", signer=signer)
    # a DURABLE audit sink, as in production: only a durable cap charge releases the submission
    # interlock, so the second live mint is possible at all
    from core.wallet.audit_sink import JsonlAuditSink
    gate = PolicyGate(max_per_tx_usd=1000.0, daily_cap_usd=5000.0,
                      audit_sink=JsonlAuditSink(str(tmp_path / "wallet_audit.jsonl")))
    u256 = lambda name, i: abi.encode_call(name, [{"type": "uint256"}], [i])  # noqa: E731

    def authorize(intent, tx):
        return tx_guard.authorize(intent, tx, holder=signer.address, gate=gate,
                                  execution_context=None, price_fn=lambda c, a: ETH_USD,
                                  account_rpc=_rpc)

    def mint(qty):
        before = int(_call(collection, abi.selector("totalMinted()")), 16)
        ids = tuple(range(before + 1, before + qty + 1))
        value = collection_mint.mint_value(qty)
        tx = rail.build_call(to=collection, data=collection_mint.encode_mint(signer.address, qty, 0), value=value)
        intent = tx_guard.TxIntent(chain="robinhood", token=None, to=collection, amount_raw=value,
                                   max_spend_usd=qty * 0.042 * ETH_USD + 5.0,
                                   idempotency_key=f"fork-mint-{before}-{qty}",
                                   is_collection_mint=True, mint_ids=ids)
        # warm the fork's state cache (pool, registry, accounts): a cold fork can exceed the
        # simulation's RPC timeout on the first eth_simulateV1 — a read, nothing is sent
        _rpc("eth_call", [{"from": signer.address, "to": collection, "value": hex(value),
                           "data": tx["data"]}, "latest"])
        # the default autonomous ceiling ($25) is below the price: the value is charged, so
        # the unattended lane refuses and the owner queue is the answer
        queued = authorize(intent, tx)
        assert queued.allowed is False and queued.lane == "owner_queue", queued.reason
        assert queued.amount_usd >= qty * 0.042 * ETH_USD, queued.amount_usd
        # the owner raised the ceiling for this test (an owner-granted call behaves the same)
        monkeypatch.setenv("AGENT_WALLET_MAX_PER_TX_USD", "1000")
        monkeypatch.setenv("DEFI_AUTONOMOUS_MAX_USD", "1000")
        try:
            decision = authorize(intent, tx)
        finally:
            monkeypatch.delenv("DEFI_AUTONOMOUS_MAX_USD")
            monkeypatch.delenv("AGENT_WALLET_MAX_PER_TX_USD")
        assert decision.allowed is True, decision.reason
        print(f"\nmint {qty}: {decision.reason}, lane {decision.lane}, ${decision.amount_usd}, "
              f"sim gas {decision.sim_gas_used}")
        bal0 = int(_rpc("eth_getBalance", [signer.address, "latest"]), 16)
        signed = rail.size_gas(tx, decision.sim_gas_used)
        tx_hash = rail.sign_and_send(signed)
        receipt = rail.await_receipt(tx_hash)
        assert receipt.status == "success", receipt
        gate.record(venue="defi", action="agent_nft_collection_mint", amount_usd=decision.amount_usd,
                    counterparty=collection, idempotency_key=intent.idempotency_key,
                    result_ref=tx_hash, chain="robinhood")
        paid = bal0 - int(_rpc("eth_getBalance", [signer.address, "latest"]), 16)
        assert value < paid < value + 10 ** 15, (paid, value)       # the price plus gas only
        for i in ids:
            owner_of = "0x" + _call(collection, u256("ownerOf", i))[-40:]
            assert owner_of.lower() == signer.address.lower(), (i, owner_of)
            acct = to_checksum_address("0x" + _call(collection, u256("accountOf", i))[-40:])
            eth = int(_rpc("eth_getBalance", [acct, "latest"]), 16)
            pnl = int(_call(PNL, abi.encode_call("balanceOf", [{"type": "address"}], [acct])), 16)
            assert eth >= STIPEND, (i, eth)
            assert pnl > 0 or eth > STIPEND, (i, eth, pnl)          # the buy or the ETH fallback
            print(f"  id {i}: owner treasury, account {acct} eth {eth} pnl {pnl}")
        return ids, tx_hash

    ids1, _ = mint(1)
    assert ids1 == (1,)
    _rpc("anvil_mine", [hex(4)])                  # id 1 is due: the next mint auto-reveals it
    ids3, h3 = mint(3)
    assert ids3 == (2, 3, 4)
    assert int(_call(collection, u256("isRevealed", 1)), 16) == 1
    assert int(_call(collection, abi.selector("totalMinted()")), 16) == 4

    # a wrong value never reaches the chain: refused before the simulation
    tx = rail.build_call(to=collection, data=collection_mint.encode_mint(signer.address, 1, 0),
                         value=collection_mint.MINT_PRICE_WEI - 1)
    bad = authorize(tx_guard.TxIntent(chain="robinhood", token=None, to=collection,
                                      amount_raw=collection_mint.MINT_PRICE_WEI - 1, max_spend_usd=500.0,
                                      idempotency_key="bad", is_collection_mint=True, mint_ids=(5,)), tx)
    assert bad.allowed is False and "exactly" in bad.reason, bad.reason
    # declaring the wrong next id is refused by the SIMULATION (the ids that arrive differ)
    tx = rail.build_call(to=collection, data=collection_mint.encode_mint(signer.address, 1, 0),
                         value=collection_mint.MINT_PRICE_WEI)
    monkeypatch.setenv("AGENT_WALLET_MAX_PER_TX_USD", "1000")
    monkeypatch.setenv("DEFI_AUTONOMOUS_MAX_USD", "1000")
    stale = authorize(tx_guard.TxIntent(chain="robinhood", token=None, to=collection,
                                        amount_raw=collection_mint.MINT_PRICE_WEI, max_spend_usd=500.0,
                                        idempotency_key="stale", is_collection_mint=True, mint_ids=(9,)), tx)
    assert stale.allowed is False and "exactly ids [9]" in stale.reason, stale.reason
    print(f"refusals: {bad.reason}\n          {stale.reason}")
