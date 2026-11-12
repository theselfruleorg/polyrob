"""115 N5 / 111 WL3 — a reverted simulation is named only from the tables of the contracts the
transaction reaches, with the error's arguments.

Before: every revert was matched against the ERC-6551P account table, so a plain call reverting
``NotOwner()`` (a common error) read "the account refused it", and a sale mint refused by
``NotListed()`` read only "execution failed".
"""
import types

from core.wallet import collection_errors as CE, simulation, tx_guard
from tests.collection_pins import MARKET, pin, polyrob_profile, sale_address

COLL = "0xC011000000000000000000000000000000000001"
SALE = sale_address(COLL)


def _intent(**kw):
    base = dict(chain="robinhood", via_account=None, is_collection_mint=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_a_sale_mint_names_the_sale_error(monkeypatch):
    pin(monkeypatch, polyrob_profile(COLL))
    got = CE.revert_name(_intent(is_collection_mint=True), {"to": SALE}, "0x665c1c57")
    assert got.startswith("the pinned sale refused it on chain: NotListed() (")
    got = CE.revert_name(_intent(is_collection_mint=True), {"to": SALE},
                         "0x51481720" + f"{5 * 10 ** 16:064x}")
    assert "WrongPrice(expected=50000000000000000)" in got


def test_a_plain_call_reverting_a_common_error_is_not_named_as_an_account(monkeypatch):
    pin(monkeypatch, polyrob_profile(COLL))
    assert CE.revert_name(_intent(), {"to": "0x" + "42" * 20}, "0x30cd7471") is None
    assert CE.revert_name(_intent(), {"to": "0x" + "42" * 20}, "0xe4073933" + "00" * 64) is None


def test_a_call_to_the_collection_names_the_collection_error(monkeypatch):
    pin(monkeypatch, polyrob_profile(COLL))
    got = CE.revert_name(_intent(), {"to": COLL}, "0x620e721c")
    assert got.startswith("the pinned collection refused it on chain: SaleNotReady()")


def test_a_call_to_the_market_names_the_sale_rail_error_with_its_index(monkeypatch):
    pin(monkeypatch, polyrob_profile(COLL))
    got = CE.revert_name(_intent(), {"to": MARKET}, "0x72fc940b" + f"{2:064x}")
    assert got.startswith("the pinned sale rail refused it on chain: ConditionFailed(index=2)")


def test_an_unreadable_registry_names_nothing_and_never_raises(monkeypatch):
    from core.wallet import collection_registry

    def boom():
        raise collection_registry.CollectionRegistryError("writable")
    monkeypatch.setattr(collection_registry, "profiles", boom)
    assert CE.revert_name(_intent(), {"to": COLL}, "0x620e721c") is None


def test_the_guard_puts_the_name_on_a_mint_refusal(monkeypatch):
    """The whole path: a reverted simulation's revert data reaches the refusal text."""
    from core.wallet.tx_guard_steps import measure
    from core.wallet.tx_guard_steps.context import GuardContext
    pin(monkeypatch, polyrob_profile(COLL))
    intent = tx_guard.TxIntent(chain="robinhood", token=None, to=SALE, amount_raw=1,
                               max_spend_usd=1.0, is_collection_mint=True, mint_ids=(1,),
                               idempotency_key="k")
    deltas = simulation.Deltas(ok=False, error="the transaction REVERTS in simulation "
                                                "({'code': -3200, 'message': 'execution failed'})",
                               revert_data="0x665c1c57")
    c = GuardContext(intent=intent, tx={"to": SALE, "data": "0x"}, holder="0x" + "11" * 20,
                     gate=None, execution_context=None, tool_self=None,
                     simulate_fn=lambda **kw: deltas, price_fn=None, fallback_price_fn=None,
                     rpc_is_pinned_fn=lambda c: True, halted_fn=lambda: False,
                     entry_paused_fn=lambda: False, forged_fn=None, autonomous_ok_fn=None,
                     liquidity_rpc=None, account_rpc=None)
    c.outer_tx = c.tx
    d = measure.step_5_simulate(c)
    assert d is not None and not d.allowed
    assert "NotListed" in d.reason and "not on the whitelist" in d.reason, d.reason


def test_the_simulation_keeps_the_revert_data():
    entry = {"status": "0x0", "error": {"code": -3200, "message": "execution failed"},
             "returnData": "0x665c1c57"}
    assert simulation._revert_data(entry) == "0x665c1c57"
