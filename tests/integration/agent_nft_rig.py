"""The agent-NFT rig: the CURRENT POLYROB stack on a plain LOCAL anvil (no fork), for the core
money guard (proposal 115 W2).

Opt-in: ``AGENT_NFT_RIG`` — ``1`` uses the desk checkout beside this repo
(``../polyrob-desk/contracts``), any other value is the path of the Foundry project. Without it,
or without ``anvil`` / ``forge`` / ``cast`` on PATH, or without the project files, every rig test
SKIPS with a named reason.

What the rig does (nothing touches a live chain, nothing is forked):

* starts its OWN ``anvil --chain-id 46630`` (Robinhood Chain testnet's id: the chain where the
  desk's ``Deploy.s.sol`` allows an absent v4 PoolManager — the ETH-fallback path — so no pool,
  no PNL and no fork is needed) on a free port, and kills it after;
* etches what the real chain has and anvil has not: the canonical ERC-6551 registry (the desk's
  fixture, sha256-pinned by ``Deploy.s.sol``), Multicall3 (core reads ``block.number`` and
  batches reads through it), the desk's own ``MockIdentityRegistry`` at the testnet ERC-8004
  Identity address (so every mint registers an identity), a small ERC-8004 v1 Reputation
  stand-in at the testnet Reputation address, and an ERC-20 at the chain's pinned wrapped-native
  address (the account's inherited token);
* runs the desk ``script/Deploy.s.sol`` UNCHANGED (``forge script --broadcast``; the broadcast
  log goes to a temp dir through ``FOUNDRY_BROADCAST``, the deploy record is off) and the desk
  ``script/pin.py --with-sale --with-transfer-safe`` UNCHANGED (``python -B``; the profile goes
  to a temp file, and pin.py validates it with THIS core's parser);
* pins that profile the way production does: the root registry file
  (``collection_registry.REGISTRY_FILE`` pointed at a read-only temp file).

⚠️ Every actor is a FRESH key (``Account.create()``): never an anvil default key.
⚠️ The desk tree is read only. ``forge build`` / ``forge script`` write only its ``out/`` and
``cache/``; nothing here writes anywhere else in it (``python -B``: no ``__pycache__``).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

OPT_IN = "AGENT_NFT_RIG"
CHAIN = "robinhood-testnet"
CHAIN_ID = 46630

REGISTRY_6551 = "0x000000006551c19487814612e58FE06813775758"
CREATE2_DEPLOYER = "0x4e59b44847b379578588920cA78FbF26c0B4956C"
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
IDENTITY_TESTNET = "0x8004A818BFB912233c491871b3d84c89A494BD9e"
REPUTATION_TESTNET = "0x8004B663056A597Dffe9eCcC1965A193B7388713"
WETH_TESTNET = "0x7943e237c7F95DA44E0301572D358911207852Fa"

BASE = 33 * 10 ** 15
START_PRICE = 99 * 10 ** 15
STEP = 33 * 10 ** 14
STEP_SECONDS = 600
WL_DURATION = 86_400
STEPS = 20

# The stand-ins anvil lacks. Compiled at run time into a temp Foundry project.
_STANDINS = r"""
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// Multicall3 (mds1/multicall, MIT): the functions core calls, the same ABI and behaviour.
contract Multicall3 {
    struct Call3 { address target; bool allowFailure; bytes callData; }
    struct Result { bool success; bytes returnData; }
    function aggregate3(Call3[] calldata calls) public payable returns (Result[] memory returnData) {
        uint256 length = calls.length;
        returnData = new Result[](length);
        for (uint256 i = 0; i < length; i++) {
            Result memory result = returnData[i];
            Call3 calldata calli = calls[i];
            (result.success, result.returnData) = calli.target.call(calli.callData);
            require(calli.allowFailure || result.success, "Multicall3: call failed");
        }
    }
    function getBlockNumber() public view returns (uint256) { return block.number; }
    function getEthBalance(address addr) public view returns (uint256) { return addr.balance; }
    function getCurrentBlockTimestamp() public view returns (uint256) { return block.timestamp; }
    function getChainId() public view returns (uint256) { return block.chainid; }
    function getBasefee() public view returns (uint256) { return block.basefee; }
}

/// An ERC-8004 v1 Reputation Registry stand-in: giveFeedback / getClients / getSummary with the
/// spec's signatures; getSummary reverts on an empty client list (as the reference does).
contract ReputationStandIn {
    mapping(uint256 => address[]) private _clients;
    mapping(uint256 => mapping(address => bool)) private _known;
    mapping(uint256 => mapping(address => uint64)) private _count;
    mapping(uint256 => mapping(address => int128)) private _sum;
    function giveFeedback(uint256 agentId, int128 value, uint8 valueDecimals, string calldata,
                          string calldata, string calldata, string calldata, bytes32) external {
        require(valueDecimals == 0, "decimals");
        if (!_known[agentId][msg.sender]) { _known[agentId][msg.sender] = true; _clients[agentId].push(msg.sender); }
        _count[agentId][msg.sender] += 1;
        _sum[agentId][msg.sender] += value;
    }
    function getClients(uint256 agentId) external view returns (address[] memory) { return _clients[agentId]; }
    function getSummary(uint256 agentId, address[] calldata clients, string calldata, string calldata)
        external view returns (uint64 count, int128 summaryValue, uint8 summaryValueDecimals) {
        require(clients.length > 0, "clientAddresses required");
        for (uint256 i = 0; i < clients.length; i++) {
            count += _count[agentId][clients[i]];
            summaryValue += _sum[agentId][clients[i]];
        }
        summaryValueDecimals = 0;
    }
}

/// An ERC-8004 Identity Registry stand-in for the card (115 W6): `setAgentURI` with the reference
/// registry's owner check and events (`MetadataUpdate`, `URIUpdated(agentId, newURI, msg.sender)`),
/// `tokenURI`, `ownerOf`, `balanceOf`. Ownership is SEEDED: the rig etches this over the desk's mock
/// for one test and restores the mock after. Namespaced storage, so the mock's slots stay as they were.
contract IdentityCardStandIn {
    event MetadataUpdate(uint256 _tokenId);
    event URIUpdated(uint256 indexed agentId, string newURI, address indexed updatedBy);
    struct S { mapping(uint256 => address) owner; mapping(address => uint256) bal; mapping(uint256 => string) uri; }
    function _s() private pure returns (S storage s) { assembly { s.slot := 0x7269672e6964656e746974792e636172640000000000000000000000000000 } }
    function seed(uint256 id, address to, string calldata uri) external { S storage s = _s(); s.owner[id] = to; s.bal[to] += 1; s.uri[id] = uri; }
    function ownerOf(uint256 id) external view returns (address o) { o = _s().owner[id]; require(o != address(0), "nonexistent"); }
    function balanceOf(address a) external view returns (uint256) { return _s().bal[a]; }
    function tokenURI(uint256 id) external view returns (string memory) { return _s().uri[id]; }
    function getApproved(uint256) external pure returns (address) { return address(0); }
    function isApprovedForAll(address, address) external pure returns (bool) { return false; }
    function setAgentURI(uint256 agentId, string calldata newURI) external {
        require(msg.sender == _s().owner[agentId], "Not authorized");
        _s().uri[agentId] = newURI;
        emit MetadataUpdate(agentId);
        emit URIUpdated(agentId, newURI, msg.sender);
    }
}

/// An ERC-20 with an open mint (the account's inherited token in the rig).
contract RigToken {
    string public constant name = "Wrapped Ether";
    string public constant symbol = "WETH";
    uint8 public constant decimals = 18;
    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);
    function mint(address to, uint256 v) external { totalSupply += v; balanceOf[to] += v; emit Transfer(address(0), to, v); }
    function transfer(address to, uint256 v) external returns (bool) { return _move(msg.sender, to, v); }
    function approve(address s, uint256 v) external returns (bool) { allowance[msg.sender][s] = v; emit Approval(msg.sender, s, v); return true; }
    function transferFrom(address f, address to, uint256 v) external returns (bool) {
        uint256 a = allowance[f][msg.sender];
        if (a != type(uint256).max) { require(a >= v, "allowance"); allowance[f][msg.sender] = a - v; }
        return _move(f, to, v);
    }
    function _move(address f, address to, uint256 v) private returns (bool) {
        require(balanceOf[f] >= v, "balance");
        balanceOf[f] -= v; balanceOf[to] += v; emit Transfer(f, to, v); return true;
    }
}
"""


def contracts_dir() -> Optional[Path]:
    """The Foundry project named by the opt-in, or None when the rig is not opted in."""
    raw = os.environ.get(OPT_IN, "").strip()
    if not raw:
        return None
    if raw == "1":
        return Path(__file__).resolve().parents[3] / "polyrob-desk" / "contracts"
    return Path(raw).expanduser().resolve()


def skip_reason() -> Optional[str]:
    """Why the rig cannot run here, or None."""
    path = contracts_dir()
    if path is None:
        return (f"agent-NFT rig: opt in with {OPT_IN}=1 (the desk checkout beside this repo, "
                f"../polyrob-desk/contracts) or {OPT_IN}=<path of the Foundry project>")
    missing = [t for t in ("anvil", "forge", "cast") if not shutil.which(t)]
    if missing:
        return f"agent-NFT rig: {', '.join(missing)} not on PATH (install foundry)"
    for rel in ("src/Polyrob.sol", "script/Deploy.s.sol", "script/pin.py",
                "test/fixtures/erc6551_registry.runtime.hex", "test/mocks/Mocks.sol",
                "deploy/engine/engine.js.gz"):
        if not (path / rel).exists():
            return f"agent-NFT rig: {path / rel} is missing ({OPT_IN} names {path})"
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _fee_market_shim(upstream: str):
    """A local JSON-RPC forwarder in front of anvil that answers ``eth_maxPriorityFeePerGas`` with
    0, as an Arbitrum-stack node (Robinhood Chain) does — anvil always suggests 1 gwei, which
    prices every core call ~40x above the chain's fee market. Everything else passes through
    unchanged. Core talks to the shim; the rig's own setup talks to anvil."""
    import http.server
    import threading

    class Forward(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                msg = json.loads(body)
            except ValueError:
                msg = None
            if isinstance(msg, dict) and msg.get("method") == "eth_maxPriorityFeePerGas":
                out = json.dumps({"jsonrpc": "2.0", "id": msg.get("id"), "result": "0x0"}).encode()
            else:
                req = urllib.request.Request(upstream, body, {"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=120) as r:
                    out = r.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Forward)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@dataclass
class Rig:
    """A running anvil with the deployed stack; every helper is a local JSON-RPC call."""
    url: str
    contracts: Path
    work: Path
    proc: Any = None
    shim: Any = None
    core_url: str = ""          # what core's RPC pin names (the fee-market shim)
    collection: str = ""
    sale: str = ""
    account_impl: str = ""
    journal_log: str = ""
    market: str = ""
    zone: str = ""
    owner: str = ""             # the collection owner (fresh, impersonated)
    proceeds: str = ""
    deploy_block: int = 0
    profile: Dict[str, Any] = field(default_factory=dict)
    pin_log: str = ""
    deploy_log: str = ""
    card_standin: str = ""      # 115 W6: the IdentityCardStandIn runtime (etched by one test)

    # -- JSON-RPC -------------------------------------------------------------------------------
    def rpc(self, method: str, params: list):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(self.url, body, {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            out = json.loads(r.read())
        if "error" in out:
            raise RuntimeError(f"{method}: {out['error']}")
        return out["result"]

    def call(self, to: str, data: str, *, frm: Optional[str] = None, value: int = 0) -> str:
        tx = {"to": to, "data": data}
        if frm:
            tx["from"] = frm
        if value:
            tx["value"] = hex(value)
        return self.rpc("eth_call", [tx, "latest"])

    def uint(self, to: str, sig: str, *args) -> int:
        return int(self.call(to, self.encode(sig, *args)), 16)

    def address_of(self, to: str, sig: str, *args) -> str:
        return "0x" + self.call(to, self.encode(sig, *args))[-40:]

    @staticmethod
    def encode(sig: str, *args) -> str:
        from core.wallet import abi
        name, rest = sig.split("(", 1)
        types = [t for t in rest.rstrip(")").split(",") if t]
        return abi.encode_call(name, [{"type": t} for t in types], list(args))

    def send(self, frm: str, to: Optional[str], data: str, value: int = 0, *,
             expect_ok: bool = True, gas: int = 25_000_000) -> dict:
        """An impersonated transaction from a FRESH address (never an anvil default key)."""
        self.rpc("anvil_impersonateAccount", [frm])
        tx = {"from": frm, "data": data, "value": hex(value), "gas": hex(gas)}
        if to:
            tx["to"] = to
        h = self.rpc("eth_sendTransaction", [tx])
        rc = self.receipt(h)
        if expect_ok:
            assert int(rc["status"], 16) == 1, f"{data[:10]} → {to} reverted: {rc}"
        return rc

    def receipt(self, h: str) -> dict:
        for _ in range(600):
            rc = self.rpc("eth_getTransactionReceipt", [h])
            if rc:
                return rc
            time.sleep(0.05)
        raise AssertionError(f"no receipt for {h}")

    def fund(self, addr: str, wei: int = 100 * 10 ** 18) -> None:
        self.rpc("anvil_setBalance", [addr, hex(wei)])

    def balance(self, addr: str) -> int:
        return int(self.rpc("eth_getBalance", [addr, "latest"]), 16)

    def code(self, addr: str) -> str:
        return self.rpc("eth_getCode", [addr, "latest"])

    def warp(self, seconds: int) -> None:
        self.rpc("evm_increaseTime", [int(seconds)])
        self.rpc("evm_mine", [])

    def mine(self, n: int = 1) -> None:
        self.rpc("anvil_mine", [hex(n)])

    def head(self) -> int:
        return int(self.rpc("eth_blockNumber", []), 16)

    def core_rpc(self, method: str, params: list):
        """The same JSON-RPC through the shim core uses."""
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(self.core_url, body, {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            out = json.loads(r.read())
        if "error" in out:
            raise RuntimeError(f"{method}: {out['error']}")
        return out["result"]

    def stop(self) -> None:
        if self.shim is not None:
            self.shim.shutdown()
            self.shim = None
        if self.proc is not None:
            self.proc.kill()
            self.proc.wait(timeout=10)
            self.proc = None

    # -- the collection ---------------------------------------------------------------------------
    def owner_of(self, token_id: int) -> str:
        return self.address_of(self.collection, "ownerOf(uint256)", token_id).lower()

    def next_id(self) -> int:
        return self.uint(self.collection, "nextId()")

    def account_of(self, token_id: int) -> str:
        return self.address_of(self.collection, "accountOf(uint256)", token_id)

    def price_now(self) -> int:
        return self.uint(self.sale, "priceNow()")

    def phase(self) -> int:
        return self.uint(self.sale, "phase()")


def _run(cmd: List[str], *, cwd: Optional[Path] = None, env: Optional[dict] = None,
         timeout: int = 900) -> str:
    r = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env, capture_output=True,
                       text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])} failed ({r.returncode}):\n{r.stdout[-3000:]}\n"
                           f"{r.stderr[-3000:]}")
    return r.stdout


def _fixture(contracts: Path, name: str) -> str:
    raw = (contracts / "test" / "fixtures" / f"{name}.runtime.hex").read_text().replace("\n", "")
    return raw if raw.startswith("0x") else "0x" + raw


def _standin_runtimes(work: Path) -> Dict[str, str]:
    proj = work / "standins"
    (proj / "src").mkdir(parents=True, exist_ok=True)
    (proj / "src" / "Standins.sol").write_text(_STANDINS)
    _run(["forge", "build", "--root", str(proj), "--offline", "--use", "0.8.24",
          "--optimize", "--via-ir"], timeout=600)
    out = {}
    for name in ("Multicall3", "ReputationStandIn", "RigToken", "IdentityCardStandIn"):
        art = json.loads((proj / "out" / "Standins.sol" / f"{name}.json").read_text())
        out[name] = art["deployedBytecode"]["object"]
    return out


def _desk_runtime(contracts: Path, source: str, name: str) -> str:
    art = json.loads((contracts / "out" / source / f"{name}.json").read_text())
    return art["deployedBytecode"]["object"]


def start(work: Path) -> Rig:
    """Start anvil, etch the environment, deploy the stack with the desk's Deploy.s.sol, pin it
    with the desk's pin.py. Raises with the tool output on any failure."""
    contracts = contracts_dir()
    assert contracts is not None
    work.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    rig = Rig(url=f"http://127.0.0.1:{port}", contracts=contracts, work=work)
    # 0.024 gwei base fee and no suggested tip: the Robinhood (Arbitrum-stack) fee market
    # (core/wallet/chains.py; eth_maxPriorityFeePerGas answers 0 there, so the rail's own 0.1 gwei
    # floor applies). anvil's defaults (1 gwei tip) would price every call far above the chain's.
    rig.proc = subprocess.Popen(["anvil", "--chain-id", str(CHAIN_ID), "--port", str(port),
                                 "--silent", "--block-base-fee-per-gas", "24000000",
                                 "--gas-limit", "60000000"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                if int(rig.rpc("eth_chainId", []), 16) == CHAIN_ID:
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("anvil did not come up")
        rig.shim = _fee_market_shim(rig.url)
        rig.core_url = f"http://127.0.0.1:{rig.shim.server_port}"
        _build(rig)
        _etch(rig)
        _deploy(rig)
        _pin(rig)
    except BaseException:
        rig.stop()
        raise
    return rig


def _build(rig: Rig) -> None:
    # The desk build: writes only its out/ and cache/ (pin.py compares against out/).
    _run(["forge", "build"], cwd=rig.contracts, timeout=1800)


def _etch(rig: Rig) -> None:
    from eth_account import Account
    c = rig.contracts
    rig.rpc("anvil_setCode", [REGISTRY_6551, _fixture(c, "erc6551_registry")])
    rig.rpc("anvil_setCode", [CREATE2_DEPLOYER, _fixture(c, "create2_deployer")])
    standins = _standin_runtimes(rig.work)
    rig.rpc("anvil_setCode", [MULTICALL3, standins["Multicall3"]])
    rig.rpc("anvil_setCode", [REPUTATION_TESTNET, standins["ReputationStandIn"]])
    rig.rpc("anvil_setCode", [WETH_TESTNET, standins["RigToken"]])
    rig.card_standin = standins["IdentityCardStandIn"]
    # The desk's own ERC-8004 identity stand-in (test/mocks/Mocks.sol, mode OK): register(uri)
    # mints the identity to msg.sender (the account). Its `next` counter lives in storage, which
    # an etch does not carry: set it to 1 so agentId 0 never appears.
    rig.rpc("anvil_setCode", [IDENTITY_TESTNET, _desk_runtime(c, "Mocks.sol", "MockIdentityRegistry")])
    helper = Account.create().address
    rig.fund(helper)
    rig.send(helper, IDENTITY_TESTNET, rig.encode("setNext(uint256)", 1))
    for addr, pin in ((REGISTRY_6551, "d7df998352f46d061e9e27c6a17d5108d7439482cb136c45e0f0733c7bd3da56"),
                      (CREATE2_DEPLOYER, "e0af82ad2e5188285db8ba0b2ae054d13f0fd4fe325c48c7f2dace48454404b9")):
        got = hashlib.sha256(bytes.fromhex(rig.code(addr)[2:])).hexdigest()
        assert got == pin, f"etched {addr} hashes to {got}, not the pin {pin}"


def _deploy(rig: Rig) -> None:
    from eth_account import Account
    owner = Account.create()
    rig.owner = owner.address
    rig.proceeds = Account.create().address
    rig.fund(rig.owner, 1000 * 10 ** 18)
    env = dict(os.environ)
    env.update({"OWNER": rig.owner, "PROCEEDS": rig.proceeds, "FACE_WRITER": rig.owner,
                "POLYROB_DROP_DEPLOY_RECORD": "0",
                "FOUNDRY_BROADCAST": str(rig.work / "broadcast")})
    rig.deploy_log = _run(["forge", "script", "script/Deploy.s.sol", "--rpc-url", rig.url,
                           "--broadcast", "--private-key", "0x" + bytes(owner.key).hex(),
                           "--slow"], cwd=rig.contracts, env=env, timeout=1800)
    line = next(ln for ln in rig.deploy_log.splitlines() if "deployed address" in ln)
    rig.collection = line.split()[-1].lower()
    rig.sale = rig.address_of(rig.collection, "SALE()").lower()
    rig.account_impl = rig.address_of(rig.collection, "ACCOUNT_IMPL()").lower()
    rig.market = rig.address_of(rig.collection, "MARKET()").lower()
    rig.zone = rig.address_of(rig.collection, "ZONE()").lower()
    rig.journal_log = rig.address_of(rig.account_impl, "JOURNAL()").lower()
    # the collection is the last CREATE2 of the ceremony; its block is the deploy block
    rig.deploy_block = rig.head()


def _pin(rig: Rig) -> None:
    out = rig.work / "polyrob.profile.json"
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    rig.pin_log = _run(["python", "-B", "script/pin.py", "--chain", str(CHAIN_ID), "--rpc", rig.url,
                        "--address", rig.collection, "--no-write", "--deploy-block",
                        str(rig.deploy_block), "--with-sale", "--with-transfer-safe",
                        "--profile-out", str(out)], cwd=rig.contracts, env=env, timeout=600)
    rig.profile = json.loads(out.read_text())
    # The real deploy record (deployments/<chain>.json) names journal_log and pin.py copies it
    # into the profile; this rehearsal writes no record, so the same value is added here.
    rig.profile["journal_log"] = rig.journal_log


def write_registry(rig: Rig, path: Path, profiles: Optional[List[dict]] = None) -> Path:
    """The owner's root registry file, read-only (``collection_registry.load`` refuses a pin
    file this process can write)."""
    if path.exists():
        path.chmod(0o644)
    path.write_text(json.dumps({"profiles": profiles if profiles is not None else [rig.profile]}))
    path.chmod(0o444)
    return path


def wl_tree(contracts: Path):
    """The desk's whitelist tree module (``scripts/wl_tree.py``), imported without a bytecode
    cache so the desk tree is not written."""
    import importlib.util
    import sys
    src = contracts.parent / "scripts" / "wl_tree.py"
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location("_desk_wl_tree", src)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = old
    return mod
