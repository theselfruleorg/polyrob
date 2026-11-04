"""099 review C14: an ORDER run (mandate ``ord_…``) never rides the DEFI_MONITOR_EXITS
carve-out — it is admitted only through the offer step (4c), on EVM and Solana."""
import types

from core.money.authorize import FORGED_TEXTS
from tests.unit.core.wallet.test_cr_h01_exit_shaped_grant import (  # noqa: F401
    ROUTER, _authorize, _decimals, _deltas, _intent)


def _ctx(**meta):
    return types.SimpleNamespace(user_id="local", role="orchestrator", is_sub_agent=False,
                                 session_id="s_c14", metadata=meta)


def test_evm_monitor_exit_admits_a_plain_run_but_not_an_order_run(monkeypatch):
    monkeypatch.setenv("DEFI_MONITOR_EXITS", "true")
    plain = _authorize(_intent(ROUTER), _deltas(ROUTER), ctx=_ctx(), forged=True)
    assert plain.reason not in FORGED_TEXTS         # the carve-out let it past step 2
    order = _authorize(_intent(ROUTER), _deltas(ROUTER), ctx=_ctx(mandate="ord_abc"),
                       forged=True)
    assert not order.allowed and order.reason in FORGED_TEXTS


def test_solana_monitor_exit_refuses_an_order_run(monkeypatch):
    monkeypatch.setenv("DEFI_MONITOR_EXITS", "true")
    monkeypatch.setattr("tools.controller.action_registration._is_forged_or_autonomous_turn",
                        lambda ctx, tool: True)
    monkeypatch.setattr("tools.controller.action_registration._is_autonomous_goal_turn",
                        lambda ctx, tool: False)
    monkeypatch.setattr("tools.controller.turn_origin._is_forged_or_autonomous_turn",
                        lambda ctx, tool: True)
    from core.wallet import tx_guard
    monkeypatch.setattr(tx_guard, "_halted", lambda: False)
    monkeypatch.setattr(tx_guard, "_entry_paused", lambda: False)
    from tools.defi.trade_tool import DefiTradeTool
    tool = DefiTradeTool.__new__(DefiTradeTool)
    ok = tool._solana_turn_gate(_ctx(), exit_shaped_fn=lambda: True)
    assert ok == (None, True, True)
    refused, auto, monitor = tool._solana_turn_gate(_ctx(mandate="ord_abc"),
                                                    exit_shaped_fn=lambda: True)
    assert refused and "cannot move funds" in refused and not auto and not monitor
