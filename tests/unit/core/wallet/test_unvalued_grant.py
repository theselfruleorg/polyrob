"""An UNVALUED allowance grant must not read as free (S3, 2026-09-14).

The 2026-08-26 exit-untying gave an allowance grant two exemptions at once:
a grant within the wallet's own held balance waived the high-confidence price
bar, and — when NO source could price the token — it was valued at **$0.00**.
The second exemption had no spender condition at all, so the shape

    approve_token(token=<a just-launched coin>, spender=<anyone>,
                  amount=<the wallet's entire holding>)

was authorized on the AUTONOMOUS lane, charged nothing to the daily cap, and
left the whole position claimable by ``transferFrom`` in a later transaction
that never reaches this guard.

Two rules are pinned here:

1. A grant whose token cannot be priced by ANY source is charged the
   AUTONOMOUS CEILING plus a cent — strictly above the ceiling, so it lands on
   the owner queue and is charged to the caps. An unvalued grant must not read
   as free.
2. A grant is EXIT-BOUNDED only when its spender is the destination the intent
   declares AND that spender is a route spender the chain pins. The exit the
   exemption exists for is the sell leg of a position, and that leg approves
   the router — nobody else.
"""
import pytest

from core.wallet import tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas

#: Base's pinned Uniswap V3 SwapRouter02 — the spender a real exit approves.
ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"
#: Base's pinned LI.FI Diamond — the other spender a real exit can approve.
LIFI = "0x1231DEB6f5749EF6cE6943a275A1D3E7486F4EaE"
ATTACKER = "0x1111111111111111111111111111111111111111"
COIN = "0xB2000000000000000000000Ff4a547c891AB1b01"   # a just-launched coin
HOLDER = "0x2222222222222222222222222222222222222222"

HELD = 10 ** 24            # the wallet's entire holding, 18 decimals
CEILING = 5.0              # a pinned autonomous ceiling for these tests


@pytest.fixture(autouse=True)
def _decimals(monkeypatch):
    monkeypatch.setattr(tx_guard, "_decimals_for", lambda chain, token: 18)


@pytest.fixture(autouse=True)
def _ceiling(monkeypatch):
    """Pin the autonomous ceiling so the charged number is exact and no pref
    store on the developer's box can move it."""
    monkeypatch.setattr(tx_guard, "autonomous_max_usd",
                        lambda *a, **kw: CEILING)


def _approve_intent(*, spender=ROUTER, to=None, grant=HELD, held=HELD,
                    max_spend_usd=100.0, **kw):
    base = dict(chain="base", token=COIN, to=(to or spender), amount_raw=0,
                max_spend_usd=max_spend_usd, is_allowance_op=True,
                expected_allowance_grants=((COIN, spender, grant),),
                idempotency_key="k-unvalued", held_balance_raw=held)
    base.update(kw)
    return tx_guard.TxIntent(**base)


def _approve_deltas(*, spender=ROUTER, grant=HELD):
    return Deltas(ok=True, native_delta=0, token_deltas={COIN: 0},
                  allowance_deltas={(COIN, spender): grant})


def _authorize(intent, deltas, *, gate=None, price=None, fallback=None):
    return tx_guard.authorize(
        intent, {"to": COIN, "data": "0x095ea7b3", "value": 0, "chainId": 8453},
        holder=HOLDER,
        gate=gate or PolicyGate(max_per_tx_usd=500.0, daily_cap_usd=500.0),
        execution_context=None,
        simulate_fn=lambda **_: deltas,
        price_fn=(price if callable(price) else (lambda chain, addr: price)),
        fallback_price_fn=(fallback if callable(fallback)
                           else (lambda chain, addr: fallback)),
        rpc_is_pinned_fn=lambda chain: True,
        halted_fn=lambda: False,
        entry_paused_fn=lambda: False,
    )


# --------------------------------------------------------------------------
# 1. An unvalued grant is charged the ceiling, never $0
# --------------------------------------------------------------------------

def test_an_unpriceable_grant_is_not_valued_at_zero():
    d = _authorize(_approve_intent(), _approve_deltas())
    assert d.amount_usd is not None
    assert d.amount_usd > 0.0, "an unvalued grant read as free"


def test_an_unpriceable_grant_is_charged_the_autonomous_ceiling_plus_a_cent():
    d = _authorize(_approve_intent(), _approve_deltas())
    assert d.amount_usd == pytest.approx(CEILING + 0.01)


def test_an_unpriceable_grant_lands_on_the_owner_queue():
    """Strictly above the ceiling, so the owner is asked — the autonomous lane
    is exactly what the $0 valuation bought the attacker."""
    d = _authorize(_approve_intent(), _approve_deltas())
    assert d.allowed is False
    assert d.lane == "owner_queue"


def test_an_unpriceable_grant_is_charged_to_the_daily_cap():
    """A $0 grant consumed no cap at all. The ceiling-charged one does: a cap
    with less headroom than the charge refuses it."""
    gate = PolicyGate(max_per_tx_usd=500.0, daily_cap_usd=CEILING)
    d = _authorize(_approve_intent(), _approve_deltas(), gate=gate)
    assert d.allowed is False
    assert "PolicyGate" in d.reason


def test_an_unpriceable_grant_above_the_declared_max_spend_refuses():
    """The attack shape declares a small max_spend_usd; the charge now exceeds
    it, so the transaction refuses outright instead of passing at $0."""
    d = _authorize(_approve_intent(max_spend_usd=1.0), _approve_deltas())
    assert d.allowed is False
    assert "max_spend_usd" in d.reason


# --------------------------------------------------------------------------
# 2. Exit-bounded means "to the router", not "to anyone"
# --------------------------------------------------------------------------

def test_an_unpriceable_grant_to_an_unpinned_spender_is_not_exit_bounded():
    """The S3 attack: the whole holding of an unpriceable coin approved to an
    address that is not a route spender. It was authorized at $0.00."""
    d = _authorize(_approve_intent(spender=ATTACKER),
                   _approve_deltas(spender=ATTACKER))
    assert d.allowed is False
    assert "no trustworthy price" in d.reason


def test_an_unpriceable_grant_to_an_unpinned_spender_never_consults_the_fallback():
    """The fallback price is the exit exemption's own seam. A grant that is not
    an exit must not reach it, however well the coin happens to price there."""
    d = _authorize(_approve_intent(spender=ATTACKER),
                   _approve_deltas(spender=ATTACKER), fallback=0.000001)
    assert d.allowed is False
    assert "no trustworthy price" in d.reason


def test_a_grant_to_a_spender_the_intent_did_not_declare_is_not_exit_bounded():
    """`to` is the declared counterparty. A grant to somebody else is not the
    transaction the caller described, whoever that somebody is."""
    d = _authorize(_approve_intent(spender=LIFI, to=ROUTER),
                   _approve_deltas(spender=LIFI))
    assert d.allowed is False
    assert "no trustworthy price" in d.reason


def test_a_grant_beyond_the_held_balance_is_still_not_exit_bounded():
    d = _authorize(_approve_intent(held=HELD // 10), _approve_deltas())
    assert d.allowed is False
    assert "no trustworthy price" in d.reason


# --------------------------------------------------------------------------
# 3. Regressions — the legitimate exit leg is untouched
# --------------------------------------------------------------------------

def test_a_priced_grant_to_the_declared_router_is_unchanged():
    d = _authorize(_approve_intent(grant=10 ** 18, held=10 ** 18),
                   _approve_deltas(grant=10 ** 18), price=1.0)
    assert d.allowed is True, d.reason
    assert d.lane == "autonomous"
    assert d.amount_usd == pytest.approx(1.0)


def test_a_fallback_priced_exit_bounded_grant_to_the_router_still_prices(monkeypatch):
    """028's exemption: the high-confidence bar is waived for an exit within
    held balance, and the FALLBACK price is what values it."""
    d = _authorize(_approve_intent(grant=10 ** 18, held=10 ** 18),
                   _approve_deltas(grant=10 ** 18), fallback=2.0)
    assert d.allowed is True, d.reason
    assert d.amount_usd == pytest.approx(2.0)


def test_an_exit_bounded_grant_to_the_pinned_aggregator_is_also_an_exit():
    """A chain's LI.FI Diamond is as pinned as its Uniswap router."""
    d = _authorize(_approve_intent(spender=LIFI, grant=10 ** 18, held=10 ** 18),
                   _approve_deltas(spender=LIFI, grant=10 ** 18), fallback=2.0)
    assert d.allowed is True, d.reason


def test_a_revoke_is_unaffected_and_needs_no_price():
    """A revoke declares no grant. Cleaning up a worthless or unpriceable token
    must never be blocked — it is the hygiene the whole design wants easy."""
    intent = _approve_intent(expected_allowance_grants=(), max_spend_usd=0.01)
    deltas = Deltas(ok=True, native_delta=0, token_deltas={COIN: 0},
                    allowance_deltas={(COIN, ROUTER): -HELD})
    d = _authorize(intent, deltas)
    assert d.allowed is True, d.reason
    assert d.amount_usd == 0.0


# --------------------------------------------------------------------------
# 4. S4 — a `call` that declares a grant has it priced too
# --------------------------------------------------------------------------

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def _call_intent(*, grant_token=USDC, spender=ROUTER, grant=10 ** 18, **kw):
    base = dict(chain="base", token=USDC, to=ROUTER, amount_raw=10 ** 18,
                max_spend_usd=100.0,
                expected_allowance_grants=((grant_token, spender, grant),),
                idempotency_key="k-call")
    base.update(kw)
    return tx_guard.TxIntent(**base)


def _call_deltas(*, spender=ROUTER, grant=10 ** 18):
    return Deltas(ok=True, native_delta=0, token_deltas={USDC: -(10 ** 18)},
                  allowance_deltas={(USDC, spender): grant})


def test_a_call_that_declares_a_grant_has_the_grant_priced():
    """Before S4 the grant rode along free: the pricing loop only ran under
    `is_allowance_op`, so a `call` declaring an allowance was valued on its
    outflow alone."""
    d = _authorize(_call_intent(), _call_deltas(), price=1.0)
    assert d.amount_usd == pytest.approx(2.0), (
        "the $1 outflow plus the $1 grant, not the outflow alone")


def test_a_call_whose_grant_cannot_be_priced_refuses():
    """A `call` declares no held balance, so its grant is never exit-bounded —
    an unpriceable one refuses outright rather than riding along at $0."""
    d = _authorize(_call_intent(grant_token=COIN, grant=HELD),
                   Deltas(ok=True, native_delta=0,
                          token_deltas={USDC: -(10 ** 18)},
                          allowance_deltas={(COIN, ROUTER): HELD}),
                   price=lambda chain, addr: (
                       1.0 if addr.lower() == USDC.lower() else None))
    assert d.allowed is False
    assert "no trustworthy price" in d.reason
