"""115 W1 — holding is the whole relation: what an arrived NFT's account already holds is
recorded by the holdings watch itself, with no "adopt" step.

Each tracked token the account holds becomes an ``inherited`` position (no cost basis) once; an
unreadable balance is named and tried again on the next pass; the amount is in token units
(raw / 10**decimals), never raw units.
"""
import asyncio

import pytest

from core import open_positions
from core.wallet import abi, nft_holdings, nft_inherited
from tests.unit.core.wallet.test_nft_holdings import PINNED, TREASURY, ChainRpc, chain  # noqa: F401

TOKEN = "0x" + "a0" * 20
OTHER = "0x" + "b0" * 20
SEL_BALANCE = abi.selector("balanceOf(address)")


class TokenRpc(ChainRpc):
    def __init__(self):
        super().__init__()
        self.balances = {TOKEN: 2_500_000, OTHER: 0}
        self.broken = set()

    def __call__(self, method, params):
        if method == "eth_call" and params[0]["data"].startswith(SEL_BALANCE):
            to = params[0]["to"].lower()
            if to in self.broken:
                raise RuntimeError("rpc down")
            return "0x" + f"{self.balances[to]:064x}"
        return super().__call__(method, params)


@pytest.fixture
def rpc(chain, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setattr(nft_inherited, "tracked_tokens", lambda c, r: [
        {"address": TOKEN, "symbol": "AAA", "decimals": 6},
        {"address": OTHER, "symbol": "BBB", "decimals": 18}])
    monkeypatch.setattr(open_positions, "open_positions_db_path",
                        lambda *a, **k: str(tmp_path / "open_positions.db"))
    r = TokenRpc()
    r.transfers = [(0x50, 3, TREASURY)]
    r.owners = {3: TREASURY}
    return r


def _tick(rpc_, told):
    async def notify(container, user_id, text):
        told.append(text)
    return asyncio.run(nft_holdings.tick(None, treasury=TREASURY, rpc_for=lambda c: rpc_,
                                         notify=notify, user_id="owner"))


def _rows(tmp_path):
    import os
    from core.wallet import erc6551
    path = str(tmp_path / "open_positions.db")
    if not os.path.exists(path):
        return []
    account = erc6551.account_address(4663, PINNED, 3, implementation=erc6551.ACCOUNT_V3_IMPL)
    return open_positions.positions_for("owner", account=account, db_path=path)


def test_an_arrival_records_what_the_account_holds_in_token_units(rpc, tmp_path):
    told = []
    _tick(rpc, told)
    assert len(told) == 1 and "It already holds AAA: recorded as inherited" in told[0]
    rows = _rows(tmp_path)
    assert [(r.address, r.qty, r.origin) for r in rows] == [(TOKEN, 2.5, "inherited")]
    assert rows[0].account and rows[0].entry_usd is None


def test_it_is_recorded_once(rpc, tmp_path):
    told = []
    _tick(rpc, told)
    rpc.balances[TOKEN] = 9_000_000
    _tick(rpc, told)
    assert len(told) == 1
    assert [r.qty for r in _rows(tmp_path)] == [2.5]


def test_an_unreadable_balance_is_named_and_tried_again(rpc, tmp_path):
    rpc.broken = {TOKEN}
    told = []
    res = _tick(rpc, told)
    assert "could not read all of its token balances yet (AAA" in told[0]
    assert res.errors == [], "an unread balance is not a failed ownership pass"
    assert _rows(tmp_path) == []
    rpc.broken = set()
    _tick(rpc, told)
    assert len(told) == 1, "the arrival is told once"
    assert [r.qty for r in _rows(tmp_path)] == [2.5]


def test_unknown_decimals_are_never_guessed(rpc, monkeypatch, tmp_path):
    monkeypatch.setattr(nft_inherited, "tracked_tokens", lambda c, r: [
        {"address": TOKEN, "symbol": "AAA", "decimals": None}])
    told = []
    _tick(rpc, told)
    assert "AAA (decimals unknown)" in told[0] and _rows(tmp_path) == []


def test_an_unreadable_token_list_is_not_an_empty_one(rpc, monkeypatch, tmp_path):
    def boom(c, r):
        raise RuntimeError("pins unreadable")
    monkeypatch.setattr(nft_inherited, "tracked_tokens", boom)
    told = []
    _tick(rpc, told)
    assert "the tracked token list" in told[0] and "It already holds" not in told[0]
