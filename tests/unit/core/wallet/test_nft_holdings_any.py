"""124 W3 — the holdings watch for any collection: an ERC-721 of an UNPINNED contract that
arrives is discovered (N/A pin), an NFT that arrives INSIDE a held account is reported nested
(N1), and an id unreadable for long no longer holds the cursor and is reported once a day (O2)."""
import pytest

from core.wallet import abi, collection_registry, erc6551, holdings_state, nft_holdings
from tests.collection_pins import pin

NFT = "0x4444444444444444444444444444444444444444"
OTHER = "0x5555555555555555555555555555555555555555"
TREASURY = "0x" + "2a" * 20
STRANGER = "0x" + "77" * 20
ACCOUNT = erc6551.account_address(4663, NFT, 3, implementation=erc6551.ACCOUNT_V3_IMPL)


def _w(addr):
    return "0x" + "0" * 24 + addr[2:].lower()


class Chain:
    def __init__(self):
        self.logs = []                 # (block, contract, id, to)
        self.owners = {}               # (contract, id) -> owner
        self.broken = set()

    def __call__(self, method, params):
        if method == "eth_blockNumber":
            return hex(0x100)
        if method == "eth_getBalance":
            return "0x0"
        if method == "eth_getCode":
            return "0x6080604052" if params[0].lower() in (NFT.lower(), OTHER.lower()) else "0x"
        if method == "eth_getLogs":
            q = params[0]
            lo, hi = int(q["fromBlock"], 16), int(q["toBlock"], 16)
            want = q["topics"][2]
            return [{"address": c, "blockNumber": hex(b), "logIndex": "0x0",
                     "transactionHash": "0x" + format(b, "064x"),
                     "topics": [nft_holdings.TOPIC_TRANSFER, _w(STRANGER), _w(to), hex(i)], "data": "0x"}
                    for (b, c, i, to) in self.logs
                    if lo <= b <= hi and _w(to) == want and (not q.get("address") or q["address"].lower() == c.lower())]
        assert method == "eth_call", method
        to, data = params[0]["to"].lower(), params[0]["data"]
        if data.startswith("0x01ffc9a7"):
            return "0x" + f"{1:064x}"
        if data.startswith(abi.selector("ownerOf(uint256)")):
            i = int(data[10:], 16)
            if (to, i) in self.broken:
                raise RuntimeError("rpc down")
            return _w(self.owners.get((to, i), STRANGER))
        raise RuntimeError("execution reverted")


@pytest.fixture
def chain(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    pin(monkeypatch)
    collection_registry._CHAIN_PROFILES.clear()
    monkeypatch.setattr(nft_holdings, "_discovery_chains", lambda state: ["robinhood"])
    return Chain()


def _scan(rpc, state, now=1000.0):
    return nft_holdings.scan(TREASURY, state=state, rpc_for=lambda c: rpc, now=lambda: now)


def test_an_nft_of_an_unpinned_collection_is_discovered_and_tracked(chain):
    chain.logs = [(0x80, NFT, 3, TREASURY)]
    chain.owners = {(NFT.lower(), 3): TREASURY}
    state = holdings_state.load_state(holdings_state.state_path())
    res = _scan(chain, state)
    assert [(r["collection"], r["token_id"]) for r in res.arrived] == [(NFT.lower(), 3)]
    assert res.arrived[0]["account"].lower() == ACCOUNT.lower()


def test_an_nft_that_arrives_inside_a_held_account_is_reported_nested(chain):
    chain.logs = [(0x80, NFT, 3, TREASURY), (0x90, OTHER, 9, ACCOUNT)]
    chain.owners = {(NFT.lower(), 3): TREASURY}
    state = holdings_state.load_state(holdings_state.state_path())
    res = _scan(chain, state)
    assert [(r["collection"], r["token_id"]) for r in res.nested] == [(OTHER.lower(), 9)]
    assert "sweep it to the treasury" in nft_holdings.nested_text(res.nested[0])
    assert _scan(chain, state).nested == []          # once


def test_an_id_unreadable_for_long_moves_the_cursor_and_is_reported_daily(chain):
    chain.logs = [(0x80, NFT, 3, TREASURY)]
    chain.owners = {(NFT.lower(), 3): TREASURY}
    state = holdings_state.load_state(holdings_state.state_path())
    _scan(chain, state)
    chain.broken = {(NFT.lower(), 3)}
    for n in range(nft_holdings.UNREADABLE_PASSES - 1):
        assert _scan(chain, state).unreadable == []
    res = _scan(chain, state, now=2000.0)
    assert len(res.unreadable) == 1 and "unreadable since" in nft_holdings.unreadable_text(res.unreadable[0])
    assert _scan(chain, state, now=3000.0).unreadable == []          # once a day
    assert f"4663:{NFT.lower()}:3" in state["tracked"]                 # unknown is not lost


# --- audit 2026-11-21 M-3: a capped discovery, an honest notice, no unpinned default ---------

def _spam(n):
    return ["0x" + format(0x9000 + i, "040x") for i in range(n)]


class SpamChain(Chain):
    def __init__(self, contracts):
        super().__init__()
        self.contracts = {c.lower() for c in contracts}

    def __call__(self, method, params):
        if method == "eth_getCode" and params[0].lower() in self.contracts:
            return "0x6080604052"
        return super().__call__(method, params)


def test_new_unpinned_collections_are_capped_per_chain_per_day_with_one_line(chain):
    many = _spam(nft_holdings.DISCOVERY_DAILY_CAP + 7)
    rpc = SpamChain(many)
    rpc.logs = [(0x80 + i, c, 1, TREASURY) for i, c in enumerate(many)]
    rpc.owners = {(c.lower(), 1): TREASURY for c in many}
    state = holdings_state.load_state(holdings_state.state_path())
    res = _scan(rpc, state)
    assert len(res.arrived) == nft_holdings.DISCOVERY_DAILY_CAP
    assert res.skipped == [("robinhood", 7)]
    line = nft_holdings.skipped_text(*res.skipped[0])
    assert "7 more collection" in line and "/nft trust" in line
    # the next pass of the same day: no new line, nothing more watched
    rpc.logs.append((0xf0, _spam(30)[-1], 1, TREASURY))
    res2 = _scan(rpc, state)
    assert res2.skipped == [] and res2.arrived == []
    # the next day the cap starts again
    rpc.contracts.add(_spam(30)[-1].lower())
    rpc.owners[(_spam(30)[-1].lower(), 1)] = TREASURY
    state["discovered"]["robinhood"] = 0xf0        # the fake head never moves: re-read the new log
    res3 = _scan(rpc, state, now=1000.0 + 86_400)
    assert [r["collection"] for r in res3.arrived] == [_spam(30)[-1].lower()]


def test_an_unpinned_arrival_states_the_truth_not_you_gave_me(chain):
    chain.logs = [(0x80, NFT, 3, TREASURY)]
    chain.owners = {(NFT.lower(), 3): TREASURY}
    state = holdings_state.load_state(holdings_state.state_path())
    row, = _scan(chain, state).arrived
    text = nft_holdings.arrived_text(row)
    assert row["pinned"] is False
    assert "did not pin" in text and "collection contract controls who owns it" in text
    assert "You gave me" not in text and "as its owner" not in text


def test_an_unpinned_nft_is_never_the_select_default(chain, monkeypatch):
    from core.wallet.nft_account import NftAccountError
    chain.logs = [(0x80, NFT, 3, TREASURY)]
    chain.owners = {(NFT.lower(), 3): TREASURY}
    path = holdings_state.state_path()
    state = holdings_state.load_state(path)
    _scan(chain, state)
    holdings_state.save_state(path, state)
    called = []
    monkeypatch.setattr("core.wallet.nft_account.resolve", lambda *a, **k: called.append(k))
    with pytest.raises(NftAccountError, match="never the default"):
        nft_holdings.select("robinhood", None, rpc=chain, treasury=TREASURY)
    assert called == []


# --- the watch keeps what it could not read, and says what it skips ---------------------------

class ProfileChain(Chain):
    """``supportsInterface(ERC-721)`` answers ``erc721`` (True / False), or the read fails."""

    def __init__(self, erc721="yes"):
        super().__init__()
        self.erc721 = erc721

    def __call__(self, method, params):
        if method == "eth_call" and params[0]["data"].startswith("0x01ffc9a780ac58cd"):
            if self.erc721 == "down":
                raise RuntimeError("rpc down")
            return "0x" + f"{1 if self.erc721 == 'yes' else 0:064x}"
        return super().__call__(method, params)


def test_a_failed_profile_read_keeps_the_collection_and_the_next_pass_tracks_it(chain):
    rpc = ProfileChain(erc721="down")
    rpc.logs = [(0x80, NFT, 3, TREASURY)]
    rpc.owners = {(NFT.lower(), 3): TREASURY}
    state = holdings_state.load_state(holdings_state.state_path())
    res = _scan(rpc, state)
    assert res.arrived == [] and any("try again on the next pass" in e for e in res.errors)
    assert f"4663:{NFT.lower()}" in state["collections"]            # kept, not dropped
    rpc.erc721 = "yes"
    assert [r["token_id"] for r in _scan(rpc, state).arrived] == [3]


def test_a_contract_that_says_it_is_not_an_erc721_is_dropped(chain):
    rpc = ProfileChain(erc721="no")
    rpc.logs = [(0x80, NFT, 3, TREASURY)]
    state = holdings_state.load_state(holdings_state.state_path())
    res = _scan(rpc, state)
    assert any("is not watched" in e for e in res.errors)
    assert f"4663:{NFT.lower()}" not in state["collections"]


def test_an_untracked_arrival_unreadable_for_long_is_reported_too(chain):
    chain.logs = [(0x80, NFT, 3, TREASURY)]
    chain.broken = {(NFT.lower(), 3)}                 # never read: never tracked
    state = holdings_state.load_state(holdings_state.state_path())
    for _ in range(nft_holdings.UNREADABLE_PASSES - 1):
        assert _scan(chain, state).unreadable == []
    row, = _scan(chain, state, now=2000.0).unreadable
    text = nft_holdings.unreadable_text(row)
    assert "was sent to the treasury" in text and "#3" in text
    assert f"4663:{NFT.lower()}:3" not in state["tracked"]


def test_an_unreadable_pin_store_skips_discovery_so_no_pinned_arrival_is_capped(chain, monkeypatch):
    from core.wallet import collection_registry

    def broken():
        raise collection_registry.CollectionRegistryError("pins unreadable")
    monkeypatch.setattr(collection_registry, "profiles", broken)
    chain.logs = [(0x80, NFT, 3, TREASURY)]
    state = holdings_state.load_state(holdings_state.state_path())
    res = _scan(chain, state)
    assert any("pins cannot be read" in e for e in res.errors)
    assert "robinhood" not in state.get("discovered", {})          # the cursor waits
    assert not state.get("discovery_cap") and not state.get("collections")


def test_an_nft_nested_again_after_it_left_is_reported_again(chain):
    chain.logs = [(0x80, NFT, 3, TREASURY), (0x90, OTHER, 9, ACCOUNT)]
    chain.owners = {(NFT.lower(), 3): TREASURY}
    state = holdings_state.load_state(holdings_state.state_path())
    assert len(_scan(chain, state).nested) == 1
    # swept out, then sent into the account again: a new Transfer log (another block)
    chain.logs.append((0xa0, OTHER, 9, ACCOUNT))
    state["nested_scanned"] = {ACCOUNT.lower(): 0x95}
    again = _scan(chain, state).nested
    assert [(r["collection"], r["token_id"]) for r in again] == [(OTHER.lower(), 9)]
