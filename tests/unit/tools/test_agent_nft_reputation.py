"""115 W5 — the ERC-8004 reputation of a held NFT's identity is READ from the chain.

The agentId comes from the collection's own views; the registry's ``getSummary`` reverts on an
empty client list, so it is never called without a client; an unreadable read is UNREADABLE,
never "no feedback"; the line always names the clients (a holder can rate its own agent).
"""
from core.wallet import abi, erc8004
from tests.unit.tools.test_agent_nft_polyrob_view import (  # noqa: F401
    PINNED, PolyrobChain, _no_package, _w, armed)
from tests.unit.tools.test_agent_nft_withdraw import _run, _tool
from core.wallet import erc8004_reputation as reputation
from tools.agent_nft.tool import SnapshotParams

REP = erc8004.resolve_reputation_registry("robinhood")
C1, C2 = "0x" + "c1" * 20, "0x" + "c2" * 20
SEL = {s: abi.selector(s) for s in (
    "hasIdentity(uint256)", "agentIdOf(uint256)", "getClients(uint256)",
    "getSummary(uint256,address[],string,string)", "balanceOf(address)")}


def _addresses(items):
    return "0x" + f"{32:064x}" + f"{len(items):064x}" + "".join("0" * 24 + a[2:] for a in items)


class RepChain(PolyrobChain):
    def __init__(self, owner, *, clients=(), has=True, broken=False):
        super().__init__(owner)
        self.clients, self.has, self.broken = clients, has, broken

    def __call__(self, method, params, *a, **k):
        if method == "eth_call":
            sel, to = params[0]["data"][:10], params[0]["to"].lower()
            if sel == SEL["balanceOf(address)"]:
                self.reads.append(sel)
                return _w(1 if self.has else 0)
            if sel == SEL["hasIdentity(uint256)"] and to == PINNED.lower():
                return _w(int(self.has))
            if sel == SEL["agentIdOf(uint256)"] and to == PINNED.lower():
                return _w(41)
            if to == REP.lower():
                self.reads.append(sel)
                if self.broken:
                    raise RuntimeError("rpc down")
                if sel == SEL["getClients(uint256)"]:
                    return _addresses(self.clients)
                if sel == SEL["getSummary(uint256,address[],string,string)"]:
                    assert self.clients, "getSummary reverts on an empty client list"
                    return _w(2) + _w(190)[2:] + _w(0)[2:]
        return super().__call__(method, params, *a, **k)


def _snapshot(armed_signer, chain):
    tool, _ = _tool(armed_signer, chain)
    res = _run(_no_package(tool).agent_nft_snapshot(SnapshotParams(chain="robinhood", nft="3")))
    assert res.error is None, res.error
    return res.extracted_content


def test_the_selectors_are_the_registry_and_collection_signatures():
    assert SEL["getSummary(uint256,address[],string,string)"] == "0x81bbba58"
    assert SEL["getClients(uint256)"] == "0x42dd519c"
    assert SEL["agentIdOf(uint256)"] == "0x540a90d0"
    assert SEL["hasIdentity(uint256)"] == "0x83aa3bcc"


def test_feedback_is_shown_with_its_clients(armed):  # noqa: F811
    text = _snapshot(armed, RepChain(armed.address, clients=(C1, C2)))
    assert "reputation: agentId 41: 2 feedback entries, summed value 190, from 2 client(s)" in text
    assert C1 in text.lower() and C2 in text.lower() and "a count is not a score" in text


def test_no_client_means_no_summary_call(armed):  # noqa: F811
    chain = RepChain(armed.address, clients=())
    text = _snapshot(armed, chain)
    assert "reputation: agentId 41: no feedback on chain" in text
    assert SEL["getSummary(uint256,address[],string,string)"] not in chain.reads


def test_an_unreadable_registry_is_not_no_feedback(armed):  # noqa: F811
    text = _snapshot(armed, RepChain(armed.address, clients=(C1,), broken=True))
    assert "reputation: unreadable" in text and "no feedback" not in text
    assert "reputation: rpc down" in text


def test_an_account_without_an_identity_has_no_reputation_line(armed):  # noqa: F811
    chain = RepChain(armed.address, has=False)
    text = _snapshot(armed, chain)
    assert "reputation:" not in text
    assert SEL["getClients(uint256)"] not in chain.reads


def test_the_value_uses_the_registry_decimals():
    r = reputation.Reputation(7, (C1,), count=1, value=455, decimals=2)
    assert "summed value 4.55" in r.line()
