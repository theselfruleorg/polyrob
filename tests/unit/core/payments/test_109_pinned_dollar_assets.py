"""109 W1: every dollar chains.py pins on a money chain is payable; nothing else is."""
import pytest

from core.payments import assets
from core.wallet import chains


def test_every_pinned_dollar_is_a_builtin_payable_asset():
    for row in chains.all_rows():
        if row.family != "evm" or not row.money_enabled:
            continue
        pins = ([("USDC", row.usdc)] if row.usdc else []) + [
            (sym, addr) for addr, sym, _n, _d in row.stables]
        for sym, addr in pins:
            a = assets.get(f"{sym.lower()}-{row.name}")
            assert a is not None, (row.name, sym)
            assert a.chain == row.name and a.address.lower() == addr.lower()
            assert chains.is_usd_pegged(a.chain, a.address)


def test_the_robinhood_dollar_is_usdg_and_resolves_as_its_default():
    from modules.x402.invoice_assets import resolve_invoice_asset
    a = assets.get("usdg-robinhood")
    assert a is not None and a.decimals == 6 and a.symbol == "USDG"
    assert assets.get("usdc-robinhood") is None          # no USDC exists there
    assert resolve_invoice_asset("robinhood", None).asset_id == "usdg-robinhood"


def test_no_builtin_row_is_unpegged():
    for a in assets.BUILTIN_ASSETS.values():
        assert chains.is_usd_pegged(a.chain, a.address), a.asset_id


def test_a_draft_refuses_a_rail_nobody_pinned(tmp_path):
    from core.commerce import store
    from core.commerce.offer import OfferError
    m = {"spec": "polyrob.offer/1", "id": "thing", "title": "T", "summary": "S",
         "pricing": {"kind": "fixed", "amount_usd": "3.30"},
         "rails": [{"asset": "usdc-robinhood", "mode": "transfer"}],
         "inputs": {"wallet": {"type": "string", "format": "evm_address",
                               "role": "recipient"}},
         "deliverable": {"kind": "onchain_asset", "proof": "tx_receipt"},
         "fulfillment": {"playbook": "offer-thing", "tools": ["defi_trade"],
                         "budget_per_order": {"spend_usd": 0.2}},
         "listings": ["a2a"]}
    p = str(tmp_path / "commerce.db")
    with pytest.raises(OfferError, match="usdc-robinhood"):
        store.save_draft("u1", m, path=p)
    m["rails"] = [{"asset": "usdg-robinhood", "mode": "transfer"}]
    assert store.save_draft("u1", m, path=p).state == "draft"
