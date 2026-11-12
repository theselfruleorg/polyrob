"""115 W9 — the buyer's verbs ``agent_nft_buy`` / ``agent_nft_market_claim``
(``tools/agent_nft/sale_buy.py``): the verb reads the listing the seller's site shares, checks
its live state, builds the buyer conditions from a fresh read of the account, simulates (dropping
only holdings whose value is not checked), and hands ONE exact call to the guard (stubbed here;
its rules are ``tests/unit/core/wallet/test_tx_guard_sale.py``). A Seaport listing, an offer or
an ERC-20 price is refused by name; a dead listing sends nothing."""
import pytest

import core.wallet.abi as abi
from core.wallet import sale_acts as A, sale_conditions as S
from tests.collection_pins import CODE, MARKET
from tests.unit.tools.test_agent_nft_polyrob_view import IMPL, PINNED, armed  # noqa: F401
from tests.unit.tools.test_agent_nft_withdraw import FakeRail, _run, _tool
from tools.agent_nft.tool import BuyParams, MarketClaimParams

COLL = PINNED.lower()
SELLER = "0x" + "5e" * 20
JUNK = "0x" + "77" * 20
TOKEN_ID = 3
PRICE = 10 ** 16
CHAIN_ID = 4663


def _w(n):
    return "0x" + format(int(n), "064x")


def _listing(**kw):
    base = dict(collection=COLL, id=TOKEN_ID, transferCount=2, seller=SELLER, currency=S.ZERO,
                price=PRICE, maxRoyalty=0, expiry=10 ** 9, salt=11,
                promiseHash=S.conditions_hash([]), nonce=0)
    base.update(kw)
    return S.Listing.of(base)


def _link(listing=None, **kw):
    payload = {"v": 1, "k": "m", "chainId": CHAIN_ID, "market": MARKET, "collection": COLL,
               "listing": dict(zip(listing.__dataclass_fields__, listing.as_tuple()))
               if listing else None, "sig": "0x" + "ab" * 65, "promise": []}
    payload.update(kw)
    if payload["listing"] is None:
        l = _listing()
        payload["listing"] = dict(zip(l.__dataclass_fields__, l.as_tuple()))
    return "https://example.invalid/buy.html?id=3#x=" + S.encode_link(payload)


class BuyChain:
    """A pinned collection (#3 owned by the seller, transferCount 2) and its market; the token's
    account holds ETH and a junk ERC-20 (off the reviewed list). ``fail_junk``: the junk
    condition fails the simulated buy (a token that answers the market otherwise)."""

    def __init__(self, *, owner=SELLER, transfer_count=2, nonce=0, used=False, now=1000,
                 fail=None, owed=0, digest_ok=True):
        self.owner, self.transfer_count, self.nonce, self.used = owner, transfer_count, nonce, used
        self.now, self.fail, self.owed, self.digest_ok = now, fail, owed, digest_ok
        self.buys = []

    def __call__(self, method, params, *a, **k):
        if method == "eth_getCode":
            return CODE if params[0].lower() in (COLL, MARKET) else "0x"
        if method == "eth_blockNumber":
            return hex(0x300)
        if method == "eth_getBlockByNumber":
            return {"timestamp": hex(self.now)}
        if method == "eth_getBalance":
            return hex(5 * 10 ** 15)
        if method == "eth_getLogs":
            if params[0]["topics"][0] == S.TOPIC_TRANSFER:
                return [{"address": JUNK, "topics": [S.TOPIC_TRANSFER, _w(1), params[0]["topics"][2]],
                         "data": _w(9)}]
            return []
        if method == "eth_call":
            to, data = params[0]["to"].lower(), params[0]["data"]
            sel = data[:10]
            if params[0].get("from"):          # the simulated buy
                self.buys.append(data)
                call = S.decode_buy(data)
                if self.fail is not None and len(call.conditions) > 1:
                    raise RuntimeError(f"eth_call: {{'code': 3, 'message': 'execution reverted', "
                                       f"'data': '{self.fail}'}}")
                return "0x"
            if to == COLL:
                return {S.OWNER_OF_SELECTOR: _w(int(self.owner, 16)),
                        S.TRANSFER_COUNT_SELECTOR: _w(self.transfer_count),
                        S.HAS_IDENTITY_SELECTOR: _w(0), S.POSITION_OF_SELECTOR: _w(0)}[sel]
            if to == MARKET:
                if sel == A.NONCES_SELECTOR:
                    return _w(self.nonce)
                if sel == A.USED_SALT_SELECTOR:
                    return _w(int(self.used))
                if sel == S.OWED_SELECTOR:
                    return _w(self.owed)
                if sel == S.EIP712_DOMAIN_SELECTOR:
                    return "0x" + abi.encode(
                        [{"type": "bytes1"}, {"type": "string"}, {"type": "string"},
                         {"type": "uint256"}, {"type": "address"}, {"type": "bytes32"},
                         {"type": "uint256[]"}],
                        ["0x0f", "TestMarket", "1", CHAIN_ID, MARKET, "0x" + "00" * 32, []]).hex()
                if sel == S.LISTING_DIGEST_SELECTOR:
                    (raw,) = abi.decode([S._LISTING], "0x" + data[10:])
                    d = S.listing_digest(S.Listing.of(raw), name="TestMarket", version="1",
                                         chain_id=CHAIN_ID, market=MARKET)
                    return d if self.digest_ok else _w(1)
            if to == JUNK and sel == S.BALANCE_OF_SELECTOR:
                return _w(9)
            if sel == S.BALANCE_OF_SELECTOR:
                return _w(0)
            if sel == S.DECIMALS_SELECTOR:
                return _w(18)
            if sel == S.SYMBOL_SELECTOR:
                return "0x"
        raise AssertionError((method, params))


def _hash_of(text):
    import re
    return re.search(r"conditions_hash (0x[0-9a-f]{64})", text).group(1)


def _buy(armed, chain, link=None, **kw):
    """The quote (dry run), then the buy of exactly that quote (its conditions_hash)."""
    tool, seen = _tool(armed, chain)
    link = link or _link()
    dry = _run(tool.agent_nft_buy(BuyParams(chain="robinhood", listing=link,
                                            max_spend_usd=100.0, **kw)))
    if dry.error:
        return dry, seen
    params = BuyParams(chain="robinhood", listing=link, max_spend_usd=100.0, dry_run=False,
                       conditions_hash=_hash_of(dry.extracted_content), **kw)
    return _run(tool.agent_nft_buy(params)), seen


def test_buy_hands_the_guard_one_exact_market_buy(armed):
    FakeRail.sent = []
    res, seen = _buy(armed, BuyChain())
    assert res.error is None, res.error
    assert len(seen) == 2          # the quote's dry run and the buy, the same call
    intent, tx, _kw = seen[-1]
    assert seen[0][1]["data"] == tx["data"]
    assert intent.token is None and intent.amount_raw == PRICE and intent.to == MARKET
    assert intent.expected_nft_in == ((COLL, "erc721", TOKEN_ID, 1),)
    assert not intent.is_nft_op and not intent.via_account
    assert tx["to"] == MARKET and tx["value"] == PRICE
    call = S.decode_buy(tx["data"])
    assert call.recipient == armed.address.lower() and call.listing == _listing()
    kinds = [(c.kind, c.target) for c in call.conditions]
    assert kinds == [(S.ETH, S.ZERO), (S.ERC20, JUNK)]
    text = res.extracted_content
    assert "BUY" in text and "CHECKED" in text and "value not checked" in text
    assert FakeRail.sent


def test_a_failing_unreviewed_holding_is_dropped_and_labeled(armed):
    fail = "0x72fc940b" + format(1, "064x")      # buyer condition 1 = the junk token
    chain = BuyChain(fail=fail)
    res, seen = _buy(armed, chain)
    assert res.error is None, res.error
    _intent, tx, _kw = seen[-1]
    assert [c.kind for c in S.decode_buy(tx["data"]).conditions] == [S.ETH]
    assert len(chain.buys) == 4 and "NOT CHECKED (worth 0)" in res.extracted_content
    assert "cannot be checked" in res.extracted_content


def test_a_failing_reviewed_condition_is_named_and_nothing_is_sent(armed):
    chain = BuyChain(fail="0x72fc940b" + format(0, "064x"))   # the ETH condition
    res, seen = _buy(armed, chain)
    assert res.error and "ConditionFailed" in res.error and "Nothing was broadcast" in res.error
    assert not seen


@pytest.mark.parametrize("chain, needle", [
    (BuyChain(owner=JUNK), "does not own"),
    (BuyChain(transfer_count=3), "moved since the listing was signed"),
    (BuyChain(now=10 ** 9 + 1), "expired"),
    (BuyChain(used=True), "its salt is used"),
    (BuyChain(nonce=1), "nonce moved"),
    (BuyChain(digest_ok=False), "disagree on the listing"),
])
def test_a_dead_listing_sends_nothing(armed, chain, needle):
    res, seen = _buy(armed, chain)
    assert res.error and needle in res.error and not seen, res.error


@pytest.mark.parametrize("link, needle", [
    (_link(k="s"), "Seaport listing"),
    (_link(k="o"), "an offer"),
    (_link(listing=_listing(currency=JUNK)), "ETH listings only"),
    (_link(market="0x" + "99" * 20), "another market"),
    (_link(chainId=1), "for chain 1"),
    (_link(listing=_listing(promiseHash="0x" + "22" * 32)), "promise in the link"),
    (_link(listing=_listing(collection="0x" + "99" * 20)), "not a pinned collection"),
    ("not a listing at all", "unreadable"),
])
def test_what_is_not_a_market_listing_of_a_pinned_collection_is_refused(armed, link, needle):
    res, seen = _buy(armed, BuyChain(), link=link)
    assert res.error and needle in res.error and not seen, res.error


def test_a_real_buy_names_its_quote_and_refuses_a_changed_account(armed):
    tool, seen = _tool(armed, BuyChain())
    res = _run(tool.agent_nft_buy(BuyParams(chain="robinhood", listing=_link(),
                                            max_spend_usd=100.0, dry_run=False)))
    assert res.error and "names the conditions_hash" in res.error and not seen
    res = _run(tool.agent_nft_buy(BuyParams(chain="robinhood", listing=_link(),
                                            max_spend_usd=100.0, dry_run=False,
                                            conditions_hash="0x" + "12" * 32)))
    assert res.error and "changed since the quote" in res.error and not seen


def test_a_tolerance_other_than_0_or_50_is_refused_by_the_schema():
    with pytest.raises(Exception):
        BuyParams(chain="robinhood", listing="x" * 10, max_spend_usd=1.0, tolerance_bps=10)
    assert BuyParams(chain="robinhood", listing="x" * 10, max_spend_usd=1.0,
                     tolerance_bps=50).tolerance_bps == 50


def test_market_claim(armed):
    tool, seen = _tool(armed, BuyChain(owed=7))
    res = _run(tool.agent_nft_market_claim(MarketClaimParams(chain="robinhood", dry_run=False)))
    assert res.error is None, res.error
    (intent, tx, _kw), = seen
    assert intent.is_claim and intent.min_native_inflow_wei == 7 and intent.amount_raw == 0
    assert tx["to"] == MARKET and tx["data"] == S.encode_claim(armed.address) and tx["value"] == 0


def test_nothing_owed_no_transaction(armed):
    tool, seen = _tool(armed, BuyChain(owed=0))
    res = _run(tool.agent_nft_market_claim(MarketClaimParams(chain="robinhood")))
    assert "owes this treasury nothing" in res.extracted_content and not seen


def test_the_buy_card_shows_the_holdings_or_says_it_could_not(armed):
    from tools.agent_nft.sale_buy import buy_card_context
    tool, _seen = _tool(armed, BuyChain())
    lines = buy_card_context({"chain": "robinhood", "listing": _link()}, tool=tool)
    assert lines[0].startswith("• What you buy") and any("CHECKED" in ln for ln in lines)
    bad = buy_card_context({"chain": "robinhood", "listing": "nope nope"}, tool=tool)
    assert bad[0].startswith("⚠️ THE QUOTE COULD NOT BE READ")
