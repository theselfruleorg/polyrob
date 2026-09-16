"""tx_guard's two new shapes (042): the declared MINIMUM INFLOW, and CREATE.

Same discipline as the main suite — nearly every test is a refusal.
"""
import pytest

from core.wallet import tx_guard, token_template
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
AUSDC = "0x4e65fE4DbA92790696d040ac24Aa414708F5c0AB"
POOL = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
HOLDER = "0x2222222222222222222222222222222222222222"


def _gate():
    return PolicyGate(max_per_tx_usd=100.0, daily_cap_usd=1000.0)


def _authorize(intent, deltas, *, tx=None, gate=None, price=1.0):
    return tx_guard.authorize(
        intent,
        tx if tx is not None else {"to": USDC, "data": "0xdeadbeef", "value": 0,
                                   "chainId": 8453, "nonce": 5,
                                   "gas": 120_000, "maxFeePerGas": 10 ** 9},
        holder=HOLDER,
        gate=gate or _gate(),
        execution_context=None,
        simulate_fn=lambda **_: deltas,
        price_fn=lambda chain, addr: price,
        rpc_is_pinned_fn=lambda chain: True,
        halted_fn=lambda: False,
        entry_paused_fn=lambda: False,
        forged_fn=lambda ctx, tool: False,
    )


# ==========================================================================
# The declared MINIMUM INFLOW — the assertion 038 §3.3 named
# ==========================================================================

def _call_intent(**kw):
    base = dict(chain="base", token=USDC, to=POOL, amount_raw=1_000_000,
                max_spend_usd=5.0, idempotency_key="k",
                inflow_token=AUSDC, min_inflow_raw=990_000)
    base.update(kw)
    return tx_guard.TxIntent(**base)


def _call_deltas(**kw):
    base = dict(ok=True, native_delta=0,
                token_deltas={USDC: -1_000_000, AUSDC: 995_000},
                allowance_deltas={}, gas_used=210_000)
    base.update(kw)
    return Deltas(**base)


def test_a_call_that_returns_enough_is_authorized():
    d = _authorize(_call_intent(), _call_deltas())
    assert d.allowed is True, d.reason


def test_the_038_hole_is_closed_a_call_that_returns_NOTHING_refuses():
    """*"A call that took USDC and returned no aUSDC clears every current
    check."* It does not any more."""
    d = _authorize(_call_intent(),
                   _call_deltas(token_deltas={USDC: -1_000_000, AUSDC: 0}))
    assert d.allowed is False
    assert "measured NO inflow" in d.reason


def test_a_short_return_refuses():
    d = _authorize(_call_intent(),
                   _call_deltas(token_deltas={USDC: -1_000_000, AUSDC: 100}))
    assert d.allowed is False
    assert "below the declared minimum" in d.reason


def test_a_minimum_with_no_token_to_measure_it_on_refuses():
    d = _authorize(_call_intent(inflow_token=None), _call_deltas())
    assert d.allowed is False
    assert "no inflow_token" in d.reason


def test_a_zero_minimum_declared_explicitly_refuses_as_a_caller_bug():
    d = _authorize(_call_intent(min_inflow_raw=0), _call_deltas())
    assert d.allowed is False
    assert "greater than zero" in d.reason


def test_no_minimum_declared_is_byte_identical_legacy_behaviour():
    """Unset must not start refusing swaps that shipped before 042."""
    d = _authorize(_call_intent(min_inflow_raw=None),
                   _call_deltas(token_deltas={USDC: -1_000_000, AUSDC: 0}))
    assert d.allowed is True, d.reason


# ==========================================================================
# CREATE
# ==========================================================================

def _deploy_tx(**kw):
    base = {"to": None, "data": token_template.INIT_CODE, "value": 0,
            "chainId": 8453, "nonce": 11, "gas": 120_000,
            "maxFeePerGas": 10 ** 9}
    base.update(kw)
    return base


def _deploy_intent(**kw):
    base = dict(chain="base", token=None, to=None, amount_raw=0,
                max_spend_usd=5.0, idempotency_key="d",
                is_deploy=True, init_code=token_template.INIT_CODE,
                expected_runtime=token_template.RUNTIME,
                immutable_slots=token_template.IMMUTABLE_SLOTS)
    base.update(kw)
    return tx_guard.TxIntent(**base)


def _deploy_deltas(**kw):
    base = dict(ok=True, native_delta=0, token_deltas={}, allowance_deltas={},
                gas_used=501_207, return_data=token_template.RUNTIME)
    base.update(kw)
    return Deltas(**base)


def test_a_clean_template_deploy_is_authorized_and_names_its_address():
    d = _authorize(_deploy_intent(), _deploy_deltas(), tx=_deploy_tx())
    assert d.allowed is True, d.reason
    assert d.deploy is not None
    assert d.deploy.template_matched is True
    assert d.deploy.predicted_address.startswith("0x")
    assert d.deploy.runtime_hash == token_template.RUNTIME_HASH


def test_a_zero_value_deploy_is_still_PRICED_by_its_fee():
    """A deployment that sends nothing still COSTS its fee. Pricing only the
    (zero) endowment would book every deployment at $0.00 and consume none of
    the cap — the confident-zero class."""
    d = _authorize(_deploy_intent(), _deploy_deltas(), tx=_deploy_tx(),
                   price=2600.0)          # a realistic native price
    assert d.allowed is True, d.reason
    # 501,207 gas x1.5 at 1 gwei = 0.00075 ETH; at $2600 that is ~$1.96.
    assert d.amount_usd == pytest.approx(1.95, abs=0.05)


def test_a_sub_cent_deploy_rounds_like_every_other_verb():
    """Not a special case: `authorize` rounds every amount to cents (a
    $1.9903-vs-$1.99 refusal is a quote artifact, not a policy). An L2 deploy
    genuinely costs fractions of a cent, and it is recorded as such."""
    d = _authorize(_deploy_intent(), _deploy_deltas(), tx=_deploy_tx(), price=1.0)
    assert d.allowed is True, d.reason
    assert d.amount_usd == 0.0


def test_an_unpriceable_fee_refuses_rather_than_booking_zero():
    d = tx_guard.authorize(
        _deploy_intent(), _deploy_tx(), holder=HOLDER, gate=_gate(),
        execution_context=None, simulate_fn=lambda **_: _deploy_deltas(),
        price_fn=lambda chain, addr: None,
        rpc_is_pinned_fn=lambda chain: True, halted_fn=lambda: False,
        entry_paused_fn=lambda: False, forged_fn=lambda ctx, tool: False)
    assert d.allowed is False
    assert "price" in d.reason.lower()


def test_a_deploy_intent_carrying_a_destination_refuses():
    d = _authorize(_deploy_intent(to=POOL), _deploy_deltas(), tx=_deploy_tx())
    assert d.allowed is False
    assert "no destination" in d.reason


def test_a_deploy_intent_whose_TRANSACTION_has_a_destination_refuses():
    """The intent says CREATE, the transaction is a CALL. That is the shape an
    attacker would use to get calldata signed under a deploy's looser rules."""
    d = _authorize(_deploy_intent(), _deploy_deltas(), tx=_deploy_tx(to=POOL))
    assert d.allowed is False
    assert "CALL, not the CREATE" in d.reason


def test_a_deploy_declaring_a_token_refuses():
    d = _authorize(_deploy_intent(token=USDC), _deploy_deltas(), tx=_deploy_tx())
    assert d.allowed is False
    assert "NATIVE value only" in d.reason


def test_a_constructor_that_pays_the_deployer_refuses():
    d = _authorize(_deploy_intent(), _deploy_deltas(native_delta=10 ** 15),
                   tx=_deploy_tx())
    assert d.allowed is False
    assert "pays the deployer" in d.reason


def test_a_constructor_that_overspends_its_endowment_refuses():
    d = _authorize(_deploy_intent(amount_raw=10 ** 15),
                   _deploy_deltas(native_delta=-10 ** 17), tx=_deploy_tx())
    assert d.allowed is False
    assert "spends more than it was endowed" in d.reason


def test_a_tampered_runtime_refuses_before_anything_is_signed():
    produced = bytearray(bytes.fromhex(token_template.RUNTIME[2:]))
    produced[100] ^= 0xFF
    d = _authorize(_deploy_intent(),
                   _deploy_deltas(return_data="0x" + bytes(produced).hex()),
                   tx=_deploy_tx())
    assert d.allowed is False
    assert "not the contract that was declared" in d.reason


def test_an_endowed_deploy_is_priced_on_endowment_plus_fee():
    endowed = _authorize(_deploy_intent(amount_raw=10 ** 18, max_spend_usd=100.0),
                         _deploy_deltas(native_delta=-10 ** 18), tx=_deploy_tx())
    free = _authorize(_deploy_intent(), _deploy_deltas(), tx=_deploy_tx())
    assert endowed.allowed is True, endowed.reason
    assert endowed.amount_usd > free.amount_usd


def test_a_non_deploy_intent_with_no_destination_still_refuses():
    """The `to=None` relaxation must be reachable ONLY through is_deploy."""
    d = _authorize(
        tx_guard.TxIntent(chain="base", token=USDC, to=None, amount_raw=1,
                          max_spend_usd=1.0),
        _call_deltas(), tx=_deploy_tx())
    assert d.allowed is False
    assert "not a deploy" in d.reason


# ==========================================================================
# A sell into a NATIVE-quoted venue (042)
# ==========================================================================

MEME = "0xD8c32C1585758Bd7505F9ceA9C977a4294873ab2"


def _sell_intent(**kw):
    base = dict(chain="base", token=MEME, to=POOL, amount_raw=1_000,
                max_spend_usd=50.0, idempotency_key="s",
                min_native_inflow_wei=10 ** 15)
    base.update(kw)
    return tx_guard.TxIntent(**base)


def _sell_deltas(**kw):
    base = dict(ok=True, native_delta=2 * 10 ** 15,
                token_deltas={MEME: -1_000}, allowance_deltas={},
                gas_used=180_000)
    base.update(kw)
    return Deltas(**base)


def _authorize_sell(intent=None, deltas=None, *, native_price=2500.0):
    """The realistic launchpad shape: the MEME has no price at any source (it is
    minutes old), the chain's wrapped native does."""
    def _price(chain, addr):
        return None if addr.lower() == MEME.lower() else native_price

    return tx_guard.authorize(
        intent or _sell_intent(),
        {"to": POOL, "data": "0xd04c6983", "value": 0, "chainId": 8453,
         "nonce": 5, "gas": 200_000, "maxFeePerGas": 10 ** 9},
        holder=HOLDER, gate=_gate(), execution_context=None,
        simulate_fn=lambda **_: (deltas if deltas is not None else _sell_deltas()),
        price_fn=_price, rpc_is_pinned_fn=lambda chain: True,
        halted_fn=lambda: False, entry_paused_fn=lambda: False,
        forged_fn=lambda ctx, tool: False)


def test_a_sell_into_native_is_authorized_when_the_receipt_clears_the_floor():
    """Without min_native_inflow_wei, rule 6b refuses this shape outright as an
    undeclared native movement."""
    d = _authorize_sell()
    assert d.allowed is True, d.reason


def test_a_short_native_receipt_refuses():
    d = _authorize_sell(deltas=_sell_deltas(native_delta=10 ** 12))
    assert d.allowed is False
    assert "below the declared minimum" in d.reason


def test_a_sell_that_returns_NOTHING_refuses():
    d = _authorize_sell(deltas=_sell_deltas(native_delta=0))
    assert d.allowed is False
    assert "returns nothing observable" in d.reason


def test_a_native_OUTFLOW_on_a_declared_receipt_refuses():
    d = _authorize_sell(deltas=_sell_deltas(native_delta=-10 ** 15))
    assert d.allowed is False


def test_the_UNPRICEABLE_token_is_valued_by_its_MEASURED_native_receipt():
    """A launchpad token minutes old has no price at any source. Pricing the
    outflow dead-ends and the sell refuses — the reachable-but-not-executable
    failure. The receipt is exact and unfalsifiable, so the caps run on it."""
    prices = {}

    def _price(chain, addr):
        # The meme has no price; the chain's wrapped native does.
        from core.wallet import chains
        if addr.lower() == MEME.lower():
            return None
        prices["asked"] = addr
        return 2500.0

    d = tx_guard.authorize(
        _sell_intent(), {"to": POOL, "data": "0xd04c6983", "value": 0,
                         "chainId": 8453, "nonce": 5, "gas": 200_000,
                         "maxFeePerGas": 10 ** 9},
        holder=HOLDER, gate=_gate(), execution_context=None,
        simulate_fn=lambda **_: _sell_deltas(),
        price_fn=_price, rpc_is_pinned_fn=lambda chain: True,
        halted_fn=lambda: False, entry_paused_fn=lambda: False,
        forged_fn=lambda ctx, tool: False)
    assert d.allowed is True, d.reason
    # 0.002 native at $2500 = $5.00
    assert d.amount_usd == pytest.approx(5.0, abs=0.01)


def test_an_unpriceable_NATIVE_asset_still_refuses():
    """Unknown is never free, even on the fallback path."""
    d = _authorize_sell(native_price=None)
    assert d.allowed is False
    assert "price" in d.reason.lower()


def test_a_declared_zero_minimum_refuses_as_a_caller_bug():
    d = _authorize_sell(intent=_sell_intent(min_native_inflow_wei=0))
    assert d.allowed is False
    assert "greater than zero" in d.reason


def test_an_ERC20_send_with_NO_declared_receipt_still_refuses_native_movement():
    """The pre-042 rule is untouched for every intent that does not declare a
    native receipt — a transfer that also moves native is still undeclared."""
    d = _authorize_sell(intent=_sell_intent(min_native_inflow_wei=None))
    assert d.allowed is False
    assert "unexpected native balance change" in d.reason


# ==========================================================================
# CREATE2 — the deterministic shape (042b)
# ==========================================================================

def _c2_tx(**kw):
    from core.wallet import deploy_guard
    base = {"to": deploy_guard.CREATE2_FACTORY, "data": "0xdead", "value": 0,
            "chainId": 8453, "nonce": 11, "gas": 120_000,
            "maxFeePerGas": 10 ** 9}
    base.update(kw)
    return base


def _c2_intent(**kw):
    from core.wallet import deploy_guard
    base = dict(chain="base", token=None, to=deploy_guard.CREATE2_FACTORY,
                amount_raw=0, max_spend_usd=5.0, idempotency_key="d",
                is_deploy=True, init_code=token_template.INIT_CODE,
                create2_salt="0x" + "11" * 32)
    base.update(kw)
    return tx_guard.TxIntent(**base)


def _c2_deltas(**kw):
    from core.wallet import deploy_guard
    addr = deploy_guard.predict_create2_address(
        "0x" + "11" * 32, token_template.INIT_CODE)
    base = dict(ok=True, native_delta=0, token_deltas={}, allowance_deltas={},
                gas_used=501_207,
                return_data="0x" + "00" * 12 + addr[2:].lower())
    base.update(kw)
    return Deltas(**base)


def test_a_create2_deploy_is_authorized_and_names_its_committed_address():
    d = _authorize(_c2_intent(), _c2_deltas(), tx=_c2_tx(), price=2600.0)
    assert d.allowed is True, d.reason
    assert d.deploy is not None
    assert d.deploy.predicted_address.startswith("0x")


def test_a_create2_intent_addressed_anywhere_but_the_factory_refuses():
    """The pinned factory is what makes the address a proof. Any other
    destination is an ordinary call wearing a deploy's name."""
    d = _authorize(_c2_intent(to=POOL), _c2_deltas(), tx=_c2_tx(to=POOL))
    assert d.allowed is False
    assert "pinned factory" in d.reason


def test_a_create2_intent_over_a_transaction_sent_elsewhere_refuses():
    d = _authorize(_c2_intent(), _c2_deltas(), tx=_c2_tx(to=POOL))
    assert d.allowed is False
    assert "not the pinned CREATE2 factory" in d.reason


def test_a_create2_deploy_declaring_a_token_refuses():
    d = _authorize(_c2_intent(token=USDC), _c2_deltas(), tx=_c2_tx())
    assert d.allowed is False
    assert "NATIVE value only" in d.reason


def test_a_plain_deploy_addressed_to_the_factory_still_refuses():
    """Without a salt there is no commitment, so a destination is just a
    destination — and a CREATE has none."""
    from core.wallet import deploy_guard
    d = _authorize(
        _c2_intent(create2_salt=None, to=deploy_guard.CREATE2_FACTORY),
        _c2_deltas(), tx=_c2_tx())
    assert d.allowed is False
    assert "a deployment has no destination" in d.reason
