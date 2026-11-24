"""A v4 liquidity grant of a token no source can price is judged at the mint.

2026-11-21: a confirmed v4 move of the agent's own coin stopped on its Permit2
grant ("the allowance grant has no trustworthy price"). A grant to the pinned
Permit2 or the pinned v4 PositionManager moves nothing by itself: the mint that
spends it is simulated and priced by the guard. Every other unpriceable grant
still refuses (or is charged, when exit-bounded) as before.
"""
from core.wallet import tx_guard
from core.wallet.liquidity_guard import v4_grant_moves_nothing

PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
V4_PM = "0x58daec3116aae6d93017baaea7749052e8a04fa7"  # robinhood
OTHER = "0x1111111111111111111111111111111111111111"
COIN = "0xB2000000000000000000000Ff4a547c891AB1b01"


def _intent(spender):
    return tx_guard.TxIntent(
        chain="robinhood", token=COIN, to=spender, amount_raw=0, max_spend_usd=1.0,
        is_allowance_op=True, expected_allowance_grants=((COIN, spender, 10 ** 24),),
        idempotency_key="k-v4-grant")


def _price(intent):
    return tx_guard._price_allowance_grants(
        intent, price_fn=lambda c, t: None, fallback_price_fn=lambda c, t: None,
        execution_context=None)


def test_the_pinned_v4_spenders_move_nothing_by_themselves():
    assert v4_grant_moves_nothing("robinhood", V4_PM)
    assert v4_grant_moves_nothing("robinhood", PERMIT2.lower())
    assert not v4_grant_moves_nothing("robinhood", OTHER)
    assert not v4_grant_moves_nothing("nochain", V4_PM)
    assert not v4_grant_moves_nothing("robinhood", None)


def test_an_unpriceable_permit2_grant_to_the_position_manager_is_not_refused():
    assert _price(_intent(V4_PM)) == (0.0, None)


def test_an_unpriceable_erc20_grant_to_permit2_is_not_refused():
    assert _price(_intent(PERMIT2)) == (0.0, None)


def test_an_unpriceable_grant_to_another_spender_still_refuses():
    total, decision = _price(_intent(OTHER))
    assert total == 0.0 and decision is not None and not decision.allowed
    assert "no trustworthy price" in decision.reason
