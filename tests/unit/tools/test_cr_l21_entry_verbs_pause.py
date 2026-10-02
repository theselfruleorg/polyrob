"""CR-L21 remainder: the scoped `/pause trading` (``trade_entry``) binds every
ENTRY money verb — bridge, EVM/SPL deploy, launchpad launch/buy, dapp connect —
while an exit (launchpad sell, a generic contract_call) still runs."""
import types

import pytest

import core.autonomy_control as ac
import core.money.authority as auth


@pytest.fixture
def trading_paused(monkeypatch):
    st = ac.PauseState(paused=True, scopes=("trading",), since=None, until=None,
                       set_by="owner", via="test", reason="stop entries",
                       source="record", record_scopes=("trading",))

    def fake_allows(kind, data_dir=None):
        denied = kind == "trade_entry"
        return ac.Decision(not denied,
                           "paused (trading) by owner via test" if denied else "running",
                           st)

    monkeypatch.setattr(ac, "allows", fake_allows)
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner-1")
    monkeypatch.setattr(auth, "delegated_refusal", lambda ctx, verb: None)


def test_bridge_is_an_entry(trading_paused):
    from tools.defi import bridge_verb
    assert "trading" in (bridge_verb._refuse_paused() or "")


def test_deploy_is_an_entry_but_contract_call_default_is_not(trading_paused):
    from tools.defi import deploy_verb
    assert "trading" in (deploy_verb._refuse_paused(entry=True) or "")
    assert deploy_verb._refuse_paused() is None      # contract_call's call shape


@pytest.mark.asyncio
async def test_evm_deploy_refuses_under_the_trading_pause(trading_paused, monkeypatch):
    from tools.defi import deploy_verb
    import tools.defi.trade_tool as tt
    monkeypatch.setattr(tt, "_unsupported_chain", lambda chain: None)

    class _Tool:
        def _ar(self, *, content=None, error=None):
            return types.SimpleNamespace(content=content, error=error)

        def _get_wallet(self):
            raise AssertionError("the pause must refuse before the wallet")

    res = await deploy_verb._perform_deploy(
        _Tool(), execution_context=None, verb="deploy_token", chain="base",
        init_code="0x00", value_wei=0, max_spend_usd=1.0, dry_run=False,
        describe="t", expected_runtime=None, immutable_slots=())
    assert res.error and "trading" in res.error and "NOT SENT" in res.error


@pytest.mark.asyncio
async def test_spl_deploy_refuses_under_the_trading_pause(trading_paused, monkeypatch):
    from tools.defi import spl_deploy_verb
    import tools.defi.deploy_verb as dv
    import tools.defi.trade_tool as tt
    monkeypatch.setattr(dv, "deploy_enabled", lambda: True)
    monkeypatch.setattr(tt, "_solana_trade_enabled", lambda: True)

    class _Tool:
        def _ar(self, *, content=None, error=None):
            return types.SimpleNamespace(content=content, error=error)

        def _solana_turn_gate(self, *a, **k):
            raise AssertionError("the pause must refuse first")

    params = types.SimpleNamespace(dry_run=False)
    res = await spl_deploy_verb.perform_solana_deploy_token(_Tool(), params, None)
    assert res.error and "trading" in res.error and "NOT SENT" in res.error


def test_launchpad_launch_and_buy_refuse_sell_runs(trading_paused, monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_ENABLED", "true")
    from tools.launchpad.tool import LaunchpadTool
    tool = LaunchpadTool("launchpad", config=None, container=None)
    assert "trading" in (tool._preflight(None, "launch a token", dry_run=False) or "")
    assert "trading" in (tool._preflight(None, "buy on a launchpad", dry_run=False) or "")
    assert tool._preflight(None, "sell on a launchpad", dry_run=False,
                           entry=False) is None


@pytest.mark.asyncio
async def test_launchpad_sell_verb_passes_entry_false(trading_paused, monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_ENABLED", "true")
    from tools.launchpad.tool import LaunchpadTool, TradeParams
    tool = LaunchpadTool("launchpad", config=None, container=None)
    seen = {}

    def spy(ctx, verb, *, dry_run, entry=True):
        seen[verb] = entry
        return "stop"

    tool._preflight = spy
    tok = "0xD8c32C1585758Bd7505F9ceA9C977a4294873ab2"
    await tool.launchpad_sell(TradeParams(token=tok, amount=1, max_spend_usd=1, dry_run=False))
    assert seen == {"sell on a launchpad": False}


@pytest.mark.asyncio
async def test_dapp_connect_refuses_under_the_trading_pause(trading_paused, monkeypatch):
    monkeypatch.setenv("DAPP_BROWSER_ENABLED", "true")
    from tools.dapp_browser.tool import ConnectParams, DappBrowserTool
    tool = DappBrowserTool()
    res = await tool.dapp_connect(ConnectParams(
        url="https://app.example.org", chain="base", max_spend_usd=5,
        session_budget_usd=10), execution_context=None)
    assert res.error and "trading" in res.error and "Nothing was connected" in res.error
