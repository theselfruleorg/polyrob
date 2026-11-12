"""115 W7 — the seller's verbs ``agent_nft_sale_ready`` / ``agent_nft_sale_unready`` /
``agent_nft_sale_cancel_listing``: each builds the exact call the contract requires and hands it
to the guard (stubbed here; the guard's own rules are tests/unit/core/wallet/test_sale_acts.py),
says what readiness means before consent, and sends nothing when there is nothing to do."""
import pytest

from core.wallet import abi, erc6551, sale_acts as A
from tests.collection_pins import CODE, MARKET, pin, profile
from tests.unit.tools.test_agent_nft_polyrob_view import ACCOUNT, PINNED, PolyrobChain, armed  # noqa: F401
from tests.unit.tools.test_agent_nft_withdraw import FakeRail, _run, _tool
from tools.agent_nft.tool import RevealParams, SaleCancelListingParams, SaleReadyParams


class SaleChain(PolyrobChain):
    def __init__(self, owner, *, ready=False, nonce=4, used=False):
        super().__init__(owner, ready=ready)
        self.nonce, self.used = nonce, used

    def __call__(self, method, params, *a, **k):
        if method == "eth_getCode" and params[0].lower() == MARKET:
            return CODE
        if method == "eth_call":
            sel = params[0]["data"][:10]
            if sel == A.NONCES_SELECTOR:
                return "0x" + format(self.nonce, "064x")
            if sel == A.USED_SALT_SELECTOR:
                return "0x" + format(int(self.used), "064x")
        return super().__call__(method, params, *a, **k)


def test_ready_is_ready_to_sell_by_the_nfts_own_account(armed):
    tool, seen = _tool(armed, SaleChain(armed.address))
    res = _run(tool.agent_nft_sale_ready(SaleReadyParams(chain="robinhood", nft="3", dry_run=False)))
    assert res.error is None, res.error
    (intent, tx, _kw), = seen
    assert intent.is_nft_op and intent.to == PINNED.lower() and intent.amount_raw == 0
    assert intent.via_account == ACCOUNT and not intent.nft_out and intent.token is None
    assert tx["to"] == ACCOUNT
    to, value, data, op = erc6551.decode_execute(tx["data"])
    assert (to.lower(), value, data, op) == (PINNED.lower(), 0, A.encode_ready(3), 0)
    text = res.extracted_content
    assert "PLAIN marketplace fill may move the NFT" in text and "agent_nft_sale_unready" in text
    assert FakeRail.sent


def test_ready_when_already_ready_sends_nothing(armed):
    tool, seen = _tool(armed, SaleChain(armed.address, ready=True))
    res = _run(tool.agent_nft_sale_ready(SaleReadyParams(chain="robinhood", nft="3")))
    assert "already ready" in res.extracted_content and not seen


def test_a_kind_without_readiness_cannot_be_made_ready(armed, monkeypatch):
    pin(monkeypatch, profile(PINNED, journal_prefix="POLYROB"))    # Tokenbound AccountV3
    tool, seen = _tool(armed, SaleChain(armed.address))
    res = _run(tool.agent_nft_sale_ready(SaleReadyParams(chain="robinhood", nft="3")))
    assert res.error and "no sale readiness" in res.error and not seen


def test_unready_is_cancel_ready_from_the_treasury(armed):
    tool, seen = _tool(armed, SaleChain(armed.address, ready=True))
    res = _run(tool.agent_nft_sale_unready(SaleReadyParams(chain="robinhood", nft="3",
                                                           dry_run=False)))
    assert res.error is None, res.error
    (intent, tx, _kw), = seen
    assert intent.is_nft_op and intent.via_account is None and intent.to == PINNED.lower()
    assert tx["to"].lower() == PINNED.lower() and tx["data"] == A.encode_cancel_ready(3)


def test_unready_when_not_ready_sends_nothing(armed):
    tool, seen = _tool(armed, SaleChain(armed.address, ready=False))
    res = _run(tool.agent_nft_sale_unready(SaleReadyParams(chain="robinhood", nft="3")))
    assert "not ready — no transaction" in res.extracted_content and not seen


def test_cancel_all_is_increment_nonce_on_the_pinned_market(armed):
    tool, seen = _tool(armed, SaleChain(armed.address, nonce=4))
    res = _run(tool.agent_nft_sale_cancel_listing(SaleCancelListingParams(chain="robinhood",
                                                                          dry_run=False)))
    assert res.error is None, res.error
    (intent, tx, _kw), = seen
    assert intent.is_nft_op and intent.to == MARKET and tx["to"] == MARKET
    assert tx["data"] == A.encode_increment_nonce()
    assert "EVERY listing and offer" in res.extracted_content and "nonce 4 → 5" in res.extracted_content


def test_cancel_one_salt(armed):
    tool, seen = _tool(armed, SaleChain(armed.address))
    res = _run(tool.agent_nft_sale_cancel_listing(SaleCancelListingParams(chain="robinhood", salt=7)))
    assert res.error is None, res.error
    assert seen[0][1]["data"] == A.encode_market_cancel(7)


def test_a_used_salt_sends_nothing(armed):
    tool, seen = _tool(armed, SaleChain(armed.address, used=True))
    res = _run(tool.agent_nft_sale_cancel_listing(SaleCancelListingParams(chain="robinhood", salt=7)))
    assert "already used or cancelled" in res.extracted_content and not seen


def test_no_pinned_market_refuses(armed, monkeypatch):
    pin(monkeypatch, profile(PINNED, journal_prefix="POLYROB"))
    tool, seen = _tool(armed, SaleChain(armed.address))
    res = _run(tool.agent_nft_sale_cancel_listing(SaleCancelListingParams(chain="robinhood")))
    assert res.error and "no collection with a pinned market" in res.error and not seen


def test_the_verbs_are_off_without_the_flag(armed, monkeypatch):
    monkeypatch.delenv("AGENT_NFT_ENABLED")
    tool, seen = _tool(armed, SaleChain(armed.address))
    for fn, p in ((tool.agent_nft_sale_ready, SaleReadyParams(chain="robinhood")),
                  (tool.agent_nft_sale_unready, SaleReadyParams(chain="robinhood")),
                  (tool.agent_nft_sale_cancel_listing, SaleCancelListingParams(chain="robinhood"))):
        assert "AGENT_NFT_ENABLED" in _run(fn(p)).error
    assert not seen


def test_reveal_max_ids_is_unset_by_default():
    p = RevealParams(chain="robinhood")
    assert p.max_ids is None and "max_ids" not in p.model_fields_set
    assert "sized to the fee cap" in RevealParams.model_fields["max_ids"].description


def test_the_readiness_selector_matches_the_view(armed):
    assert A.SALE_READY_OF_SELECTOR == abi.selector("saleReadyOf(uint256)")
