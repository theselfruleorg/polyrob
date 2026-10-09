"""CR-M12 (2026-09-23): LP minimums came from the pool's own spot price with no
independent check, and LP slippage allowed 5000 bps against the swap rail's 1000.
"""
import pytest
from pydantic import ValidationError

from tests.unit.tools.defi import test_lp_verbs as lp
from tests.unit.tools.defi.test_lp_verbs import reads  # noqa: F401  (LP read stubs + flag)
from tools.defi import lp_verbs as V
from tools.defi.trade_tool import LpAddParams, LpRemoveParams


def test_lp_slippage_is_capped_at_the_swap_bound():
    with pytest.raises(ValidationError):
        LpAddParams(chain='base', token_a=lp.T0, token_b=lp.T1, amount_a=1,
                    amount_b=1, max_spend_usd=5, slippage_bps=5000)
    with pytest.raises(ValidationError):
        LpRemoveParams(token_id=42, slippage_bps=1001)
    assert LpRemoveParams(token_id=42, slippage_bps=1000).slippage_bps == 1000


def test_an_existing_pool_far_from_the_independent_price_refuses(monkeypatch):
    monkeypatch.setattr(lp.R, 'pool_address', lambda *a: lp.POOL)
    # Pool says 1:1 (sqrt = 2**96); the independent price says token0 is 2x.
    prices = {lp.T0: 2.0, lp.T1: 1.0}
    with pytest.raises(ValueError, match='disagrees'):
        V.prepare_add(lp.add(), lambda *a: None, lp.HOLDER, lp.NPM,
                      price_fn=lambda c, t: prices.get(t))


def test_an_agreeing_pool_passes_and_says_so(monkeypatch):
    monkeypatch.setattr(lp.R, 'pool_address', lambda *a: lp.POOL)
    plan = V.prepare_add(lp.add(), lambda *a: None, lp.HOLDER, lp.NPM,
                         price_fn=lambda c, t: 1.0)
    assert 'AGREES' in plan.description


def test_no_independent_price_is_named_not_hidden(monkeypatch):
    monkeypatch.setattr(lp.R, 'pool_address', lambda *a: lp.POOL)
    plan = V.prepare_add(lp.add(), lambda *a: None, lp.HOLDER, lp.NPM,
                         price_fn=lambda c, t: None)
    assert 'UNAVAILABLE' in plan.description


@pytest.mark.parametrize('price', [None, float('nan'), float('inf'), -1, 0, True])
@pytest.mark.parametrize('verb', ['lp_add', 'lp_remove'])
def test_live_liquidity_refuses_unavailable_independent_price(monkeypatch, price, verb):
    monkeypatch.setattr(lp.R, 'pool_address', lambda *a: lp.POOL)
    with pytest.raises(ValueError, match='UNAVAILABLE'):
        if verb == 'lp_add':
            V.prepare_add(lp.add(dry_run=False), None, lp.HOLDER, lp.NPM,
                          price_fn=lambda c, t: price)
        else:
            V.prepare_exit(LpRemoveParams(token_id=42, dry_run=False), None,
                           lp.HOLDER, lp.NPM, verb, price_fn=lambda c, t: price)


def test_a_withdrawal_from_a_disagreeing_pool_refuses():
    prices = {lp.T0: 3.0, lp.T1: 1.0}
    with pytest.raises(ValueError, match='disagrees'):
        V.prepare_exit(LpRemoveParams(chain='base', token_id=42), None, lp.HOLDER,
                       lp.NPM, 'lp_remove', price_fn=lambda c, t: prices.get(t))


@pytest.mark.asyncio
async def test_the_verb_wires_the_independent_price(monkeypatch):
    monkeypatch.setattr(lp.R, 'pool_address', lambda *a: lp.POOL)
    t, gate = lp.tool()
    t._price_fn = lambda c, a: {lp.T0: 2.0, lp.T1: 1.0}.get(a)
    res = await t.lp_add(lp.add(dry_run=False))
    assert res.error and 'disagrees' in res.error
    assert not gate.recorded


def test_exit_price_falls_back_to_the_exit_grade_price():
    """Regression: a live lp_remove refused when a leg had only an exit-grade
    price (e.g. the agent's own launched token), locking its own liquidity."""
    import types
    tool = types.SimpleNamespace(
        _price=lambda c, t: None if t == lp.T0 else 1.0,
        _fallback_price=lambda c, t: 1.0)
    fn = V.exit_price_fn(tool)
    p = types.SimpleNamespace(chain='base', dry_run=False)
    note = V.pool_price_check(p, lp.T0, lp.T1, 18, 18, 2 ** 96, fn)
    assert 'AGREES' in note


def test_exit_with_no_price_at_all_still_refuses():
    import types
    tool = types.SimpleNamespace(_price=lambda c, t: None, _fallback_price=lambda c, t: None)
    p = types.SimpleNamespace(chain='base', dry_run=False)
    with pytest.raises(ValueError, match='refused'):
        V.pool_price_check(p, lp.T0, lp.T1, 18, 18, 2 ** 96, V.exit_price_fn(tool))
