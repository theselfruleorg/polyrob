"""069 v4 A4 — the holdings watch: an NFT of a pinned collection that ARRIVES on the treasury is
reported once and tracked; a tracked one that LEAVES is reported once and dropped; an
unreadable ``ownerOf`` is unknown, never a loss. Deterministic, no model turn."""
import asyncio

import pytest

from core.wallet import abi, erc6551, nft_holdings
from core.wallet.nft_account import NftAccountError
from tests.collection_pins import CODE, pin, profile

PINNED = "0xC011000000000000000000000000000000000001"
TREASURY = "0x" + "2a" * 20
STRANGER = "0x" + "77" * 20
SEL_OWNER_OF = abi.selector("ownerOf(uint256)")


def _w(addr):
    return "0x" + "0" * 24 + addr[2:].lower()


class ChainRpc:
    def __init__(self):
        self.head = 0x100
        self.owners = {}             # id -> owner
        self.transfers = []          # (block, id, to)
        self.broken_owner = set()
        self.log_queries = []

    def __call__(self, method, params):
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getLogs":
            q = params[0]
            lo, hi = int(q["fromBlock"], 16), int(q["toBlock"], 16)
            self.log_queries.append((lo, hi))
            return [{"address": PINNED, "blockNumber": hex(b), "logIndex": "0x0",
                     "topics": [nft_holdings.TOPIC_TRANSFER, _w(STRANGER), _w(to), hex(i)],
                     "data": "0x"}
                    for (b, i, to) in self.transfers
                    if lo <= b <= hi and to.lower() == TREASURY.lower()]
        if method == "eth_call":
            data = params[0]["data"]
            assert data.startswith(SEL_OWNER_OF)
            token_id = int(data[10:], 16)
            if token_id in self.broken_owner:
                raise RuntimeError("rpc down")
            return _w(self.owners.get(token_id, STRANGER))
        if method == "eth_getCode":
            return CODE if params[0].lower() == PINNED.lower() else "0x" + "60" * 45
        raise AssertionError(method)


@pytest.fixture
def chain(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("POLYROB_INSTANCE_ID", raising=False)
    pin(monkeypatch, profile(PINNED, journal_prefix="POLYROB"))
    return ChainRpc()


def _scan(rpc, state):
    return nft_holdings.scan(TREASURY, state=state, rpc_for=lambda c: rpc, now=lambda: 1000.0)


def test_an_arrival_is_found_by_its_transfer_and_confirmed_by_owner_of(chain):
    chain.transfers = [(0x50, 3, TREASURY), (0x60, 5, TREASURY)]
    chain.owners = {3: TREASURY, 5: STRANGER}          # 5 came and went again
    state = nft_holdings.load_state(nft_holdings.state_path())
    res = _scan(chain, state)
    assert [r["token_id"] for r in res.arrived] == [3] and res.lost == []
    row = res.arrived[0]
    assert row["account"] == erc6551.account_address(4663, PINNED, 3)
    assert row["label"] == "POLYROB #3" and row["chain"] == "robinhood"
    assert state["scanned"][f"4663:{PINNED.lower()}"] == 0x101
    assert "You gave me POLYROB #3" in nft_holdings.arrived_text(row)
    # the next pass scans only new blocks and reports nothing new
    chain.head = 0x180
    res = _scan(chain, state)
    assert res.arrived == [] and res.kept == 1 and chain.log_queries[-1] == (0x101, 0x180)


def test_a_loss_is_reported_once_and_dropped(chain):
    chain.transfers = [(0x50, 3, TREASURY)]
    chain.owners = {3: TREASURY}
    state = nft_holdings.load_state(nft_holdings.state_path())
    _scan(chain, state)
    chain.owners = {3: STRANGER}
    res = _scan(chain, state)
    assert [r["token_id"] for r in res.lost] == [3]
    assert state["tracked"] == {} and state["lost"][-1]["new_owner"].lower() == STRANGER
    assert "is no longer mine" in nft_holdings.lost_text(res.lost[0])
    assert _scan(chain, state).lost == []            # once


def test_an_unreadable_owner_is_unknown_not_lost(chain):
    chain.transfers = [(0x50, 3, TREASURY)]
    chain.owners = {3: TREASURY}
    state = nft_holdings.load_state(nft_holdings.state_path())
    _scan(chain, state)
    chain.broken_owner = {3}
    chain.head = 0x200
    res = _scan(chain, state)
    assert res.lost == [] and res.errors and f"4663:{PINNED.lower()}:3" in state["tracked"]
    assert state["scanned"][f"4663:{PINNED.lower()}"] == 0x101      # not advanced: re-scan


def test_an_untrusted_registry_changes_nothing(chain, monkeypatch):
    from core.wallet import collection_registry

    def boom():
        raise collection_registry.CollectionRegistryError("writable")
    monkeypatch.setattr(collection_registry, "profiles", boom)
    state = {"tracked": {"k": {"token_id": 1}}, "scanned": {}, "lost": []}
    res = _scan(chain, state)
    assert res.errors and state["tracked"] == {"k": {"token_id": 1}}


def test_tick_tells_the_owner_once_per_change(chain):
    chain.transfers = [(0x50, 3, TREASURY)]
    chain.owners = {3: TREASURY}
    told = []

    async def notify(container, user_id, text):
        told.append(text)

    def run():
        return asyncio.run(nft_holdings.tick(None, treasury=TREASURY, rpc_for=lambda c: chain,
                                             notify=notify))
    run()
    run()
    assert len(told) == 1 and "You gave me POLYROB #3" in told[0]
    chain.owners = {3: STRANGER}
    run()
    run()
    assert len(told) == 2 and "no longer mine" in told[1]


def test_an_unreadable_state_file_is_not_rebuilt(chain):
    path = nft_holdings.state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    told = []

    async def notify(*a):
        told.append(a)
    res = asyncio.run(nft_holdings.tick(None, treasury=TREASURY, rpc_for=lambda c: chain,
                                        notify=notify))
    assert res.errors and told == [] and path.read_text() == "{not json"


def test_select_defaults_to_the_only_tracked_nft(chain):
    with pytest.raises(NftAccountError, match="holds no tracked NFT"):
        nft_holdings.select("robinhood", None, rpc=chain, treasury=TREASURY)
    chain.transfers = [(0x50, 3, TREASURY), (0x51, 4, TREASURY)]
    chain.owners = {3: TREASURY}
    asyncio.run(nft_holdings.tick(None, treasury=TREASURY, rpc_for=lambda c: chain,
                                  notify=lambda *a: asyncio.sleep(0)))
    held = nft_holdings.select("robinhood", None, rpc=chain, treasury=TREASURY)
    assert held.token_id == 3 and held.account == erc6551.account_address(4663, PINNED, 3)
    chain.transfers.append((0x101, 4, TREASURY))       # 4 arrives (again) later
    chain.owners = {3: TREASURY, 4: TREASURY}
    chain.head = 0x101
    asyncio.run(nft_holdings.tick(None, treasury=TREASURY, rpc_for=lambda c: chain,
                                  notify=lambda *a: asyncio.sleep(0)))
    with pytest.raises(NftAccountError, match="name one"):
        nft_holdings.select("robinhood", None, rpc=chain, treasury=TREASURY)
    assert nft_holdings.select("robinhood", "4", rpc=chain, treasury=TREASURY).token_id == 4


def test_overview_lists_accounts_with_their_approvals_and_reports_changes(chain):
    chain.transfers = [(0x50, 3, TREASURY)]
    chain.owners = {3: TREASURY}

    def rpc(method, params):
        if method == "eth_getBalance":
            return hex(5 * 10 ** 15)
        return chain(method, params)
    text = nft_holdings.overview(treasury=TREASURY, rpc_for=lambda c: rpc)
    assert "NEW: You gave me POLYROB #3" in text
    assert erc6551.account_address(4663, PINNED, 3) in text
    assert "native:  0.005000" in text and "approvals: none (complete" in text
    assert "NEW:" not in nft_holdings.overview(treasury=TREASURY, rpc_for=lambda c: rpc)
