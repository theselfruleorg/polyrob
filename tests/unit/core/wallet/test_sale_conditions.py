"""115 W9 / 111 §8.2 — the ONE buyer-conditions builder (``core/wallet/sale_conditions.py``).

* every selector, topic and the EIP-712 ``Listing`` typehash computed from its signature, and the
  kinds equal to the contract of record (``Conditions.sol``) when the desk checkout is beside;
* the shared vectors (``fixtures/sale_conditions_vectors.json``): the same snapshot gives the
  same conditions and rows in core as in the desk reader (``web/checked-sale.js``); with the
  desk checkout and node present the desk reader is RUN on the fixture to prove it is its output;
* the encoders equal the desk's (``buy``, ``Condition[]``, the hashes, the EIP-712 digest, the
  share link);
* the cap per fill, :func:`admissible` (drop only what is not checked anyway), the reader on a
  fake chain (one block, the logs since the deploy block, unreadable is not zero).
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from core.wallet import abi, sale_conditions as S

FIXTURE = Path(__file__).parent / "fixtures" / "sale_conditions_vectors.json"
V = json.loads(FIXTURE.read_text())
DESK = Path(__file__).resolve().parents[5] / "polyrob-desk"


def _desk(*rel):
    p = DESK.joinpath(*rel)
    return p if p.exists() else None


# ------------------------------------------------------------------------------ the literals

@pytest.mark.parametrize("literal, signature", [
    (S.BUY_SELECTOR, S.BUY_SIGNATURE),
    (S.CHECKED_FILL_SELECTOR,
     "checkedFill(((address,address,(uint8,address,uint256,uint256,uint256)[],"
     "(uint8,address,uint256,uint256,uint256,address)[],uint8,uint256,uint256,bytes32,uint256,"
     "bytes32,uint256),uint120,uint120,bytes,bytes),(uint256,uint8,uint256,uint256,bytes32[])[],"
     "bytes32,(address,uint256,address,uint256),(uint8,address,uint256,uint256,bytes)[])"),
    (S.ACCEPT_SELECTOR,
     "accept((address,uint256,bool,address,address,uint256,uint256,uint256,uint256,bytes32,"
     "uint256),bytes,uint256,(uint8,address,uint256,uint256,bytes)[],"
     "(uint8,address,uint256,uint256,bytes)[])"),
    (S.CLAIM_SELECTOR, "claim(address)"),
    (S.OWED_SELECTOR, "owed(address)"),
    (S.LISTING_DIGEST_SELECTOR, "listingDigest((address,uint256,uint256,address,address,uint256,"
                                "uint256,uint256,uint256,bytes32,uint256))"),
    (S.EIP712_DOMAIN_SELECTOR, "eip712Domain()"),
    (S.TRANSFER_COUNT_SELECTOR, "transferCount(uint256)"),
    (S.POSITION_OF_SELECTOR, "positionOf(uint256)"),
    (S.HAS_IDENTITY_SELECTOR, "hasIdentity(uint256)"),
    (S.AGENT_ID_OF_SELECTOR, "agentIdOf(uint256)"),
    (S.IDENTITY_REGISTRY_SELECTOR, "identityRegistry()"),
    (S.OWNER_OF_SELECTOR, "ownerOf(uint256)"),
    (S.GET_APPROVED_SELECTOR, "getApproved(uint256)"),
    (S.GET_AGENT_WALLET_SELECTOR, "getAgentWallet(uint256)"),
    (S.BALANCE_OF_SELECTOR, "balanceOf(address)"),
    (S.BALANCE_OF_ID_SELECTOR, "balanceOf(address,uint256)"),
    (S.DECIMALS_SELECTOR, "decimals()"),
    (S.SYMBOL_SELECTOR, "symbol()"),
    (S.POSITION_LIQUIDITY_SELECTOR, "getPositionLiquidity(uint256)"),
    (S.POOL_AND_POSITION_SELECTOR, "getPoolAndPositionInfo(uint256)"),
    (S.CONDITION_FAILED_SELECTOR, "ConditionFailed(uint256)"),
])
def test_every_selector_is_its_signature(literal, signature):
    assert literal == abi.selector(signature)


@pytest.mark.parametrize("literal, signature", [
    (S.TOPIC_TRANSFER, "Transfer(address,address,uint256)"),
    (S.TOPIC_TRANSFER_SINGLE, "TransferSingle(address,address,address,uint256,uint256)"),
    (S.TOPIC_TRANSFER_BATCH, "TransferBatch(address,address,address,uint256[],uint256[])"),
    (S.TOPIC_TRANSFER_6909, "Transfer(address,address,address,uint256,uint256)"),
    (S.TOPIC_SOLD, "Sold(address,uint256,address,address,address,uint256,uint256)"),
    (S.TOPIC_CHECKED_FILL, "CheckedFill(address,uint256,address,bytes32)"),
    (S.TOPIC_OWED, "Owed(address,uint256)"),
])
def test_every_topic_is_its_signature(literal, signature):
    from eth_utils import keccak
    assert literal == "0x" + keccak(text=signature).hex()


def test_the_listing_typehash_is_its_type_string():
    from eth_utils import keccak
    assert S.LISTING_TYPEHASH == "0x" + keccak(text=S.LISTING_TYPE).hex()
    assert S.LISTING_TYPE == V["encodings"]["listingType"]


def test_the_buyer_selectors_are_the_guards():
    from core.wallet import tx_guard_sale
    assert tx_guard_sale.BUYER_SELECTORS == (S.BUY_SELECTOR, S.CLAIM_SELECTOR,
                                             S.CHECKED_FILL_SELECTOR, S.ACCEPT_SELECTOR)


def test_the_kinds_and_the_typehash_are_the_contracts():
    """Against the contract of record, when the desk checkout is beside this repo."""
    src = _desk("contracts", "src", "sale", "Conditions.sol")
    market = _desk("contracts", "src", "sale", "PolyrobMarket.sol")
    if src is None or market is None:
        pytest.skip("the desk checkout (../polyrob-desk) is not beside this repo")
    text = src.read_text()
    for name, value in S.KINDS.items():
        assert f"uint8 internal constant {name} = {value};" in text, name
    import re
    flat = re.sub(r'"\s*\n\s*"', "", market.read_text())
    assert f'"{S.LISTING_TYPE}"' in flat


# ------------------------------------------------------------------------------ the shared vectors

def _core_case(case):
    o = case["opts"]
    snap = json.loads(json.dumps(case["snapshot"]))
    built = S.build(_ints(snap), tolerance_bps=o.get("toleranceBps", 0),
                    reviewed_erc20=o.get("reviewedErc20", ()),
                    reviewed_hooks=o.get("reviewedHooks", (S.ZERO,)), exclude=o.get("exclude"),
                    any_id=o.get("anyId", False), only_reviewed=o.get("onlyReviewed", False),
                    acted=o.get("acted", ()))
    conds = [[c.kind, c.target, str(c.id), str(c.min), c.data] for c in built.conditions]
    rows = [{"label": r.label, "checked": r.checked, "cond": r.cond, "why": r.why, "key": r.key,
             "known": r.known, "bad": r.bad, "acted": r.acted,
             "min": None if r.min is None else str(r.min)} for r in built.rows]
    return conds, rows


def _ints(snap):
    """The desk's snapshot carries big numbers as strings (JSON); core's reader gives ints."""
    def num(v):
        return None if v is None else int(v)
    for k in ("eth", "positionId"):
        if k in snap:
            snap[k] = num(snap[k])
    if snap.get("position"):
        snap["position"]["liquidity"] = num(snap["position"]["liquidity"])
    if snap.get("identity"):
        snap["identity"]["agentId"] = num(snap["identity"]["agentId"])
    for h in snap.get("holdings") or ():
        for k in ("amount", "id"):
            if k in h:
                h[k] = num(h[k])
        if h.get("position"):
            h["position"]["liquidity"] = num(h["position"]["liquidity"])
    return snap


@pytest.mark.parametrize("case", V["cases"], ids=[c["name"] for c in V["cases"]])
def test_core_builds_the_desk_readers_list(case):
    conds, rows = _core_case(case)
    assert conds == case["expect"]["conditions"]
    assert rows == case["expect"]["rows"]


_GEN_JS = r"""
const fs = require("fs"), path = require("path"), vm = require("vm");
const root = process.argv[2], cases = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const ctx = { TextEncoder, TextDecoder, btoa, atob, crypto: globalThis.crypto }; ctx.window = ctx;
vm.createContext(ctx);
for (const f of ["web/abi.js", "web/checked-sale.js"]) vm.runInContext(fs.readFileSync(path.join(root, f), "utf8"), ctx);
const S = ctx.CheckedSale;
process.stdout.write(JSON.stringify(cases.map((c) => {
  const d = S.defaults(c.snapshot, c.opts);
  return { conditions: d.conditions.map((x) => [Number(x.kind), String(x.target).toLowerCase(), String(x.id),
             String(x.min), String(x.data).toLowerCase()]),
           rows: d.rows.map((r) => ({ label: r.label, checked: !!r.checked, cond: r.cond === undefined ? null : r.cond,
             why: r.why || "", key: r.key === undefined ? null : r.key, known: r.known === undefined ? null : r.known,
             bad: !!r.bad, acted: !!r.acted, min: r.min === undefined ? null : String(r.min) })) };
})));
"""


def test_the_desk_reader_still_builds_the_fixture(tmp_path):
    """The fixture IS the desk reader's output: run it (node) on the same cases."""
    if _desk("web", "checked-sale.js") is None or not shutil.which("node"):
        pytest.skip("the desk checkout (../polyrob-desk/web) or node is not here")
    js, cases = tmp_path / "gen.js", tmp_path / "cases.json"
    js.write_text(_GEN_JS)
    cases.write_text(json.dumps([{k: c[k] for k in ("name", "snapshot", "opts")}
                                 for c in V["cases"]]))
    out = subprocess.run(["node", str(js), str(DESK), str(cases)], capture_output=True, text=True,
                         timeout=60, check=True).stdout
    assert json.loads(out) == [c["expect"] for c in V["cases"]]


# ------------------------------------------------------------------------------ the encoders

E = V["encodings"]


def _listing():
    return S.Listing.of(E["listing_in"])


def test_the_encoders_are_the_desks():
    conds = [S.Condition.of(c) for c in E["conditions_in"]]
    promise = [S.Condition.of(c) for c in E["promise_in"]]
    assert S.encode_conditions(conds) == E["conditions"]
    assert S.conditions_hash(conds) == E["conditionsHash"]
    assert S.conditions_hash([]) == E["emptyHash"]
    assert _listing().promiseHash == S.conditions_hash(promise)
    data = S.encode_buy(_listing(), E["sig"], promise, conds, E["inputs"]["recipient"])
    assert data == E["buy"]
    assert S.encode_claim(E["inputs"]["recipient"]) == E["claim"]
    dom = E["marketDomain"]
    assert S.domain_separator(dom["name"], dom["version"], E["inputs"]["chainId"],
                              E["inputs"]["market"]) == E["domainSeparator"]
    assert S.listing_digest(_listing(), name=dom["name"], version=dom["version"],
                            chain_id=E["inputs"]["chainId"],
                            market=E["inputs"]["market"]) == E["listingDigest"]


def test_buy_calldata_decodes_exactly_and_nothing_else():
    call = S.decode_buy(E["buy"])
    assert call.listing == _listing() and call.sig == E["sig"]
    assert call.recipient == E["inputs"]["recipient"] and len(call.conditions) == 3
    for bad in (E["buy"] + "00", E["buy"][:-2], "0x12345678" + E["buy"][10:]):
        with pytest.raises(ValueError):
            S.decode_buy(bad)
    assert S.decode_claim(E["claim"]) == E["inputs"]["recipient"]
    with pytest.raises(ValueError):
        S.decode_claim(E["claim"] + "00")


def test_the_share_link_is_the_desks():
    for text in (E["link_code"], "https://example.invalid/buy.html?id=7#x=" + E["link_code"],
                 json.dumps(E["link_payload"])):
        p = S.decode_link(text)
        assert p["k"] == "m" and S.Listing.of(p["listing"]) == _listing()
    assert S.decode_link(S.encode_link(E["link_payload"])) == S.decode_link(E["link_code"])
    for bad in ('{"v":2,"k":"m"}', "%%%", ""):
        with pytest.raises(ValueError):
            S.decode_link(bad)


def test_a_condition_failed_revert_gives_its_index():
    assert S.failed_condition_index("0x72fc940b" + format(4, "064x")) == 4
    assert S.failed_condition_index("0x5cd5d233") is None
    assert S.failed_condition_index(None) is None


# ------------------------------------------------------------------------------ cap, admissible

def _snap(n_junk=0, **kw):
    snap = {"account": "0x" + "cc" * 20, "positionManager": "0x" + "58" * 20,
            "identityRegistry": "0x" + "80" * 20, "eth": 10 ** 18, "positionId": 0,
            "holdings": [{"kind": "erc20", "token": "0x" + "bb" * 20, "amount": 7, "symbol": "TOK"}]
            + [{"kind": "erc20", "token": "0x" + format(i + 1, "040x"), "amount": 1, "symbol": "J"}
               for i in range(n_junk)]}
    snap.update(kw)
    return snap


def test_the_cap_keeps_known_holdings_and_labels_the_rest_left_out():
    built = S.build(_snap(n_junk=60), reviewed_erc20=["0x" + "bb" * 20])
    assert len(built.conditions) == S.MAX_CONDITIONS
    assert built.conditions[0].kind == S.ETH and built.conditions[1].target == "0x" + "bb" * 20
    left = [r for r in built.rows if r.why == S.CAPPED_WHY]
    assert len(left) == 62 - S.MAX_CONDITIONS and all(not r.checked and r.cond is None for r in left)
    for r in built.rows:
        if r.checked:
            assert built.conditions[r.cond] is not None


def test_admissible_accepts_the_list_and_drops_of_unchecked_value_only():
    built = S.build(_snap(n_junk=3), reviewed_erc20=["0x" + "bb" * 20])
    full = built.conditions
    assert S.admissible(full, built) is None
    assert S.admissible([full[0], full[1], full[3], full[4]], built) is None   # one junk dropped
    why = S.admissible(full[1:], built)          # ETH dropped
    assert why and "missing or changed" in why
    lower = list(full)
    lower[0] = S.Condition(S.ETH, S.ZERO, 0, full[0].min - 1)   # a lower minimum
    assert "missing or changed" in S.admissible(lower, built)
    assert "does not build" in S.admissible(full + [full[1]], built)
    assert S.admissible([], S.build(_snap(eth=0, holdings=[]))) is not None


def test_a_bad_tolerance_refuses():
    with pytest.raises(ValueError):
        S.build(_snap(), tolerance_bps=500)


# ------------------------------------------------------------------------------ the reader

ACCT = "0x" + "cc" * 20
COLL = "0x" + "8b" * 20
REG = "0x" + "80" * 20
TOK = "0x" + "bb" * 20
NFT = "0x" + "dd" * 20
ITEM = "0x" + "ee" * 20


def _w(n):
    return "0x" + format(int(n), "064x")


class ChainRpc:
    """One block (99) of a fake chain: the account holds ETH, TOK (an ERC-20 seen in a log), an
    NFT, an ERC-1155 item, and the token has an identity. Records every block tag it is asked."""

    def __init__(self, *, fail_logs=False, identity=True, tok_balance=7):
        self.tags, self.fail_logs, self.identity, self.tok_balance = [], fail_logs, identity, tok_balance
        self.log_calls = []

    def __call__(self, method, params):
        if method == "eth_blockNumber":
            return hex(99)
        if method == "eth_getBalance":
            self.tags.append(params[1])
            return hex(5 * 10 ** 17)
        if method == "eth_getLogs":
            f = params[0]
            self.log_calls.append((int(f["fromBlock"], 16), int(f["toBlock"], 16)))
            if self.fail_logs:
                raise RuntimeError("range too large")
            t0 = f["topics"][0]
            if t0 == S.TOPIC_TRANSFER:
                return [{"address": TOK, "topics": [t0, _w(1), _w(int(ACCT, 16))], "data": _w(7)},
                        {"address": NFT, "topics": [t0, _w(1), _w(int(ACCT, 16)), _w(42)],
                         "data": "0x"},
                        {"address": REG, "topics": [t0, _w(0), _w(int(ACCT, 16)), _w(9)],
                         "data": "0x"}]
            if isinstance(t0, list):
                return [{"address": ITEM, "topics": [S.TOPIC_TRANSFER_SINGLE, _w(1), _w(1),
                                                     _w(int(ACCT, 16))],
                         "data": _w(3) + format(4, "064x")}]
            return []
        if method == "eth_call":
            to, data = params[0]["to"].lower(), params[0]["data"]
            self.tags.append(params[1])
            sel = data[:10]
            if to == COLL:
                return {S.HAS_IDENTITY_SELECTOR: _w(1 if self.identity else 0),
                        S.IDENTITY_REGISTRY_SELECTOR: _w(int(REG, 16)),
                        S.AGENT_ID_OF_SELECTOR: _w(9), S.POSITION_OF_SELECTOR: _w(0)}[sel]
            if to == REG:
                return {S.OWNER_OF_SELECTOR: _w(int(ACCT, 16)), S.GET_APPROVED_SELECTOR: _w(0),
                        S.GET_AGENT_WALLET_SELECTOR: _w(int(ACCT, 16)),
                        S.SYMBOL_SELECTOR: "0x"}[sel]
            if to == TOK:
                if sel == S.BALANCE_OF_SELECTOR:
                    if self.tok_balance is None:
                        raise RuntimeError("execution reverted")
                    return _w(self.tok_balance)
                return {S.DECIMALS_SELECTOR: _w(6),
                        S.SYMBOL_SELECTOR: _w(32) + _w(3)[2:] + b"TOK".hex().ljust(64, "0")}[sel]
            if to == NFT:
                return {S.OWNER_OF_SELECTOR: _w(int(ACCT, 16)), S.SYMBOL_SELECTOR: "0x"}[sel]
            if to == ITEM:
                return {S.BALANCE_OF_ID_SELECTOR: _w(4), S.SYMBOL_SELECTOR: "0x"}[sel]
        raise AssertionError((method, params))


def test_the_reader_reads_every_holding_at_one_block():
    rpc = ChainRpc()
    snap, built = S.read_and_build(rpc, collection=COLL, token_id=3, account=ACCT, deploy_block=10,
                                   ctx=S.ReadContext(reviewed_erc20=(TOK,)))
    assert set(rpc.tags) == {hex(99)} and snap["block"] == 99
    assert snap["coverage"] == "complete" and all(lo >= 10 for lo, _hi in rpc.log_calls)
    kinds = [(c.kind, c.target, c.id, c.min) for c in built.conditions]
    assert kinds == [(S.IDENTITY, REG, 0, 0), (S.ETH, S.ZERO, 0, 5 * 10 ** 17), (S.ERC20, TOK, 0, 7),
                     (S.ERC721, NFT, 42, 0), (S.ERC1155, ITEM, 3, 4)]
    tok = next(r for r in built.rows if r.token == TOK)
    assert tok.label == "TOK" and tok.known is True and tok.decimals == 6
    lines = S.card_lines(snap, built)
    assert any("CHECKED" in ln for ln in lines) and "every block" in lines[-1]


def test_an_unread_scan_is_named_and_an_unreadable_balance_is_not_zero():
    snap, built = S.read_and_build(ChainRpc(fail_logs=True, tok_balance=None), collection=COLL,
                                   token_id=3, account=ACCT, deploy_block=0,
                                   ctx=S.ReadContext(reviewed_erc20=(TOK,)))
    assert snap["coverage"] == "incomplete" and "eth_getLogs" in snap["why"]
    row = next(r for r in built.rows if r.token == TOK)
    assert row.bad and not row.checked and row.amount is None and row.why == S.UNREADABLE_WHY
    assert "NOT every block was read" in S.card_lines(snap, built)[-1]


def test_no_identity_no_identity_condition():
    snap, built = S.read_and_build(ChainRpc(identity=False), collection=COLL, token_id=3,
                                   account=ACCT, deploy_block=0, ctx=S.ReadContext())
    assert snap["identity"] is None and S.IDENTITY not in [c.kind for c in built.conditions]
    # the registry's NFT (#9) is then an ordinary NFT holding, checked by ownership
    assert (S.ERC721, REG, 9) in [(c.kind, c.target, c.id) for c in built.conditions]


def test_the_log_scan_halves_its_window_on_an_error(monkeypatch):
    monkeypatch.setattr(S, "LOG_WINDOW", 8)
    monkeypatch.setattr(S, "LOG_WINDOW_FLOOR", 2)
    calls = []

    def rpc(method, params):
        lo, hi = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
        calls.append((lo, hi))
        if hi - lo >= 4:
            raise RuntimeError("too many")
        return [{"lo": lo}]
    logs, ok, why = S.scan_logs(rpc, {"topics": []}, 0, 9)
    assert ok and why == "" and [x["lo"] for x in logs] == [0, 4, 8]
    logs, ok, why = S.scan_logs(lambda m, p: (_ for _ in ()).throw(RuntimeError("x")),
                                {"topics": []}, 0, 9)
    assert not ok and "failed" in why


def test_the_module_imports_no_guard_module():
    import ast
    tree = ast.parse(Path(S.__file__).read_text())
    names = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module == "core.wallet":
            imported |= {a.name for a in n.names}
    assert imported <= {"abi", "chains", "dex_registry"}, imported
    assert not any("tx_guard" in m or "collection_registry" in m or "simulation" in m
                   for m in names), names
