"""120 W4 — the agent's money reads (§3.4, §4.4) and the three texts of T7.

Reads only: no test here moves money, and none opens a network connection
(the wallet is on testnet, every store is a tmp file or a patched reader).
"""
import types

import pytest

from core.wallet.agent_wallet import AgentWallet
from core.wallet.config import WalletConfig
from tools.x402.client import FakeX402Client
from tools.x402.service import EmptyWalletParams, X402PayTool


def _wallet_tool():
    cfg = WalletConfig(enabled=True, backend="local_eoa", master_seed="s" * 40,
                       network="testnet", max_per_tx_usd=10.0,
                       x402_client_enabled=True, x402_facilitator_url="http://f")
    return X402PayTool(wallet=AgentWallet(cfg),
                       client=FakeX402Client(price_usd=None, pay_to=None, paid_body="X"))


def _owner(monkeypatch, refusal=None):
    from core.money import authority
    monkeypatch.setattr(authority, "turn_refusal", lambda ctx: refusal)


def _journal(monkeypatch, rows=None, exc=None):
    from core.wallet import submission_journal

    def fake(data_dir=None):
        if exc is not None:
            raise exc
        return rows or []
    monkeypatch.setattr(submission_journal, "unresolved", fake)


# --- unaccounted submissions: x402_wallet_status ------------------------------ #

@pytest.mark.asyncio
async def test_wallet_status_lists_an_unaccounted_submission_with_the_operator_step(monkeypatch):
    _owner(monkeypatch)
    _journal(monkeypatch, rows=[{"chain": "base", "tx_hash": "0xfeed"}])
    out = (await _wallet_tool().x402_wallet_status(EmptyWalletParams())).extracted_content
    from core.wallet.view import submission_lines
    for line in submission_lines([{"chain": "base", "tx_hash": "0xfeed"}]):
        assert line in out, "the agent reads the /wallet lines, not a second text"
    assert "polyrob wallet release-submission" in out
    assert "You cannot release one" in out
    assert "Spend caps:" in out


@pytest.mark.asyncio
async def test_an_unreadable_journal_is_named_and_the_rest_still_prints(monkeypatch):
    _owner(monkeypatch)
    _journal(monkeypatch, exc=OSError("disk"))
    res = await _wallet_tool().x402_wallet_status(EmptyWalletParams())
    assert res.error is None
    out = res.extracted_content
    assert "UNKNOWN" in out and "submission journal could not be read" in out
    assert "none" not in out.split("Unaccounted submissions:")[1].split("\n")[0].lower()
    assert "Spend caps:" in out and "x402 payments" in out


@pytest.mark.asyncio
async def test_no_submission_says_nothing_blocks(monkeypatch):
    _owner(monkeypatch)
    _journal(monkeypatch, rows=[])
    out = (await _wallet_tool().x402_wallet_status(EmptyWalletParams())).extracted_content
    assert "Unaccounted submissions: none" in out


@pytest.mark.asyncio
async def test_a_non_owner_does_not_read_the_journal(monkeypatch):
    _owner(monkeypatch, refusal="operator wallet access requires the bound owner identity")
    _journal(monkeypatch, rows=[{"chain": "base", "tx_hash": "0xfeed"}])
    out = (await _wallet_tool().x402_wallet_status(EmptyWalletParams())).extracted_content
    assert "0xfeed" not in out and "not shown" in out


def test_the_owner_wallet_text_still_names_no_command():
    from core.wallet.view import WalletView, render_wallet
    out = render_wallet(WalletView(owner="o", state="ready",
                                   unaccounted_submissions=[{"chain": "base",
                                                             "tx_hash": "0xabc"}]))
    assert "0xabc" in out and "polyrob" not in out


# --- the trading mandate and the bridges: defi_data.positions ----------------- #

def _bridges(monkeypatch, rows=None, exc=None):
    from core.wallet import bridge_guard

    def fake(user_id, *, db_path=None):
        if exc is not None:
            raise exc
        return rows or []
    monkeypatch.setattr(bridge_guard, "open_bridges", fake)


def _view(monkeypatch, view):
    import core.trading_report as tr
    monkeypatch.setattr(tr, "trading_view", lambda user_id, **kw: view)


def _row(**kw):
    base = {"id": "b1", "request_id": "req-1", "origin_chain_id": 8453,
            "dest_chain_id": 1, "amount_usd": 12.5, "state": "in_flight",
            "tx_ref": "0xabc", "created_at": 1_700_000_000.0, "detail": ""}
    base.update(kw)
    return base


def test_no_mandate_and_no_bridge(monkeypatch):
    from tools.defi.money_state_lines import own_state_lines
    _owner(monkeypatch)
    _view(monkeypatch, {"lines": ["trading: no mandate and no open position"],
                        "present": False, "unknown": [], "mandate": None, "halted": False})
    _bridges(monkeypatch, rows=[])
    out = "\n".join(own_state_lines("owner"))
    assert "no mandate and no open position" in out
    assert "No bridges awaiting confirmation." in out


def test_a_halted_mandate_prints_the_breaker_first_and_no_release(monkeypatch):
    from tools.defi.money_state_lines import own_state_lines
    _owner(monkeypatch)
    _view(monkeypatch, {"lines": ["mandate m1: HALTED · preset p", "breaker: LATCHED — loss"],
                        "present": True, "unknown": [], "mandate": "m1", "halted": True})
    _bridges(monkeypatch, rows=[])
    lines = own_state_lines("owner")
    assert lines[1].strip().startswith("mandate m1: HALTED")
    assert lines[2].strip().startswith("breaker: LATCHED")
    assert any("you have no release" in ln for ln in lines)


def test_trading_view_reports_halted_false_on_an_empty_home(tmp_path):
    from core.trading_report import trading_view
    view = trading_view("owner", data_dir=str(tmp_path))
    assert view["present"] is False and view["halted"] is False


def test_an_unreadable_bridge_store_is_unknown_not_none(monkeypatch):
    from tools.defi.money_state_lines import own_state_lines
    _owner(monkeypatch)
    _view(monkeypatch, {"lines": [], "present": False, "unknown": [], "mandate": None,
                        "halted": False})
    _bridges(monkeypatch, exc=OSError("disk"))
    out = "\n".join(own_state_lines("owner"))
    assert "UNKNOWN" in out and "No bridges" not in out


def test_an_open_bridge_is_listed_with_the_do_not_resend_line(monkeypatch):
    from tools.defi.money_state_lines import open_bridge_lines, own_state_lines
    _owner(monkeypatch)
    _view(monkeypatch, {"lines": [], "present": False, "unknown": [], "mandate": None,
                        "halted": False})
    _bridges(monkeypatch, rows=[_row()])
    lines = own_state_lines("owner")
    for ln in open_bridge_lines([_row()]):
        assert f"  {ln}" in lines, "the agent reads the `polyrob wallet bridges` lines"
    assert any("never send it again" in ln for ln in lines)


def test_a_leaf_does_not_read_the_mandate_or_the_bridges(monkeypatch):
    from tools.defi.money_state_lines import own_state_lines
    _owner(monkeypatch)
    _bridges(monkeypatch, rows=[_row()])
    out = own_state_lines("owner", types.SimpleNamespace(role="leaf"))
    assert len(out) == 1 and "not shown" in out[0] and "b1" not in out[0]


@pytest.mark.asyncio
async def test_a_leaf_does_not_read_the_journal(monkeypatch):
    _owner(monkeypatch)
    _journal(monkeypatch, rows=[{"chain": "base", "tx_hash": "0xfeed"}])
    out = (await _wallet_tool().x402_wallet_status(
        EmptyWalletParams(), types.SimpleNamespace(role="leaf", user_id="owner"))).extracted_content
    assert "0xfeed" not in out and "not shown" in out


def test_positions_appends_the_state_and_keeps_it_on_an_unreadable_book(monkeypatch):
    from core import open_positions
    from tools.defi import data_tool
    from tools.defi.data_tool import DefiDataTool, PositionsParams
    monkeypatch.setattr(data_tool, "_operator_read_refusal", lambda ctx: None)
    _owner(monkeypatch)
    _view(monkeypatch, {"lines": ["trading: no mandate and no open position"],
                        "present": False, "unknown": [], "mandate": None, "halted": False})
    _bridges(monkeypatch, rows=[])

    def boom(*a, **kw):
        raise OSError("disk")
    monkeypatch.setattr(open_positions, "positions_for", boom)
    ctx = types.SimpleNamespace(user_id="owner")
    res = DefiDataTool(holder=None)._positions_sync(PositionsParams(), ctx)
    assert "NOT an empty book" in res.error
    assert "Bridges in flight:" in res.error and "Trading mandate:" in res.error


# --- the asset list: x402_invoices(assets=true) ------------------------------- #

def test_an_unreadable_asset_store_is_named(monkeypatch):
    from core.payments import assets

    class _Broken:
        def list_all(self):
            raise OSError("disk")
    monkeypatch.setattr(assets, "_store", lambda data_home=None: _Broken())
    rows, error = assets.asset_listing()
    assert rows, "the builtins still resolve"
    assert error and "could not be read" in error
    assert any("could not be read" in ln for ln in assets.asset_lines())


@pytest.mark.asyncio
async def test_x402_invoices_assets_prints_the_cli_list(monkeypatch, tmp_path):
    from core.payments import assets
    from tools.x402.invoice_tool import InvoiceListParams, X402InvoiceTool
    monkeypatch.setattr(assets, "_store", lambda data_home=None: None)
    res = await X402InvoiceTool().x402_invoices(
        InvoiceListParams(assets=True), types.SimpleNamespace(user_id="owner", session_id="s"))
    for ln in assets.asset_lines():
        assert ln in res.extracted_content
    assert assets.DEFAULT_ASSET_ID in res.extracted_content

    from click.testing import CliRunner

    from cli.commands.wallet import wallet_asset_list
    cli = CliRunner().invoke(wallet_asset_list, [])
    assert cli.output.splitlines() == assets.asset_lines(), "one text for both seats"


# --- the offer report and threads: offer_manage --------------------------------- #

@pytest.mark.asyncio
@pytest.mark.parametrize("op,oid,args", [("report", "", ["report"]),
                                         ("report", "o1", ["report", "o1"]),
                                         ("threads", "o1", ["threads", "o1"])])
async def test_offer_manage_report_and_threads_read_the_owner_text(monkeypatch, op, oid, args):
    from tools.controller import card_actions, owner_ask_action
    from tools.controller.offer_actions import OPS, register_offer_actions
    assert op in OPS
    seen = {}

    async def fake_reply(user_id, words, **kw):
        seen["args"] = (user_id, words)
        return "the /offer text"
    import modules.commerce.owner_verb as ov
    monkeypatch.setattr(ov, "offer_reply", fake_reply)
    monkeypatch.setattr(card_actions, "_refusal", lambda c, ctx, uid: None)
    monkeypatch.setattr(owner_ask_action, "_user", lambda c, ctx: "owner")
    actions = {}

    class _Registry:
        def action(self, desc, param_model=None):
            def deco(fn):
                actions[fn.__name__] = (fn, param_model)
                return fn
            return deco
    register_offer_actions(types.SimpleNamespace(registry=_Registry()))
    fn, model = actions["offer_manage"]
    res = await fn(model(op=op, offer_id=oid or None), None)
    assert res.extracted_content == "the /offer text"
    assert seen["args"] == ("owner", args)


# --- T7: no agent-facing text names a command the agent cannot run --------------- #

def test_t7_texts_name_the_agent_read():
    import inspect

    from core.money import ledger
    from tools.controller import trade_mandate_action
    from tools.x402.invoice_tool import InvoiceParams
    assert "x402_invoice_x402_invoices" in InvoiceParams.model_fields["asset_id"].description
    assert "polyrob wallet asset list" not in InvoiceParams.model_fields["asset_id"].description
    src = inspect.getsource(trade_mandate_action)
    assert "/trade status shows it" not in src and "defi_data_positions" in src
    lsrc = inspect.getsource(ledger)
    assert "reconcile before further spending" not in lsrc
    assert "x402_pay_x402_wallet_status" in lsrc
