"""defi_data.lp_positions / lp_pool_info / lp_quote — Uniswap v3 chain reads
surfaced as agent actions (proposal 048 P12).

Every RPC here is the same `FakeRpc` Task 4 built for `tools/defi/lp_reads.py`
(keyed on `(to, selector)` only, imported rather than duplicated), and every
symbol lookup is stubbed via the tool's existing `identity_fn` seam — the
identity read otherwise falls through to a real chain RPC
(`core/wallet/tokens.py::get_token_identity`), which this test suite must
never reach (see `tests/unit/tools/defi/conftest.py`).
"""
import asyncio

from tools.defi import lp_abi as A
from tools.defi.data_tool import (
    DefiDataTool, LpPoolInfoParams, LpPositionsParams, LpQuoteParams,
)
from tests.unit.tools.defi.test_lp_reads import FACTORY, NPM, POOL, T0, T1, FakeRpc

ADDR = "0x" + "1" * 40
ZERO = "0x" + "0" * 40


def _run(coro):
    return asyncio.run(coro)


def _ident_fn(chain, addr):
    return type("I", (), {"symbol": addr[-4:], "name": addr, "decimals": 18,
                          "verified": False, "metadata_changed": False})()


def _tool(rpc, **kw):
    kw.setdefault("identity_fn", _ident_fn)
    return DefiDataTool(lp_rpc=rpc, **kw)


def _configure_pool(rpc, *, sqrt=2 ** 96, tick=0, liquidity=10 ** 18,
                     dec0=18, dec1=18, fee=3000, spacing=60):
    rpc.add(FACTORY, A.FACTORY_GET_POOL, [POOL])
    rpc.add(POOL, A.POOL_TOKEN0, [T0])
    rpc.add(POOL, A.POOL_TOKEN1, [T1])
    rpc.add(POOL, A.POOL_FEE, [fee])
    rpc.add(POOL, A.POOL_TICK_SPACING, [spacing])
    rpc.add(POOL, A.POOL_SLOT0, [sqrt, tick, 0, 1, 1, 0, True])
    rpc.add(POOL, A.POOL_LIQUIDITY, [liquidity])
    rpc.add(T0, A.ERC20_DECIMALS, [dec0])
    rpc.add(T1, A.ERC20_DECIMALS, [dec1])


# --------------------------------------------------------------------------
# lp_positions
# --------------------------------------------------------------------------

def test_lp_positions_balance_zero_is_a_working_read():
    rpc = FakeRpc()
    rpc.add(NPM, A.NPM_BALANCE_OF, [0])
    tool = _tool(rpc)
    res = _run(tool.lp_positions(LpPositionsParams(chain="robinhood", address=ADDR)))
    assert res.error is None
    content = (res.extracted_content or "").lower()
    assert "no uniswap v3 positions" in content
    assert "working read" in content


def test_lp_positions_unreadable_is_an_error_never_an_empty_list():
    rpc = FakeRpc()  # nothing configured -> balanceOf returns "0x" -> LpReadError
    tool = _tool(rpc)
    res = _run(tool.lp_positions(LpPositionsParams(chain="robinhood", address=ADDR)))
    assert res.extracted_content is None
    assert res.error is not None
    assert "could not be completed" in res.error.lower()
    assert "unknown is not zero" in res.error.lower()


def test_lp_positions_renders_pool_range_and_fees():
    rpc = FakeRpc()
    rpc.add(NPM, A.NPM_BALANCE_OF, [1])
    rpc.add(NPM, A.NPM_TOKEN_OF_OWNER_BY_INDEX, [42])
    rpc.add(NPM, A.NPM_POSITIONS,
            [0, ZERO, T0, T1, 3000, -60, 60, 10 ** 18, 0, 0, 5, 7])
    _configure_pool(rpc)
    rpc.add(POOL, A.POOL_FEE_GROWTH_GLOBAL0, [0])
    rpc.add(POOL, A.POOL_FEE_GROWTH_GLOBAL1, [0])
    rpc.add(POOL, A.POOL_TICKS, [0, 0, 0, 0, 0, 0, 0, True])
    tool = _tool(rpc)
    res = _run(tool.lp_positions(LpPositionsParams(chain="robinhood", address=ADDR)))
    assert res.error is None
    content = res.extracted_content or ""
    assert "#42" in content
    assert "IN RANGE" in content
    assert "holds:" in content and "fees:" in content


def test_lp_positions_v4_is_an_honest_refusal():
    tool = _tool(FakeRpc())
    res = _run(tool.lp_positions(
        LpPositionsParams(chain="robinhood", protocol="v4", address=ADDR)))
    assert res.extracted_content is None
    assert res.error is not None
    assert "phase 3 of proposal 048" in res.error
    assert "v3 only" in res.error


# --------------------------------------------------------------------------
# lp_pool_info
# --------------------------------------------------------------------------

def test_lp_pool_info_missing_pool_names_lp_add():
    rpc = FakeRpc()
    rpc.add(FACTORY, A.FACTORY_GET_POOL, [ZERO])
    tool = _tool(rpc)
    res = _run(tool.lp_pool_info(
        LpPoolInfoParams(chain="robinhood", token_a=T0, token_b=T1, fee=3000)))
    assert res.error is None
    content = (res.extracted_content or "").lower()
    assert "does not exist" in content
    assert "lp_add with initial_price" in content


def test_lp_pool_info_reads_an_existing_pool():
    rpc = FakeRpc()
    _configure_pool(rpc)
    tool = _tool(rpc)
    res = _run(tool.lp_pool_info(
        LpPoolInfoParams(chain="robinhood", token_a=T0, token_b=T1, fee=3000)))
    assert res.error is None
    content = res.extracted_content or ""
    assert POOL.lower() in content.lower()
    assert "liquidity:" in content


def test_lp_pool_info_reads_an_existing_pool_by_address_directly():
    rpc = FakeRpc()
    _configure_pool(rpc)
    tool = _tool(rpc)
    res = _run(tool.lp_pool_info(LpPoolInfoParams(chain="robinhood", pool=POOL)))
    assert res.error is None
    assert POOL.lower() in (res.extracted_content or "").lower()


def test_lp_pool_info_needs_pool_or_the_full_triple():
    tool = _tool(FakeRpc())
    res = _run(tool.lp_pool_info(LpPoolInfoParams(chain="robinhood", token_a=T0)))
    assert res.error is not None


def test_lp_pool_info_v4_is_an_honest_refusal():
    tool = _tool(FakeRpc())
    res = _run(tool.lp_pool_info(LpPoolInfoParams(chain="robinhood", protocol="v4")))
    assert res.error is not None
    assert "phase 3 of proposal 048" in res.error
    assert "v3 only" in res.error


# --------------------------------------------------------------------------
# lp_quote
# --------------------------------------------------------------------------

def test_lp_quote_full_range_single_sided_existing_pool():
    rpc = FakeRpc()
    _configure_pool(rpc)
    tool = _tool(rpc)
    res = _run(tool.lp_quote(LpQuoteParams(
        chain="robinhood", token_a=T0, token_b=T1, fee=3000,
        amount_a=1.0, range="full")))
    assert res.error is None
    content = res.extracted_content or ""
    assert "liquidity" in content
    assert "amount0" in content
    assert "amount1" in content
    assert "implied price" in content


def test_lp_quote_missing_pool_requires_initial_price():
    rpc = FakeRpc()
    rpc.add(FACTORY, A.FACTORY_GET_POOL, [ZERO])
    tool = _tool(rpc)
    res = _run(tool.lp_quote(LpQuoteParams(
        chain="robinhood", token_a=T0, token_b=T1, fee=3000, amount_a=1.0)))
    assert res.extracted_content is None
    assert res.error is not None
    assert "initial_price" in res.error


def test_lp_quote_new_pool_with_initial_price_is_a_happy_path():
    rpc = FakeRpc()
    rpc.add(FACTORY, A.FACTORY_GET_POOL, [ZERO])
    rpc.add(T0, A.ERC20_DECIMALS, [18])
    rpc.add(T1, A.ERC20_DECIMALS, [18])
    tool = _tool(rpc)
    res = _run(tool.lp_quote(LpQuoteParams(
        chain="robinhood", token_a=T0, token_b=T1, fee=3000,
        amount_a=1.0, range="full", initial_price=1.5)))
    assert res.error is None
    content = res.extracted_content or ""
    assert "not-yet-created pool" in content
    assert "liquidity" in content
    assert "implied price" in content


def test_lp_quote_custom_range_existing_pool():
    rpc = FakeRpc()
    _configure_pool(rpc)
    tool = _tool(rpc)
    res = _run(tool.lp_quote(LpQuoteParams(
        chain="robinhood", token_a=T0, token_b=T1, fee=3000,
        amount_a=1.0, amount_b=1.0, range="0.5,2.0")))
    assert res.error is None
    content = res.extracted_content or ""
    assert "ticks:" in content
    assert "liquidity" in content


def test_lp_quote_flipped_token_order_still_resolves():
    # token_a is T1, the numerically HIGHER address -> sort_tokens flips it.
    rpc = FakeRpc()
    _configure_pool(rpc)
    tool = _tool(rpc)
    res = _run(tool.lp_quote(LpQuoteParams(
        chain="robinhood", token_a=T1, token_b=T0, fee=3000,
        amount_a=1.0, range="full")))
    assert res.error is None
    content = res.extracted_content or ""
    assert "liquidity" in content
    assert "implied price" in content


def test_lp_quote_bad_fee_tier_is_refused():
    tool = _tool(FakeRpc())
    res = _run(tool.lp_quote(LpQuoteParams(
        chain="robinhood", token_a=T0, token_b=T1, fee=1234, amount_a=1.0)))
    assert res.error is not None


def test_lp_quote_needs_an_amount():
    rpc = FakeRpc()
    _configure_pool(rpc)
    tool = _tool(rpc)
    res = _run(tool.lp_quote(LpQuoteParams(
        chain="robinhood", token_a=T0, token_b=T1, fee=3000)))
    assert res.error is not None
    assert "amount_a" in res.error


def test_lp_quote_v4_is_an_honest_refusal():
    tool = _tool(FakeRpc())
    res = _run(tool.lp_quote(LpQuoteParams(
        chain="robinhood", protocol="v4", token_a=T0, token_b=T1, fee=3000,
        amount_a=1.0)))
    assert res.error is not None
    assert "phase 3 of proposal 048" in res.error
    assert "v3 only" in res.error
