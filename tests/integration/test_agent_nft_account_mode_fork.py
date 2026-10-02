"""069 v4 A3/A4/§5 end to end on a FRESH anvil fork of Robinhood Chain 4663.

Opt-in: ``AGENT_NFT_FORK_E2E=1``; needs ``anvil`` and ``forge`` on PATH, network access to the
public 4663 RPC, and the collection's Foundry project (``AGENT_NFT_CONTRACTS_DIR``, default
``../polyrob-drop/contracts`` then ``../polyrob-desk/contracts`` beside this repo; contract
``AGENT_NFT_CONTRACT``, default ``Polyrob``). The fixture starts its OWN anvil on port 8561 (the
one port this test may use) and kills it after, so every run is a fresh fork.

⚠️ Every key is FRESH (``Account.create()``): on real 4663 the anvil default addresses carry an
EIP-7702 delegation (a drainer delegate). Nothing is signed for 4663 itself — only for the fork.

Flow: compile the collection into a temp dir → deploy it through the Arachnid CREATE2 deployer
→ ``openMint`` → a HUMAN mints #1 to itself → pin the profile with the LIVE runtime hash → the
human transfers #1 to the treasury → the holdings watch reports "You gave me POLYROB #1" →
``defi_trade_transfer`` (native and WETH) and ``defi_trade_swap`` (native → WETH through a
WETH ``deposit`` route) with ``nft=`` run through the REAL guard (``eth_simulateV1``), are
signed by a ``LocalEoaSigner`` and broadcast by ``EvmRail``, booked with the account and
journaled → the account approves a spender (impersonated: "an approval exists") → the
withdraw (``/nft send``'s verb) is refused by §5 rule 5 naming ``agent_nft_revoke_all`` →
``agent_nft_revoke_all`` clears it through the account → the withdraw sends #1 to the human →
the watch reports the loss.
"""
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("AGENT_NFT_FORK_E2E") != "1",
                                reason="fork e2e: set AGENT_NFT_FORK_E2E=1 (see module doc)")

PORT = 8561
URL = f"http://127.0.0.1:{PORT}"
FORK_URL = os.environ.get("AGENT_NFT_FORK_URL", "https://rpc.mainnet.chain.robinhood.com")
_HERE = Path(__file__).resolve().parents[3]
CONTRACTS = (Path(os.environ["AGENT_NFT_CONTRACTS_DIR"]) if os.environ.get("AGENT_NFT_CONTRACTS_DIR")
             else next((p for p in (_HERE / "polyrob-drop" / "contracts",
                                    _HERE / "polyrob-desk" / "contracts") if p.exists()),
                       _HERE / "polyrob-drop" / "contracts"))
CONTRACT = os.environ.get("AGENT_NFT_CONTRACT", "Polyrob")
ARACHNID = "0x4e59b44847b379578588920cA78FbF26c0B4956C"
WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
PRICE = 42 * 10 ** 15
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
    # The public 4663 RPC sits behind a Cloudflare bot check that answers anvil's own HTTP client
    # with a 403 challenge page (measured 2026-10-01; a --fork-header User-Agent does not help,
    # Python's client passes). So anvil forks through a local forwarding proxy (ephemeral port).
    proxy = _start_proxy(FORK_URL)
    proc = subprocess.Popen(["anvil", "--fork-url", f"http://127.0.0.1:{proxy.server_port}",
                             "--port", str(PORT), "--silent",
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
        proxy.shutdown()


def _start_proxy(upstream):
    import http.server
    import threading

    class Forward(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            req = urllib.request.Request(upstream, body, {"Content-Type": "application/json",
                                                          "User-Agent": "curl/8.7.1"})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    out, code = r.read(), r.status
            except urllib.error.HTTPError as exc:
                out, code = exc.read(), exc.code
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Forward)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _artifact(tmp_path, name=None):
    out, cache = tmp_path / "out", tmp_path / "cache"
    if not out.exists():
        subprocess.run(["forge", "build", "--root", str(CONTRACTS), "--out", str(out),
                        "--cache-path", str(cache)], check=True, capture_output=True, text=True)
    name = name or CONTRACT
    return json.loads((out / f"{name}.sol" / f"{name}.json").read_text())


def _create2(sender, init: bytes) -> str:
    """Deploy *init* through the Arachnid CREATE2 deployer with a fresh salt; its address."""
    from eth_utils import keccak, to_checksum_address
    salt = os.urandom(32)
    addr = to_checksum_address(keccak(b"\xff" + bytes.fromhex(ARACHNID[2:]) + salt + keccak(init))[12:])
    rc = _send(sender, ARACHNID, "0x" + salt.hex() + init.hex())
    assert len(_rpc("eth_getCode", [addr, "latest"])) > 2, f"nothing deployed at {addr}"
    return addr, rc


def _deploy_collection(tmp_path, owner):
    """The collection as polyrob-desk deploys it (a701720+; tests/test_fork_e2e.py::_deploy,
    76a5192): two DataChunks (engine, trait schema) → PolyrobRenderer(engine chunks, sha256(engine),
    schema chunk) → Polyrob(owner, proceeds, faceWriter, renderer). Returns ``(address, block)``."""
    import hashlib

    from eth_abi import encode

    def chunk(data: bytes) -> bytes:   # runtime = 0x00 ‖ data
        return bytes.fromhex("61" + (len(data) + 1).to_bytes(2, "big").hex() + "80600a3d393df300") + data
    engine = b"// test engine"
    schema = bytes([4]) + b"tier" + bytes([1, 5]) + b"basic"
    engine_chunk, _ = _create2(owner, chunk(engine))
    schema_chunk, _ = _create2(owner, chunk(schema))
    renderer_init = bytes.fromhex(_artifact(tmp_path, "PolyrobRenderer")["bytecode"]["object"].removeprefix("0x")) \
        + encode(["address[]", "bytes32", "address"],
                 [[engine_chunk], hashlib.sha256(engine).digest(), schema_chunk])
    renderer, _ = _create2(owner, renderer_init)
    init = bytes.fromhex(_artifact(tmp_path)["bytecode"]["object"].removeprefix("0x")) \
        + encode(["address", "address", "address", "address"], [owner, owner, owner, renderer])
    collection, rc = _create2(owner, init)
    return collection, int(rc["blockNumber"], 16)


class _Wallet:
    def __init__(self, signer, gate):
        self._signer, self.policy = signer, gate

    def operational_signer(self):
        return self._signer


def test_account_mode_holdings_watch_and_rule_5_on_a_fork(fork, tmp_path, monkeypatch):
    import asyncio

    from eth_account import Account

    from core.wallet import abi, erc6551, nft_holdings
    from core.wallet.audit_sink import JsonlAuditSink
    from core.wallet.nft_account import journal_path, load_journal, recover_owner
    from core.wallet.policy import PolicyGate
    from core.wallet.signer import LocalEoaSigner
    from tests.collection_pins import live_profile, pin
    from tools.agent_nft.tool import AgentNftTool, RevokeAllParams, TakeParams
    from tools.defi.providers.routes import RouteQuote
    from tools.defi.trade_tool import DefiTradeTool, SwapParams, TransferParams

    home = tmp_path / "home"
    monkeypatch.setenv("DEFI_EVM_RPC_ROBINHOOD", fork)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(home))
    monkeypatch.setenv("AGENT_NFT_ENABLED", "true")
    for k in ("AUTONOMY_HALT", "DATA_ROOT", "POLYROB_INSTANCE_ID", "DEFI_AUTONOMOUS_MAX_USD",
              "AGENT_WALLET_MAX_PER_TX_USD"):
        monkeypatch.delenv(k, raising=False)
    run = lambda coro: asyncio.new_event_loop().run_until_complete(coro)  # noqa: E731

    owner = Account.create().address                # the collection's deployer/owner (impersonated)
    treasury = Account.create()                     # the agent's treasury key (signs, fork only)
    human = Account.create().address                # the human who gives the NFT (impersonated)
    stranger_spender = Account.create().address
    for a in (owner, treasury.address, human):
        _rpc("anvil_setBalance", [a, hex(10 ** 19)])
    for a in (treasury.address, human):
        assert _rpc("eth_getCode", [a, "latest"]) in ("0x", "0x0"), "a fresh key has no code"

    # -- the collection, minted by a human ----------------------------------------------------
    collection, deploy_block = _deploy_collection(tmp_path, owner)
    _send(owner, collection, abi.selector("openMint()"))
    mint = abi.encode_call("mint", [{"type": "address"}, {"type": "uint256"}, {"type": "uint256"}],
                           [human, 1, 0])
    _send(human, collection, mint, value=PRICE)
    token_id = 1
    owner_of = lambda: abi.decode([{"type": "address"}], _call(collection, abi.encode_call(  # noqa: E731
        "ownerOf", [{"type": "uint256"}], [token_id])))[0].lower()
    assert owner_of() == human.lower()
    account = erc6551.account_address(4663, collection, token_id)
    assert len(_rpc("eth_getCode", [account, "latest"])) > 2, "the account exists at mint"

    pin(monkeypatch, live_profile(collection, _rpc, deploy_block=deploy_block, journal_prefix="POLYROB"))

    # -- the human gives it to the treasury; the watch reports it -----------------------------
    _send(human, collection, abi.encode_call(
        "transferFrom", [{"type": "address"}, {"type": "address"}, {"type": "uint256"}],
        [human, treasury.address, token_id]))
    told = []

    async def notify(_c, _u, text):
        told.append(text)

    def watch():
        return run(nft_holdings.tick(None, treasury=treasury.address, rpc_for=lambda c: _rpc,
                                     notify=notify))
    res = watch()
    print("\nwatch:", res.as_dict(), told)
    assert [r["token_id"] for r in res.arrived] == [token_id] and not res.errors
    assert told[-1].startswith(f"You gave me POLYROB #{token_id}")
    assert watch().arrived == [] and len(told) == 1                    # once

    # -- the money verbs, from the account, through the REAL guard ------------------------------
    signer = LocalEoaSigner(bytes(treasury.key))
    gate = PolicyGate(max_per_tx_usd=100.0, daily_cap_usd=1000.0,
                      audit_sink=JsonlAuditSink(str(tmp_path / "wallet_audit.jsonl")))
    wallet = _Wallet(signer, gate)
    _rpc("anvil_setBalance", [account, hex(5 * 10 ** 16)])               # the account holds 0.05 ETH

    def wrap_route(chain, token_in, token_out, amount_in_raw, *, holder, slippage_bps):
        # A route that IS WETH.deposit(): the account sends native and receives WETH 1:1.
        assert holder.lower() == account.lower(), "the route must pay the account"
        return RouteQuote(chain=chain, token_in=token_in, token_out=token_out,
                          amount_in_raw=amount_in_raw, amount_out_raw=amount_in_raw,
                          amount_out_min_raw=amount_in_raw, spender=WETH, to=WETH,
                          calldata="0xd0e30db0", value_raw=amount_in_raw, venue="weth-deposit",
                          quoted_at=time.time(), locally_built=True), ""
    trade = DefiTradeTool(wallet=wallet, price_fn=lambda c, a: ETH_USD,
                          fallback_price_fn=lambda c, a: ETH_USD, route_fn=wrap_route,
                          account_rpc=_rpc)
    monkeypatch.setattr(DefiTradeTool, "_route_sanity",
                        lambda self, *a, **k: ("consistent", "route check: WETH deposit is 1:1"))
    import tools.defi.identity_gate as ig
    monkeypatch.setattr(ig, "buy_identity_refusal", lambda **kw: None)

    bal = lambda a: int(_rpc("eth_getBalance", [a, "latest"]), 16)  # noqa: E731
    weth = lambda a: int(_call(WETH, abi.encode_call("balanceOf", [{"type": "address"}], [a])), 16)  # noqa: E731

    acct0, human0 = bal(account), bal(human)
    r = run(trade.transfer(TransferParams(chain="robinhood", token="native", to=human, amount=0.001,
                                          max_spend_usd=10.0, nft=str(token_id), dry_run=False)))
    print("\ntransfer native:", r.error or r.extracted_content)
    assert r.error is None and "SENT AND CONFIRMED" in r.extracted_content, r.error or r.extracted_content
    assert acct0 - bal(account) == 10 ** 15 and bal(human) - human0 == 10 ** 15

    r = run(trade.swap(SwapParams(chain="robinhood", token_in="native", token_out=WETH,
                                  amount_in=0.002, max_spend_usd=20.0, account=account,
                                  dry_run=False)))
    print("\nswap native->WETH:", r.error or r.extracted_content)
    assert r.error is None and "RESULT: CONFIRMED" in r.extracted_content, r.error or r.extracted_content
    assert weth(account) == 2 * 10 ** 15

    r = run(trade.transfer(TransferParams(chain="robinhood", token=WETH, to=human, amount=0.0005,
                                          max_spend_usd=10.0, nft=str(token_id), dry_run=False)))
    print("\ntransfer WETH:", r.error or r.extracted_content)
    assert r.error is None and "SENT AND CONFIRMED" in r.extracted_content, r.error or r.extracted_content
    assert weth(account) == 15 * 10 ** 14 and weth(human) == 5 * 10 ** 14

    booked = [e for e in gate.audit_log if e.get("account")]
    assert [e["action"] for e in booked] == ["transfer", "swap", "transfer"]
    assert all(e["account"] == account.lower() for e in booked)
    from core.instance import resolve_owner_user_id
    entries = load_journal(journal_path(home, resolve_owner_user_id(), None, 4663, account))
    assert [e["kind"] for e in entries] == ["tend", "entry", "tend"]
    assert all(recover_owner(e, "POLYROB") == treasury.address.lower() for e in entries)

    # -- an approval exists → the NFT may not leave; revoke_all clears it -----------------------
    _rpc("anvil_setBalance", [account, hex(5 * 10 ** 16)])
    _send(account, WETH, abi.encode_call("approve", [{"type": "address"}, {"type": "uint256"}],
                                         [stranger_spender, 12345]))
    nft = AgentNftTool(wallet=wallet, rpc_fn=_rpc)
    nft._impl = lambda: None                                     # no agent-NFT package needed
    r = run(nft.agent_nft_withdraw_token(TakeParams(to=human, nft=str(token_id), dry_run=False)))
    print("\nwithdraw with an open approval:", r.error or r.extracted_content)
    text = r.extracted_content or r.error or ""
    assert "NOT SENT" in text and "agent_nft_revoke_all" in text and "open approval" in text, text
    assert owner_of() == treasury.address.lower()

    r = run(nft.agent_nft_revoke_all(RevokeAllParams(nft=str(token_id), dry_run=False)))
    print("\nrevoke_all:", r.error or r.extracted_content)
    assert r.error is None and "confirmed" in r.extracted_content, r.error or r.extracted_content
    assert erc6551.open_approvals(_rpc, account, deploy_block) == []

    r = run(nft.agent_nft_withdraw_token(TakeParams(to=human, nft=str(token_id), dry_run=False)))
    print("\nwithdraw after revoke:", r.error or r.extracted_content)
    assert r.error is None and "confirmed" in r.extracted_content, r.error or r.extracted_content
    assert owner_of() == human.lower()

    # -- the watch reports the loss once; the account can no longer be acted through -----------
    res = watch()
    assert [r["token_id"] for r in res.lost] == [token_id]
    assert "is no longer mine" in told[-1] and len(told) == 2
    assert watch().lost == [] and len(told) == 2
    r = run(trade.transfer(TransferParams(chain="robinhood", token="native", to=human, amount=0.001,
                                          max_spend_usd=10.0, nft=str(token_id), dry_run=False)))
    assert r.error and "does not own" in r.error
