"""The liquidity positions block of the holdings report.

Prod 2026-11-13: "all balances" left out a v4 position that held about half of
the wallet's native coin — ``portfolio`` read token balances only.
"""
from types import SimpleNamespace

import pytest

from tools.defi import lp_holdings as H, lp_reads as R, lp_v4 as V
from tools.defi.data_tool import DefiDataTool

HOLDER = "0x1111111111111111111111111111111111111111"
TKN = "0x5555555555555555555555555555555555555555"
ZERO = "0x" + "0" * 40
KEY = {"currency0": ZERO, "currency1": TKN, "fee": 10000, "tickSpacing": 200, "hooks": ZERO}
Q96 = 2 ** 96


class _Tool:
    _nft_fetch = None

    def __init__(self, prices):
        self.prices = prices

    def _lp_symbol(self, chain, token):
        return "TKN"

    def _price_for(self, chain, address):
        price, grade = self.prices.get(address.lower(), (None, None))
        return SimpleNamespace(price_usd=price, confidence=grade)


@pytest.fixture
def chain(monkeypatch):
    """One v4 position (#7) of HOLDER; the v3 manager holds none."""
    state = {"v3": 0, "v4": 1, "ids": [7], "owner": HOLDER}

    def view(rpc, to, spec, args=()):
        from core.wallet import dex_registry
        if spec["name"] == "balanceOf":
            proto = "v4" if to == dex_registry.row_for("robinhood", "v4").position_manager else "v3"
            if isinstance(state[proto], Exception):
                raise state[proto]
            return state[proto]
        raise AssertionError(spec["name"])

    monkeypatch.setattr(R, "view", view)
    monkeypatch.setattr(H, "v4_ids_from_logs", lambda *a, **k: list(state["ids"]))
    monkeypatch.setattr(V, "position", lambda rpc, chain, tid: V.V4Position(
        tid, state["owner"].lower(), KEY, "0x" + "ab" * 32, -200, 200, 10 ** 21))
    monkeypatch.setattr(V, "pool_state", lambda rpc, chain, pid: V.V4PoolState(
        pid, Q96, 0, 0, 10000, 10 ** 21, "state_view"))
    monkeypatch.setattr(V, "_decimals_pair", lambda key, fn: (18, 18))
    return state


def _weth():
    from core.wallet import chains
    return chains.get("robinhood").wrapped_native.lower()


def test_a_v4_position_is_listed_with_both_legs_and_the_total_rule(chain):
    tool = _Tool({_weth(): (2000.0, "high"), TKN: (0.001, "low")})
    block = H.build(tool, None, "robinhood", HOLDER)
    text = "\n".join(block["lines"])
    assert "v4 #7" in text and "in range" in text and "NOT in the balances above" in text
    assert "no reliable price, indicative" in text
    leg0, leg1 = block["positions"][0]["legs"]
    assert leg0["counted"] and not leg1["counted"]
    assert block["counted_usd"] == pytest.approx(leg0["amount"] * 2000.0)
    assert block["indicative_usd"] == pytest.approx(leg1["amount"] * 0.001)
    assert block["complete"]
    lines = H.total_lines(block, 100.0)
    assert "in liquidity positions" in lines[0] and "NOT counted" in lines[0]
    assert "wallet + liquidity positions" in lines[1]


def test_no_position_is_said_as_a_read_answer(chain):
    chain["v4"] = 0
    block = H.build(_Tool({}), None, "robinhood", HOLDER)
    assert block["lines"] == [
        "liquidity positions: none (the pinned position managers were read on chain)"]
    assert H.total_lines(block, 5.0) == []


def test_a_held_position_that_is_not_found_is_named_never_dropped(chain):
    chain["v4"] = 2          # the chain says two; the scan finds one
    block = H.build(_Tool({_weth(): (2000.0, "high")}), None, "robinhood", HOLDER)
    text = "\n".join(block["lines"])
    assert "the chain says 2 position(s) are held and 1 could be read" in text
    assert not block["complete"]
    assert any("INCOMPLETE" in line for line in H.total_lines(block, 0.0))


def test_a_failed_count_is_not_no_positions(chain):
    chain["v4"] = R.LpReadError("rpc down")
    block = H.build(_Tool({}), None, "robinhood", HOLDER)
    text = "\n".join(block["lines"])
    assert "NOT READ" in text and "none" not in text
    assert not block["complete"]


def test_a_candidate_another_address_owns_is_not_ours(chain):
    chain["owner"] = "0x2222222222222222222222222222222222222222"
    block = H.build(_Tool({}), None, "robinhood", HOLDER)
    assert block["positions"] == [] and not block["complete"]


def test_the_log_scan_walks_back_and_stops_at_the_count(monkeypatch):
    monkeypatch.setattr(H, "LOG_WINDOW", 100)
    asked = []

    def rpc(method, params, timeout=8.0):
        if method == "eth_blockNumber":
            return hex(250)
        f = params[0]
        asked.append((int(f["fromBlock"], 16), int(f["toBlock"], 16)))
        assert f["topics"][2].endswith(HOLDER[2:])
        if asked[-1][1] == 149:
            return [{"topics": ["0x0", "0x0", f["topics"][2], hex(9)]},
                    {"topics": ["0x0", "0x0", f["topics"][2], hex(11)]}]
        return []

    assert H.v4_ids_from_logs(rpc, "0xnpm", HOLDER, 2) == [11, 9]
    assert asked == [(150, 250), (49, 149)]          # stopped: both ids found


def test_a_report_on_injected_balance_seams_reads_no_chain():
    tool = DefiDataTool(holder=HOLDER, balances_fn=lambda *a, **k: {})
    assert tool._lp_holdings_block("robinhood", HOLDER)["lines"] == []


def test_a_v4_position_counts_the_fees_owed_or_says_they_were_not_read(chain, monkeypatch):
    tool = _Tool({_weth(): (2000.0, "high"), TKN: (0.001, "low")})
    without = H.build(tool, None, "robinhood", HOLDER)       # no StateView answer in this fixture
    assert "fees owed were NOT READ" in "\n".join(without["lines"])
    monkeypatch.setattr(V, "position_fees", lambda rpc, chain, pos: (10 ** 17, 5 * 10 ** 18))
    block = H.build(tool, None, "robinhood", HOLDER)
    text = "\n".join(block["lines"])
    assert "amounts include the fees owed: 0.100000 + 5.000000" in text
    assert block["counted_usd"] == pytest.approx(without["counted_usd"] + 0.1 * 2000.0)
