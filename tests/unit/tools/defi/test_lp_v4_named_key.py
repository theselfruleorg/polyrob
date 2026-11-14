"""117 W5/W6: v4 reads on a pool that is NOT a Pons pool — a PoolKey named in
full, or the key of a position — and the position list (the indexer names
candidates, the chain proves each one)."""
import asyncio

import pytest

from core.wallet import abi, dex_registry, liquidity_guard as G
from tools.defi import lp_reads, lp_v4 as V, nft_verbs
from tools.defi.data_tool import (DefiDataTool, LpPoolInfoParams, LpPositionsParams,
                                  LpQuoteParams)
from tests.unit.tools.defi.test_lp_v4_reads import _Rpc

ROW = dex_registry.row_for("robinhood", "v4")
NPM = ROW.position_manager
ZERO = "0x" + "0" * 40
TKN = "0x" + "55" * 20
OWNER = "0x" + "ab" * 20
OTHER = "0x" + "cd" * 20
KEY = {"currency0": ZERO, "currency1": TKN, "fee": 10000, "tickSpacing": 200, "hooks": ZERO}
LIQ = 10 ** 21


def _info(lo, hi):
    return ((hi & 0xFFFFFF) << 32) | ((lo & 0xFFFFFF) << 8)


def _rpc(sqrt=2 ** 96, tick=0, owner=OWNER, liquidity=LIQ, lo=-200, hi=200):
    rpc = _Rpc()
    rpc.add(ROW.state_view, V.STATE_VIEW_GET_SLOT0, [sqrt, tick, 0, 10000])
    rpc.add(ROW.state_view, V.STATE_VIEW_GET_LIQUIDITY, [LIQ if sqrt else 0])
    rpc.add(NPM, V.POSM_OWNER_OF, [owner])
    rpc.add(NPM, G.POSM_POOL_AND_POSITION_INFO,
            [(ZERO, TKN, 10000, 200, ZERO), _info(lo, hi)])
    rpc.add(NPM, V.POSM_POSITION_LIQUIDITY, [liquidity])
    rpc.add(TKN, abi_decimals(), [18])
    return rpc


def abi_decimals():
    from tools.defi import lp_abi as A
    return A.ERC20_DECIMALS


def _run(coro):
    return asyncio.run(coro)


# ---- the key -------------------------------------------------------------

def test_explicit_key_sorts_the_currencies_and_hashes_the_pool_id():
    key, pid, flipped = V.explicit_key(TKN, "native", 10000, 200)
    assert key == KEY and flipped is True
    assert pid == lp_reads.pool_id(KEY)
    assert V.explicit_key("native", TKN, 10000, 200)[2] is False
    # every field is part of the id
    assert V.explicit_key("native", TKN, 3000, 200)[1] != pid
    assert V.explicit_key("native", TKN, 10000, 60)[1] != pid
    assert V.explicit_key("native", TKN, 10000, 200, hooks=OTHER)[1] != pid


@pytest.mark.parametrize("args", [
    ("native", "native", 10000, 200), (TKN, TKN, 10000, 200),
    ("native", TKN, None, 200), ("native", "0xnothex", 10000, 200),
])
def test_explicit_key_refuses_a_key_that_is_not_complete(args):
    with pytest.raises(V.LpReadError):
        V.explicit_key(*args)


def test_position_info_word_decodes_signed_ticks():
    assert V.decode_position_info(_info(-160000, 380000)) == (-160000, 380000)
    assert V.decode_position_info(_info(-887200, 887200) | (7 << 56) | 1) == (-887200, 887200)


# ---- one position --------------------------------------------------------

def test_position_reads_owner_key_ticks_and_liquidity():
    pos = V.position(_rpc(), "robinhood", 42)
    assert pos.owner == OWNER and pos.key == KEY and pos.pool_id == lp_reads.pool_id(KEY)
    assert (pos.tick_lower, pos.tick_upper, pos.liquidity) == (-200, 200, LIQ)


def test_a_position_that_does_not_exist_raises_not_an_empty_position():
    rpc = _rpc()
    rpc.add(NPM, G.POSM_POOL_AND_POSITION_INFO, [(ZERO, ZERO, 0, 0, ZERO), 0])
    with pytest.raises(V.LpReadError, match="no PoolKey"):
        V.position(rpc, "robinhood", 42)
    with pytest.raises(Exception):
        V.position(lambda m, p, timeout=8.0: "0x", "robinhood", 42)


def test_lp_positions_v4_by_id_reads_the_chain():
    tool = DefiDataTool(lp_rpc=_rpc(), holder=None)
    res = _run(tool.lp_positions(LpPositionsParams(chain="robinhood", protocol="v4", token_id=42)))
    text = res.extracted_content
    assert text, res.error
    assert f"v4 position #42 on robinhood — owner {OWNER}" in text
    assert lp_reads.pool_id(KEY) in text
    assert "ticks: [-200,200]" in text and "in range" in text
    assert "holds now:" in text and "fees owed" in text


def test_position_lines_names_out_of_range_and_an_uninitialised_pool():
    out = "\n".join(V.position_lines(_rpc(tick=400), "robinhood", 42, lambda t: 18, holder=OTHER))
    assert "OUT of range" in out and "(NOT this wallet)" in out
    empty = "\n".join(V.position_lines(_rpc(sqrt=0), "robinhood", 42, lambda t: 18, holder=OWNER))
    assert "NOT initialized" in empty and "(this wallet)" in empty and "holds now" not in empty


# ---- pool info and quote on a named key ----------------------------------

def test_lp_pool_info_v4_reads_a_named_key_and_a_position_key():
    tool = DefiDataTool(lp_rpc=_rpc(), holder=None)
    named = _run(tool.lp_pool_info(LpPoolInfoParams(
        chain="robinhood", protocol="v4", token_a=TKN, token_b="native",
        fee=10000, tick_spacing=200))).extracted_content
    assert lp_reads.pool_id(KEY) in named and "the PoolKey as named" in named
    assert "1 currency0 = 1 currency1" in named
    by_id = _run(tool.lp_pool_info(LpPoolInfoParams(
        chain="robinhood", protocol="v4", token_id=42))).extracted_content
    assert lp_reads.pool_id(KEY) in by_id and "the PoolKey of position #42" in by_id


def test_lp_pool_info_v4_says_a_named_pool_is_not_initialised():
    tool = DefiDataTool(lp_rpc=_rpc(sqrt=0), holder=None)
    res = _run(tool.lp_pool_info(LpPoolInfoParams(
        chain="robinhood", protocol="v4", token_a="native", token_b=TKN,
        fee=10000, tick_spacing=200)))
    assert "NOT initialized" in res.extracted_content


def test_lp_quote_v4_named_key_takes_a_custom_range():
    tool = DefiDataTool(lp_rpc=_rpc(), holder=None)
    res = _run(tool.lp_quote(LpQuoteParams(
        chain="robinhood", protocol="v4", token_a="native", token_b=TKN, amount_a=1.0,
        fee=10000, tick_spacing=200, range="ticks:-200,200")))
    text = res.extracted_content
    assert text, res.error
    assert "ticks: [-200,200]" in text and "amount0: 1" in text and "pool tick 0" in text
    bad = _run(tool.lp_quote(LpQuoteParams(
        chain="robinhood", protocol="v4", token_a="native", token_b=TKN, amount_a=1.0,
        fee=10000, tick_spacing=200, range="ticks:-150,200")))
    assert "multiples of 200" in bad.error


def test_lp_quote_v4_quotes_a_seed_at_the_initial_price_or_refuses():
    tool = DefiDataTool(lp_rpc=_rpc(sqrt=0), holder=None)
    base = dict(chain="robinhood", protocol="v4", token_a="native", token_b=TKN,
                amount_a=1.0, fee=10000, tick_spacing=200, range="full")
    assert "give initial_price" in _run(tool.lp_quote(LpQuoteParams(**base))).error
    text = _run(tool.lp_quote(LpQuoteParams(**base, initial_price=4.0))).extracted_content
    assert "NOT initialized — quoted at initial_price (a seed)" in text
    assert "1 currency0 = 4 currency1" in text


# ---- the list ------------------------------------------------------------

def _listing(monkeypatch, payload, rpc=None, held=1, log_ids=None):
    """The list read: the count from balanceOf, ids from the log scan (faked
    through its seam; None = the scan fails) and then the indexer."""
    from tools.defi import lp_abi, lp_holdings
    monkeypatch.setenv("ALCHEMY_API_KEY", "test-key")
    urls = []

    def fetch(url):
        urls.append(url)
        if isinstance(payload, Exception):
            raise payload
        return payload

    def scan(*a, **k):
        if log_ids is None:
            raise RuntimeError("log range too large")
        return list(log_ids)
    monkeypatch.setattr(lp_holdings, "v4_ids_from_logs", scan)
    rpc = rpc or _rpc()
    if held is not None:
        rpc.add(NPM, lp_abi.NPM_BALANCE_OF, [held])
    tool = DefiDataTool(lp_rpc=rpc, holder=None, nft_fetch=fetch)
    res = _run(tool.lp_positions(LpPositionsParams(chain="robinhood", protocol="v4", address=OWNER)))
    return res, urls


def test_lp_positions_v4_finds_ids_in_the_logs_and_needs_no_indexer(monkeypatch):
    res, urls = _listing(monkeypatch, RuntimeError("no indexer"), log_ids=[42])
    text = res.extracted_content
    assert text, res.error
    assert "holds 1 Uniswap v4 position(s); 1 id(s) found" in text and "v4 position #42" in text
    assert not urls                                       # the logs gave every id


def test_lp_positions_v4_falls_back_to_the_indexer_and_reads_each_on_chain(monkeypatch):
    res, urls = _listing(monkeypatch, {"ownedNfts": [
        {"contract": {"address": NPM, "tokenType": "ERC721"}, "tokenId": "42"},
        {"contract": {"address": OTHER, "tokenType": "ERC721"}, "tokenId": "7"}]})
    text = res.extracted_content
    assert text, res.error
    assert "v4 position #42" in text and "#7" not in text    # another contract is not a position
    assert f"contractaddresses[]={NPM.lower()}" in urls[0].lower()


def test_lp_positions_v4_marks_a_position_the_wallet_no_longer_owns(monkeypatch):
    res, _ = _listing(monkeypatch, {}, rpc=_rpc(owner=OTHER), log_ids=[42])
    assert "(NOT this wallet)" in res.extracted_content   # the chain wins over the candidate


def test_lp_positions_v4_zero_held_is_an_answer_and_a_failed_read_is_not(monkeypatch):
    res, _ = _listing(monkeypatch, {}, held=0, log_ids=[])
    assert "holds no Uniswap v4 position" in res.extracted_content and res.error is None
    res, _ = _listing(monkeypatch, {}, held=None, log_ids=[])       # the count cannot be read
    assert res.extracted_content is None and "NOT READ" in res.error
    res, _ = _listing(monkeypatch, RuntimeError("indexer down"), held=2)
    assert res.extracted_content is None and "holds 2" in res.error and "NOT be found" in res.error


def test_lp_positions_v4_says_how_many_held_positions_it_could_not_find(monkeypatch):
    res, _ = _listing(monkeypatch, {}, held=3, log_ids=[42])
    assert "2 more are held but their ids were NOT found" in res.extracted_content


def test_a_position_that_was_never_minted_is_named():
    class Rpc(_Rpc):
        def __call__(self, method, params, timeout=8.0):
            raise RuntimeError("eth_call: execution reverted: NOT_MINTED")
    with pytest.raises(V.LpReadError, match="does not exist"):
        V.position(Rpc(), "robinhood", 999)


def test_enumerate_holdings_filters_by_contract():
    urls = []
    nft_verbs.enumerate_holdings(chain="robinhood", address=OWNER, api_key="k",
                                 fetch=lambda u: urls.append(u) or {}, contracts=(NPM,))
    assert urls[0].lower().endswith(f"&contractaddresses[]={NPM.lower()}")
    urls.clear()
    nft_verbs.enumerate_holdings(chain="robinhood", address=OWNER, api_key="k",
                                 fetch=lambda u: urls.append(u) or {})
    assert "contractAddresses" not in urls[0]


# ---- fees owed and pool activity (the statistics) -------------------------

Q128 = 2 ** 128


def _with_fees(rpc, last=(5 * Q128 // LIQ, 7 * Q128 // LIQ), grown=(10 ** 15, 3 * 10 ** 18)):
    """StateView answers so the position is owed exactly *grown* (raw)."""
    now = tuple(l + g * Q128 // LIQ + 1 for l, g in zip(last, grown))
    rpc.add(ROW.state_view, V.STATE_VIEW_GET_POSITION_INFO, [LIQ, last[0], last[1]])
    rpc.add(ROW.state_view, V.STATE_VIEW_GET_FEE_GROWTH_INSIDE, list(now))
    return rpc


def test_position_fees_is_growth_since_the_last_touch_times_liquidity():
    rpc = _with_fees(_rpc())
    pos = V.position(rpc, "robinhood", 42)
    f0, f1 = V.position_fees(rpc, "robinhood", pos)
    assert 10 ** 15 <= f0 <= 10 ** 15 + 1 and 3 * 10 ** 18 <= f1 <= 3 * 10 ** 18 + 1


def test_position_fees_survives_a_fee_growth_counter_that_wrapped():
    rpc = _rpc()
    rpc.add(ROW.state_view, V.STATE_VIEW_GET_POSITION_INFO, [LIQ, 2 ** 256 - 10 * Q128, 0])
    rpc.add(ROW.state_view, V.STATE_VIEW_GET_FEE_GROWTH_INSIDE, [5 * Q128, 0])
    pos = V.position(rpc, "robinhood", 42)
    assert V.position_fees(rpc, "robinhood", pos) == (15 * LIQ, 0)


def test_the_position_read_shows_fees_owed_or_says_they_were_not_read():
    with_fees = "\n".join(V.position_lines(_with_fees(_rpc()), "robinhood", 42, lambda t: 18))
    assert "fees owed (uncollected): 0.001 currency0 + 3 currency1" in with_fees
    assert "LP fee 1%" in with_fees
    without = "\n".join(V.position_lines(_rpc(), "robinhood", 42, lambda t: 18))
    assert "fees owed: NOT READ" in without and "fees owed (uncollected)" not in without


def _swap_log(a0, a1, fee=10990):
    word = lambda n: (n % 2 ** 256).to_bytes(32, "big")
    return {"data": "0x" + b"".join(word(n) for n in (a0, a1, 2 ** 96, LIQ, 0, fee)).hex(),
            "topics": [V.TOPIC_SWAP, lp_reads.pool_id(KEY), "0x" + "0" * 64]}


class _LogRpc(_Rpc):
    """The eth_call fake plus a block clock and Swap logs."""
    def __init__(self, logs, head=1_000_000, block_s=0.1):
        super().__init__()
        self.logs, self.head, self.block_s, self.asked = logs, head, block_s, []

    def __call__(self, method, params, timeout=8.0):
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getBlockByNumber":
            return {"timestamp": hex(int(int(params[0], 16) * self.block_s))}
        if method == "eth_getLogs":
            self.asked.append(params[0])
            if isinstance(self.logs, Exception):
                raise self.logs
            return self.logs
        return super().__call__(method, params, timeout)


def test_pool_activity_sums_volume_and_the_lp_part_of_the_fee():
    # one buy (1 native in), one sell (2 M token in); protocol fee 0.1% each way
    rpc = _LogRpc([_swap_log(-10 ** 18, 6 * 10 ** 24), _swap_log(3 * 10 ** 17, -2 * 10 ** 24)])
    a = V.pool_activity(rpc, "robinhood", lp_reads.pool_id(KEY), protocol_fee=1000 | (1000 << 12))
    assert a["swaps"] == 2
    assert a["volume0"] == 13 * 10 ** 17 and a["volume1"] == 8 * 10 ** 24
    assert a["lp_fees0"] == 10 ** 18 * 9990 // 10 ** 6       # 1.099% total less 0.1% protocol
    assert a["lp_fees1"] == 2 * 10 ** 24 * 9990 // 10 ** 6
    q = rpc.asked[0]
    assert q["address"] == ROW.pool_manager and q["topics"] == [V.TOPIC_SWAP, lp_reads.pool_id(KEY)]
    assert int(q["toBlock"], 16) - int(q["fromBlock"], 16) == 864_000     # 24 h of 0.1 s blocks


def test_the_swap_topic_is_the_event_signature():
    from eth_utils import keccak
    sig = "Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)"
    assert V.TOPIC_SWAP == "0x" + keccak(text=sig).hex()


def test_pool_info_shows_the_fees_and_the_last_day_or_says_not_read():
    base = _rpc()
    rpc = _LogRpc([_swap_log(-10 ** 18, 6 * 10 ** 24)])
    rpc.calls = base.calls
    rpc.add(ROW.state_view, V.STATE_VIEW_GET_SLOT0, [2 ** 96, 0, 1000 | (1000 << 12), 10000])
    text = "\n".join(V.pool_info_lines(rpc, "robinhood", token_id=42, decimals_fn=lambda t: 18))
    assert "LP fee 1% + protocol fee 0.1%" in text
    assert "1 swap(s), volume 1 currency0 / 6000000 currency1" in text
    assert "LP fees earned in that window: about 0.00999 currency0 + 0 currency1" in text
    rpc.logs = RuntimeError("log range too large")
    text = "\n".join(V.pool_info_lines(rpc, "robinhood", token_id=42, decimals_fn=lambda t: 18))
    assert "last 24 h: NOT READ" in text and "swap(s)" not in text
