"""123 S3 / 124 section 7 — the core mint body: any collection whose SALE() names it back, every
fact read from the sale now; no package, no pin, no price constant in core."""
import os

import pytest

from core.wallet import collection_mint, collection_registry, collection_sale as S
from core.wallet.signer import LocalEoaSigner
from tests.collection_pins import pin
from tests.unit.tools.test_agent_nft_withdraw import FakeRail, _run, _tool
from tools.agent_nft.tool import MintParams

COLLECTION = "0x4444444444444444444444444444444444444444"
SALE = "0x5151515151515151515151515151515151515151"
BASE, STEP, START = 10 ** 16, 10 ** 15, 3 * 10 ** 16


def _w(v):
    return "0x" + format(int(v, 16) if isinstance(v, str) else int(v), "064x")


class SaleChain:
    def __init__(self, *, phase=S.PHASE_AUCTION, minted=7, price=2 * 10 ** 16, back=COLLECTION,
                 open_=True, capacity=100, wl_minted=0):
        self.phase, self.minted, self.price, self.back = phase, minted, price, back
        self.open, self.capacity, self.wl_minted = open_, capacity, wl_minted

    def __call__(self, method, params, *a, **k):
        assert method == "eth_call", method
        to, sel = params[0]["to"].lower(), params[0]["data"][:10]
        table = {
            (COLLECTION.lower(), S.SALE_OF_SELECTOR): _w(SALE),
            (SALE.lower(), S.COLLECTION_SELECTOR): _w(self.back),
            (SALE.lower(), S.PHASE_SELECTOR): _w(self.phase),
            (COLLECTION.lower(), S.MINT_OPEN_SELECTOR): _w(int(self.open)),
            (COLLECTION.lower(), S.TOTAL_MINTED_SELECTOR): _w(self.minted),
            (SALE.lower(), S.BASE_SELECTOR): _w(BASE), (SALE.lower(), S.STEP_SELECTOR): _w(STEP),
            (SALE.lower(), S.START_PRICE_SELECTOR): _w(START),
            (SALE.lower(), S.MAX_PER_TX_SELECTOR): _w(10),
            (SALE.lower(), S.PRICE_NOW_SELECTOR): _w(self.price),
            (SALE.lower(), S.PUBLIC_CAPACITY_SELECTOR): _w(self.capacity),
            (SALE.lower(), S.WL_MINTED_SELECTOR): _w(self.wl_minted),
        }
        if (to, sel) not in table:
            raise RuntimeError(f"execution reverted {to} {sel}")
        return table[(to, sel)]


@pytest.fixture
def armed(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NFT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    pin(monkeypatch)                              # nothing pinned
    collection_registry._CHAIN_PROFILES.clear()
    FakeRail.sent = []
    return LocalEoaSigner(os.urandom(32))


def _mint(armed, chain, **kw):
    tool, seen = _tool(armed, chain)
    params = dict(chain="robinhood", collection=COLLECTION, qty=2, max_spend_usd=500.0)
    params.update(kw)
    return _run(tool.agent_nft_collection_mint(MintParams(**params))), seen


def test_an_unpinned_auction_mint_pays_the_price_now_plus_one_step(armed):
    res, seen = _mint(armed, SaleChain())
    assert res.error is None, res.error
    (intent, tx, _), = seen
    assert intent.is_collection_mint and intent.mint_ids == (8, 9) and intent.via_account is None
    assert tx["to"].lower() == SALE.lower() and tx["value"] == 2 * (2 * 10 ** 16 + STEP)
    assert tx["data"] == collection_mint.encode_mint(armed.address, 2, 0)
    assert "phase auction" in res.extracted_content


def test_the_settled_phase_pays_exactly_the_base(armed):
    res, seen = _mint(armed, SaleChain(phase=S.PHASE_SETTLED), min_out_per_token=5)
    (intent, tx, _), = seen
    assert tx["value"] == 2 * BASE and tx["data"] == collection_mint.encode_mint(armed.address, 2, 5)


def test_the_whitelist_needs_allowance_and_proof(armed):
    res, seen = _mint(armed, SaleChain(phase=S.PHASE_WHITELIST))
    assert seen == [] and "whitelist phase" in res.error
    proof = ["0x" + "ab" * 32]
    res, seen = _mint(armed, SaleChain(phase=S.PHASE_WHITELIST), allowance=2, proof=proof)
    (intent, tx, _), = seen
    assert tx["data"] == collection_mint.encode_mint_wl(armed.address, 2, 0, 2, proof)
    res, seen = _mint(armed, SaleChain(phase=S.PHASE_WHITELIST, wl_minted=1), allowance=2, proof=proof)
    assert seen == [] and "allowance" in res.error


@pytest.mark.parametrize("chain, why", [
    (SaleChain(back="0x" + "99" * 20), "names"),
    (SaleChain(open_=False), "not open"),
    (SaleChain(phase=S.PHASE_CLOSED), "not open"),
    (SaleChain(capacity=1), "sold out"),
])
def test_what_the_sale_says_no_to_refuses_before_the_guard(armed, chain, why):
    res, seen = _mint(armed, chain)
    assert seen == [] and why in res.error and "Nothing was broadcast" in res.error


def test_the_sales_own_per_call_cap_applies(armed):
    res, seen = _mint(armed, SaleChain(), qty=11)
    assert seen == [] and "1..10 per call" in res.error


def test_a_mint_to_another_address_refuses(armed):
    res, seen = _mint(armed, SaleChain(), to="0x" + "12" * 20)
    assert seen == [] and "treasury" in res.error
