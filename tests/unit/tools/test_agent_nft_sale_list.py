"""111 W9a — the seller's order verbs ``agent_nft_sale_list`` / ``agent_nft_sale_accept`` and the
open-order list (``core/wallet/market_orders.py``).

The chain proof is the rig (``tests/integration/test_agent_nft_rig.py``); here: the owner gate
signs nothing on an automatic turn, a link of the wrong kind refuses by name, the cancel finds the
ids whose orders it ends, and every order's state is read live (unread is never "ended").
"""
import types

import pytest

from core.wallet import market_orders as M, sale_acts as A, sale_conditions as S
from tests.unit.tools.test_agent_nft_polyrob_view import PINNED, armed  # noqa: F401
from tests.unit.tools.test_agent_nft_withdraw import _run, _tool
from tools.agent_nft import sale_list
from tools.agent_nft.tool import SaleAcceptParams, SaleListParams

MARKET = "0x" + "b1" * 20
COLL = "0x" + "66" * 20
CHAIN_ID = 4663


def _w(n):
    return "0x" + format(int(n), "064x")


def test_the_params_bound_the_listing():
    with pytest.raises(Exception):
        SaleListParams(chain="robinhood", price="0.1", expiry_days=31)
    with pytest.raises(Exception):
        SaleListParams(chain="robinhood", price="-1")
    with pytest.raises(Exception):
        SaleListParams(chain="robinhood", price="0.1", tolerance_bps=10)
    assert SaleListParams(chain="robinhood", price="0.05").dry_run is True


def test_a_listing_is_never_signed_on_an_automatic_turn(armed, monkeypatch):
    """The owner gate runs before the key is used: no owner turn and no granted card → nothing
    is signed and nothing is sent."""
    signed = []
    held = types.SimpleNamespace(chain="robinhood", collection=COLL, token_id=1, label="X #1",
                                 account="0x" + "ac" * 20)
    profile = types.SimpleNamespace(erc6551p=types.SimpleNamespace(market=MARKET), address=COLL,
                                    chain_id=46630)
    monkeypatch.setattr("tools.agent_nft.withdraw._held", lambda t, p, c: (held, armed))
    monkeypatch.setattr(sale_list, "quote_listing",
                        lambda *a: (profile, {}, None, [], "LIST …\n", MARKET))
    monkeypatch.setattr("core.wallet.sign_policy.sign_market_order",
                        lambda *a, **k: signed.append(a))
    tool, seen = _tool(armed, lambda m, p: None)
    res = _run(tool.agent_nft_sale_list(SaleListParams(chain="robinhood", price="0.05",
                                                       dry_run=False)))
    assert res.error and "owner's act" in res.error and "Nothing was signed" in res.error
    assert signed == [] and seen == []


def test_a_listing_dry_run_signs_nothing(armed, monkeypatch):
    held = types.SimpleNamespace(chain="robinhood", collection=COLL, token_id=1, label="X #1",
                                 account="0x" + "ac" * 20)
    profile = types.SimpleNamespace(erc6551p=types.SimpleNamespace(market=MARKET), address=COLL,
                                    chain_id=46630)
    monkeypatch.setattr("tools.agent_nft.withdraw._held", lambda t, p, c: (held, armed))
    monkeypatch.setattr(sale_list, "quote_listing",
                        lambda *a: (profile, {}, None, [], "LIST …\n", S.ZERO))
    monkeypatch.setattr(sale_list, "_owner_asked", lambda *a: pytest.fail("not asked on a dry run"))
    tool, seen = _tool(armed, lambda m, p: None)
    res = _run(tool.agent_nft_sale_list(SaleListParams(chain="robinhood", price="0.05")))
    assert res.error is None and "DRY RUN" in res.extracted_content and seen == []


def _link(k, **extra):
    return "https://x.example/a#x=" + S.encode_link(dict({"v": 1, "k": k, "chainId": CHAIN_ID}, **extra))


@pytest.mark.parametrize("k, needle", [("m", "a listing, not an offer"),
                                       ("s", "a listing, not an offer")])
def test_accept_refuses_a_listing_link_by_name(armed, k, needle):
    tool, seen = _tool(armed, lambda m, p: None)
    res = _run(tool.agent_nft_sale_accept(SaleAcceptParams(chain="robinhood", offer=_link(k))))
    assert res.error and needle in res.error and seen == []


def test_an_any_id_offer_needs_the_nft_named(armed, monkeypatch):
    from tests.unit.core.wallet.test_sale_orders import _offer
    o = _offer(anyId=True, id=0, collection=PINNED)
    link = _link("o", market=MARKET, offer={k: (str(v) if isinstance(v, int) and not isinstance(
        v, bool) else v) for k, v in o.__dict__.items()}, sig="0x" + "ab" * 65, conditions=[])
    monkeypatch.setattr("tools.agent_nft.sale_ready.pinned_market",
                        lambda c, r=None: types.SimpleNamespace(
                            erc6551p=types.SimpleNamespace(market=MARKET), address=PINNED))
    tool, _seen = _tool(armed, lambda m, p: None)
    res = _run(tool.agent_nft_sale_accept(SaleAcceptParams(chain="robinhood", offer=link)))
    assert res.error and "name the NFT to sell" in res.error


# ------------------------------------------------------------------------------ the open orders

class OrderChain:
    def __init__(self, *, tc=1, used=False, nonce=0, now=1000, broken=False):
        self.tc, self.used, self.nonce, self.now, self.broken = tc, used, nonce, now, broken

    def __call__(self, method, params):
        if self.broken:
            raise ConnectionError("down")
        if method == "eth_getBlockByNumber":
            return {"timestamp": hex(self.now)}
        sel = params[0]["data"][:10]
        return _w({S.TRANSFER_COUNT_SELECTOR: self.tc, A.USED_SALT_SELECTOR: int(self.used),
                   A.NONCES_SELECTOR: self.nonce}[sel])


def _row(**kw):
    base = {"chain": "robinhood", "chain_id": 46630, "collection": COLL, "token_id": 1,
            "market": MARKET, "seller": "0x" + "22" * 20, "salt": "7", "nonce": 0,
            "transfer_count": 1, "price": "5", "currency": S.ZERO, "expiry": 2000}
    base.update(kw)
    return base


@pytest.mark.parametrize("chain, state", [
    (OrderChain(), M.OPEN), (OrderChain(tc=2), M.MOVED), (OrderChain(used=True), M.CANCELLED),
    (OrderChain(nonce=1), M.CANCELLED), (OrderChain(now=3000), M.EXPIRED),
    (OrderChain(broken=True), M.UNKNOWN),
])
def test_an_orders_state_is_read_live(chain, state):
    assert M.status(chain, _row())[0] == state


def test_prune_drops_only_ended_orders_and_keeps_the_unread(tmp_path):
    at = tmp_path / "orders.json"
    M.record(_row(salt="1"), at=at)
    M.record(_row(salt="2", token_id=2), at=at)
    chains = {"robinhood": OrderChain(now=3000)}
    ended = M.prune(lambda c: chains[c], at=at)
    assert sorted(r["salt"] for r, _s, _w in ended) == ["1", "2"]
    assert M.open_orders(at=at) == []
    M.record(_row(salt="3"), at=at)
    assert M.prune(lambda c: OrderChain(broken=True), at=at) == []
    assert [r["salt"] for r in M.open_orders(at=at)] == ["3"]


def test_an_unreadable_orders_file_is_never_none(tmp_path):
    at = tmp_path / "orders.json"
    at.write_text("{not json")
    with pytest.raises(Exception):
        M.open_orders(at=at)
    with pytest.raises(Exception):
        M.prune(lambda c: OrderChain(), at=at)


def test_the_cancel_revokes_the_approval_of_the_orders_it_ends(monkeypatch, tmp_path):
    from tools.agent_nft.sale_ready import _ended_ids
    at = tmp_path / "orders.json"
    monkeypatch.setattr(M, "path", lambda *a, **k: at)
    M.record(_row(salt="7", token_id=3), at=at)
    M.record(_row(salt="8", token_id=4), at=at)
    profile = types.SimpleNamespace(erc6551p=types.SimpleNamespace(market=MARKET), address=COLL)
    assert _ended_ids(profile, 7, None) == [3]
    assert sorted(_ended_ids(profile, None, None)) == [3, 4]
    assert _ended_ids(profile, None, f"{COLL}#9") == [9]


def test_a_remote_signer_approval_is_named_and_the_retry_signs_the_same_listing(
        armed, monkeypatch, tmp_path):
    """``sign.order`` answers ``approval_required``: the result names the approval and the box
    command, the listing is held; a NEW quote on retry (another salt) is not what is signed —
    the held listing is, and it is released once signed."""
    from core.signer import protocol
    from core.signer.client import SignerRefused
    from core.wallet import sale_orders as O
    from tests.unit.core.wallet.test_sale_orders import _listing, _typed
    at = tmp_path / "orders.json"
    monkeypatch.setattr(M, "path", lambda *a, **k: at)
    held = types.SimpleNamespace(chain="robinhood", collection=COLL, token_id=1, label="X #1",
                                 account="0x" + "ac" * 20)
    profile = types.SimpleNamespace(erc6551p=types.SimpleNamespace(market=MARKET), address=COLL,
                                    chain_id=CHAIN_ID)
    salts = iter([111, 222])

    def quote(*a):
        l = _listing(salt=next(salts), collection=COLL)
        return profile, _typed(l), l, [], "LIST …\n", MARKET
    signed = []

    def sign(signer, req, *, facts, chain):
        signed.append(req.payload["message"]["salt"])
        if len(signed) == 1:
            raise SignerRefused(protocol.APPROVAL_REQUIRED, "requires independent signer approval",
                                {"approval_id": "sig-abc"})
        return "0x" + "ab" * 65, types.SimpleNamespace(digest="0x" + "cd" * 32)
    monkeypatch.setattr("tools.agent_nft.withdraw._held", lambda t, p, c: (held, armed))
    monkeypatch.setattr(sale_list, "quote_listing", quote)
    monkeypatch.setattr(sale_list, "_owner_asked", lambda *a: True)
    monkeypatch.setattr(O, "read_listing_facts", lambda *a, **k: None)
    monkeypatch.setattr("core.wallet.sign_policy.sign_market_order", sign)
    tool, _seen = _tool(armed, lambda m, p: None)
    p = SaleListParams(chain="robinhood", price="0.01", dry_run=False)
    first = _run(tool.agent_nft_sale_list(p))
    assert first.error and "sig-abc" in first.error
    assert "sudo polyrob owner promote signer_approval sig-abc" in first.error
    assert M.held(CHAIN_ID, COLL, 1, at=at)["approval_id"] == "sig-abc"
    second = _run(tool.agent_nft_sale_list(p))
    assert second.error is None and "LISTED" in second.extracted_content, second.error
    assert signed == [111, 111]            # the held listing, not the new quote's salt 222
    assert M.held(CHAIN_ID, COLL, 1, at=at) is None
    assert [r["salt"] for r in M.open_orders(at=at)] == ["111"]
