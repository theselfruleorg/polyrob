"""115 W7 — the holdings watch reads sale readiness for an account kind with a sale-ready view:
READY FOR SALE and READINESS LOST (no transfer) are reported once each, an unreadable read
changes nothing, and an NFT that leaves while ready is reported as SOLD OR MOVED with the
receipt facts (the pinned market's Sold price only when that event is in the receipt)."""
import asyncio

import pytest

from core.wallet import abi, nft_holdings, sale_acts as A
from tests.collection_pins import MARKET, pin, polyrob_profile

PINNED = "0xC011000000000000000000000000000000000001"
TREASURY = "0x" + "2a" * 20
BUYER = "0x" + "77" * 20
TX = "0x" + "ab" * 32


def _w(v):
    return "0x" + "0" * 24 + v[2:].lower() if isinstance(v, str) else "0x" + format(int(v), "064x")


class Chain:
    def __init__(self):
        self.owner, self.ready, self.sold = TREASURY, False, None

    def __call__(self, method, params):
        if method == "eth_blockNumber":
            return hex(0x100)
        if method == "eth_getLogs":
            q = params[0]
            if q["topics"][1] is None:     # arrivals: Transfer(*, treasury, *)
                return [{"address": PINNED, "blockNumber": "0x5", "logIndex": "0x0",
                         "topics": [nft_holdings.TOPIC_TRANSFER, _w("0x" + "00" * 20),
                                    _w(TREASURY), _w(3)], "data": "0x"}]
            return [{"address": PINNED, "blockNumber": "0x80", "transactionHash": TX,
                     "topics": [nft_holdings.TOPIC_TRANSFER, _w(TREASURY), _w(self.owner), _w(3)],
                     "data": "0x"}]
        if method == "eth_getTransactionReceipt":
            if self.sold is None:
                return {"logs": []}
            data = (_w(BUYER) + _w("0x" + "00" * 20)[2:] + _w(self.sold)[2:] + _w(0)[2:])
            return {"logs": [{"address": MARKET, "data": data, "topics": [
                A.TOPIC_SOLD, _w(PINNED), _w(3), _w(TREASURY)]}]}
        if method == "eth_call":
            sel = params[0]["data"][:10]
            if sel == abi.selector("ownerOf(uint256)"):
                return _w(self.owner)
            if sel == A.SALE_READY_OF_SELECTOR:
                if self.ready is None:
                    raise RuntimeError("rpc down")
                return _w(int(self.ready))
        raise AssertionError(f"{method} {params}")


@pytest.fixture
def chain(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("POLYROB_INSTANCE_ID", raising=False)
    pin(monkeypatch, polyrob_profile(PINNED, journal_prefix="POLYROB"))
    return Chain()


def _tick(chain, told):
    async def notify(_c, _u, text):
        told.append(text)
    return asyncio.run(nft_holdings.tick(None, treasury=TREASURY, rpc_for=lambda c: chain,
                                         notify=notify))


def test_ready_and_readiness_lost_are_reported_once_each(chain):
    told = []
    _tick(chain, told)                       # the arrival, not ready
    assert len(told) == 1 and "You gave me" in told[0]
    chain.ready = True
    res = _tick(chain, told)
    assert [r["token_id"] for r in res.ready] == [3] and "READY FOR SALE" in told[-1]
    assert len(_tick(chain, told).ready) == 0 and len(told) == 2       # once
    chain.ready = None                                                  # unreadable: no change
    res = _tick(chain, told)
    assert res.ready == res.unready == [] and len(told) == 2
    chain.ready = False
    res = _tick(chain, told)
    assert [r["token_id"] for r in res.unready] == [3]
    assert "readiness lost, no transfer" in told[-1] and len(told) == 3


def test_an_nft_that_leaves_while_ready_is_sold_or_moved_with_no_guessed_price(chain):
    told = []
    chain.ready = True
    _tick(chain, told)
    chain.owner = BUYER
    res = _tick(chain, told)
    assert [r["token_id"] for r in res.lost] == [3]
    text = told[-1]
    assert "SOLD OR MOVED while it was ready for sale" in text and TX in text
    assert "do not know the price" in text and "is no longer mine" not in text


def test_a_sale_through_the_pinned_market_names_its_price(chain):
    told = []
    chain.ready = True
    _tick(chain, told)
    chain.owner, chain.sold = BUYER, 5 * 10 ** 16
    _tick(chain, told)
    assert f"names the price {5 * 10 ** 16} wei (native)" in told[-1], told[-1]


def test_the_overview_says_whether_it_is_ready(chain):
    chain.ready = True
    text = nft_holdings.overview(treasury=TREASURY, rpc_for=lambda c: chain)
    assert "sale:    READY" in text


def test_a_tokenbound_collection_reads_no_readiness(monkeypatch, tmp_path):
    from tests.collection_pins import profile
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("POLYROB_INSTANCE_ID", raising=False)
    pin(monkeypatch, profile(PINNED, journal_prefix="POLYROB"))
    chain = Chain()
    chain.ready = None   # a read would raise; none happens
    res = nft_holdings.scan(TREASURY, state={"tracked": {}}, rpc_for=lambda c: chain)
    assert [r["token_id"] for r in res.arrived] == [3] and "sale_ready" not in res.arrived[0]
