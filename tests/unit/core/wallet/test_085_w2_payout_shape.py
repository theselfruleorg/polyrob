"""085 W2: payout shapes go to the owner on every lane (EV-1 / M-1 / A3).

The evmverbs scratch proof, as a test: with the real ``tx_guard.authorize`` and a
stub simulation, a generic call that pays an ERC-20 or native value to an
arbitrary address passed as ``lane='autonomous'``. With ``payout_shape`` it
queues for the owner. A call that declares an inflow keeps the autonomous lane.
Also 085 W5 item 3: a unit price of exactly 0.0 is "no price".
"""
from types import SimpleNamespace

import pytest

from core.wallet import tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas

HOLDER = "0x1111111111111111111111111111111111111111"
PAYEE = "0xaAaAaAaaAaAaAaaAaAAAAAAAAaaaAaAaAaaAaaAa"
TOKEN = "0x4444444444444444444444444444444444444444"
TOKEN_OUT = "0x5555555555555555555555555555555555555555"
ROUTER = "0x6666666666666666666666666666666666666666"
OWNER = "owner-1"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    import importlib
    money_regime = importlib.import_module("core.config_policy.money_regime")
    monkeypatch.setattr(money_regime, "unattended_money_allowed", lambda: True)
    for k in ("DEFI_MONITOR_EXITS", "WALLET_SIGNER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DEFI_AUTONOMOUS_TURN_TRADING", "true")
    monkeypatch.setenv("DEFI_AUTONOMOUS_MAX_USD", "300")
    monkeypatch.setattr(tx_guard, "autonomous_turn_trading_enabled", lambda: True)
    monkeypatch.setattr(tx_guard, "_principal_refusal", lambda ctx: None)
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda: OWNER)
    monkeypatch.setattr("core.instance.resolve_owner_user_id", lambda: OWNER)
    monkeypatch.setattr("core.runtime_paths.prefs_home_dir", lambda: tmp_path / "prefs")
    real = tx_guard._decimals_for
    monkeypatch.setattr(tx_guard, "_decimals_for", lambda chain, token: (
        6 if str(token).lower() in (TOKEN.lower(), TOKEN_OUT.lower()) else real(chain, token)))
    monkeypatch.setattr("core.security.refusals.record_refusal", lambda *a, **k: None)


def _price(chain, addr):
    return 1.0 if str(addr).lower() in (TOKEN.lower(), TOKEN_OUT.lower()) else 2500.0


def _authorize(intent, tx, deltas, *, autonomous=True, price_fn=_price):
    ctx = SimpleNamespace(user_id=OWNER, role="orchestrator", is_sub_agent=False,
                          session_id="s-1", metadata={})
    return tx_guard.authorize(
        intent, tx, holder=HOLDER,
        gate=PolicyGate(max_per_tx_usd=1000.0, daily_cap_usd=5000.0),
        execution_context=ctx, simulate_fn=lambda **_: deltas, price_fn=price_fn,
        rpc_is_pinned_fn=lambda c: True, halted_fn=lambda: False,
        entry_paused_fn=lambda: False,
        forged_fn=lambda c, t: autonomous, autonomous_ok_fn=lambda c, t: autonomous)


def _erc20_payout(payout_shape):
    data = "0xa9059cbb" + "0" * 24 + PAYEE[2:].lower() + hex(20_000_000)[2:].rjust(64, "0")
    tx = {"to": TOKEN, "data": data, "value": 0, "chainId": 8453, "gas": 60000,
          "maxFeePerGas": 10 ** 8}
    # The call verb binds intent.to to the decoded payee when the target is the token.
    intent = tx_guard.TxIntent(chain="base", token=TOKEN, to=PAYEE, amount_raw=20_000_000,
                               max_spend_usd=25, idempotency_key=f"k-erc20-{payout_shape}",
                               watch_spenders=(TOKEN,), payout_shape=payout_shape)
    deltas = Deltas(ok=True, native_delta=0, token_deltas={TOKEN: -20_000_000},
                    holder_transfers=((TOKEN, PAYEE, 20_000_000),), gas_used=50000)
    return intent, tx, deltas


def _native_payout(payout_shape):
    tx = {"to": PAYEE, "data": "0xdeadbeef", "value": 5 * 10 ** 15, "chainId": 8453,
          "gas": 30000, "maxFeePerGas": 10 ** 8}
    intent = tx_guard.TxIntent(chain="base", token=None, to=PAYEE, amount_raw=5 * 10 ** 15,
                               max_spend_usd=300, idempotency_key=f"k-native-{payout_shape}",
                               payout_shape=payout_shape)
    deltas = Deltas(ok=True, native_delta=-5 * 10 ** 15, token_deltas={}, gas_used=21000)
    return intent, tx, deltas


def test_the_scratch_proof_without_the_flag_was_autonomous():
    d = _authorize(*_erc20_payout(False))
    assert d.allowed is True and d.lane == "autonomous", d.reason


def test_erc20_payout_goes_to_the_owner_queue():
    d = _authorize(*_erc20_payout(True))
    assert d.allowed is False
    assert d.lane == "owner_queue"
    assert "PAYOUT" in d.reason
    assert "VALUE NOT VERIFIED" not in d.reason     # not the /send seat's extra step


def test_native_payout_goes_to_the_owner_queue():
    d = _authorize(*_native_payout(True))
    assert d.allowed is False and d.lane == "owner_queue", d.reason


def test_an_owner_turn_payout_passes_the_guard_after_the_hook_tap():
    # D2: the owner tap is the controller pre-hook (spend_exemption is None for a
    # payout). The guard itself lets an owner-direct turn through, like /send.
    d = _authorize(*_native_payout(True), autonomous=False)
    assert d.allowed is True and d.lane == "owner_direct", d.reason


def test_an_inflow_declared_call_keeps_the_autonomous_lane():
    tx = {"to": ROUTER, "data": "0x12345678" + "00" * 32, "value": 0, "chainId": 8453,
          "gas": 200000, "maxFeePerGas": 10 ** 8}
    intent = tx_guard.TxIntent(chain="base", token=TOKEN, to=ROUTER, amount_raw=10_000_000,
                               max_spend_usd=25, idempotency_key="k-inflow",
                               watch_spenders=(ROUTER,), inflow_token=TOKEN_OUT,
                               min_inflow_raw=9_000_000, payout_shape=False)
    deltas = Deltas(ok=True, native_delta=0,
                    token_deltas={TOKEN: -10_000_000, TOKEN_OUT: 9_900_000},
                    holder_transfers=((TOKEN, ROUTER, 10_000_000),), gas_used=150000)
    d = _authorize(intent, tx, deltas)
    assert d.allowed is True and d.lane == "autonomous", d.reason


# -- 085 W5 item 3: a unit price of exactly 0.0 is no price -------------------

def test_a_zero_token_price_never_values_an_outflow_at_zero():
    def zero(chain, addr):
        return 0.0 if str(addr).lower() == TOKEN.lower() else 2500.0
    d = _authorize(*_erc20_payout(False), price_fn=zero)
    assert d.allowed is False
    assert d.lane != "autonomous"


def test_a_zero_price_never_values_a_grant_at_zero():
    def zero(chain, addr):
        return 0.0 if str(addr).lower() == TOKEN.lower() else 2500.0
    intent = tx_guard.TxIntent(chain="base", token=TOKEN, to=ROUTER, amount_raw=0,
                               max_spend_usd=25, idempotency_key="k-grant",
                               expected_allowance_grants=((TOKEN, ROUTER, 10 ** 12),))
    usd, refusal = tx_guard._price_allowance_grants(
        intent, price_fn=zero, fallback_price_fn=None, execution_context=None)
    assert refusal is not None and refusal.allowed is False


def test_spend_price_reads_zero_as_none():
    from core.wallet.tx_guard_steps.value import _spend_price
    assert _spend_price(0.0) is None
    assert _spend_price(-1.0) is None
    assert _spend_price(None) is None
    assert _spend_price(1.25) == 1.25


# -- spend lane: the owner tap on an owner turn too (D2) ----------------------

@pytest.fixture
def _tiered(monkeypatch):
    monkeypatch.setenv("DEFI_TIERED_SPEND_LANE", "true")
    monkeypatch.setattr("core.config_policy.spend_lane.autonomous_ceiling_usd", lambda: 300.0)


def test_spend_exemption_is_none_for_a_payout_call(_tiered):
    from core.config_policy.spend_lane import spend_exemption
    erc20 = {"to": TOKEN, "spend_token": TOKEN, "spend_max_raw": 20_000_000,
             "max_spend_usd": 25.0, "dry_run": False}
    native = {"to": PAYEE, "value": 0.005, "max_spend_usd": 25.0, "dry_run": False}
    assert spend_exemption("defi_trade_call", erc20) is None
    assert spend_exemption("defi_trade_call", native) is None


BASE_WETH = "0x4200000000000000000000000000000000000006"
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def _canon_price(chain, addr):
    return {BASE_WETH.lower(): 2500.0, BASE_USDC.lower(): 1.0}.get(str(addr).lower())


def test_spend_exemption_is_unchanged_for_a_fair_inflow_call(_tiered, monkeypatch):
    from core.config_policy import spend_lane
    monkeypatch.setattr(spend_lane, "_SPEND_PRICE", _canon_price)
    params = {"chain": "base", "to": ROUTER, "spend_token": BASE_USDC,
              "spend_max_raw": 10_000_000, "receive_token": BASE_WETH,
              "receive_min_raw": 3_900_000_000_000_000,      # 0.0039 WETH = $9.75 of $10
              "max_spend_usd": 25.0, "dry_run": False}
    assert "within" in (spend_lane.spend_exemption("defi_trade_call", params) or "")


def test_spend_exemption_endowed_deploy_needs_the_owner(_tiered):
    from core.config_policy.spend_lane import spend_exemption
    assert spend_exemption("defi_trade_deploy_contract",
                           {"value": 0.01, "max_spend_usd": 25.0, "dry_run": False}) is None
    assert spend_exemption("defi_trade_deploy_contract",
                           {"max_spend_usd": 25.0, "dry_run": False})


def test_a_payout_dry_run_is_still_a_simulation(_tiered):
    from core.config_policy.spend_lane import spend_exemption
    assert spend_exemption("defi_trade_call", {"to": PAYEE, "value": 0.005,
                                               "max_spend_usd": 25.0, "dry_run": True})


def test_payout_shaped_predicate():
    from core.config_policy.spend_lane import payout_shaped
    assert payout_shaped("defi_trade_call", {"value": 0.1})
    assert payout_shaped("defi_trade_call", {"spend_token": TOKEN})
    assert payout_shaped("defi_trade_call", {"spend_token": TOKEN, "receive_token": TOKEN_OUT,
                                             "receive_min_raw": 0})
    # A junk receive token is never a fair inflow (085 W2, money M-1).
    assert payout_shaped("defi_trade_call", {"spend_token": TOKEN,
                                             "receive_token": TOKEN_OUT,
                                             "receive_min_raw": 1}, price_fn=_price)
    assert not payout_shaped("defi_trade_call", {
        "chain": "base", "value": 0.004, "receive_token": BASE_USDC,
        "receive_min_raw": 9_000_000}, price_fn=_canon_price)
    assert not payout_shaped("defi_trade_call", {})           # moves nothing
    assert not payout_shaped("defi_trade_swap", {"value": 1})
    assert payout_shaped("defi_trade_deploy_contract", {"value": "junk"})   # fail closed


# -- the policy-row ratchet: every capped money verb answers payout_shape -----

#: Every ``lane="defi"`` spend verb, and why it cannot pay an unpinned address
#: on the autonomous lane. A NEW defi-lane verb fails here until it is answered:
#: either its counterparty is bound (say how), or it sets ``TxIntent.payout_shape``
#: and joins ``spend_lane.PAYOUT_SHAPE_VERBS``.
PAYOUT_ANSWERS = {
    "dapp_browser_dapp_connect": "payout_shape per page tx (native to a non-contract); "
                                 "an ERC-20 transfer is an undeclared token and refuses",
    "defi_trade_swap": "inflow-bound: the route's min-out is asserted",
    "defi_trade_solana_swap": "inflow-bound: the mirrored guard's min-out",
    "defi_trade_approve_token": "a grant, not a payout: non-pinned spender = owner turn",
    "defi_trade_revoke_approval": "risk reducing: sets an allowance to zero",
    "defi_trade_bridge": "destination = the wallet; arrival is measured",
    "defi_trade_wrap": "destination = the pinned wrapped native",
    "defi_trade_unwrap": "destination = the pinned wrapped native",
    "defi_trade_call": "payout_shape",
    "defi_trade_deploy_contract": "payout_shape (value > 0); endowment proven to stay",
    "defi_trade_deploy_token": "no endowment; the supply is minted to the wallet",
    "defi_trade_solana_deploy_token": "rent only; the mint is the wallet's",
    "defi_trade_lp_add": "pinned position manager; the position is minted to the wallet",
    "defi_trade_lp_remove": "risk reducing: proceeds return to the wallet",
    "defi_trade_lp_collect": "risk reducing: fees return to the wallet",
    "defi_trade_nft_revoke_approval": "risk reducing",
    "defi_trade_register_agent": "pinned registry; the fee is the whole cost",
    "defi_trade_set_agent_uri": "pinned registry; moves nothing",
    "launchpad_buy": "pinned launchpad router; the token inflow is asserted",
    "launchpad_sell": "pinned launchpad router; the native inflow is asserted",
    "launchpad_claim": "receives only (the claim intent shape); sends nothing",
    "launchpad_launch": "pinned launchpad factory; the fee is the cost",
    "agent_nft_collection_reveal": "pinned collection; moves nothing",
    "agent_nft_journal": "pinned JournalLog; moves nothing",
    "agent_nft_revoke_all": "risk reducing",
}


def test_every_defi_lane_verb_answers_payout_shape():
    import core.config_policy.spend_lane as sl
    from core.verb_policy import ids_where
    defi = set(ids_where(lane="defi"))
    unanswered = sorted(v for v in defi if v not in PAYOUT_ANSWERS)
    assert not unanswered, (
        f"{unanswered}: a defi-lane money verb must answer whether it can pay an "
        f"unpinned address (085 W2). Bind its counterparty, or set "
        f"TxIntent.payout_shape from spend_lane.payout_shaped and answer it here")
    shaped = {v for v, why in PAYOUT_ANSWERS.items() if why.startswith("payout_shape")}
    assert shaped <= defi | {"dapp_browser_dapp_connect"}
    # payout_shaped() answers True only for the verbs this table names as payout_shape.
    for verb in defi - shaped:
        assert not sl.payout_shaped(verb, {"value": 1, "spend_token": "0x1"}), verb
