"""125 S6 (B-M2): the Solana turn gate asks the SAME turn-origin function as tx_guard step 2.

``core.wallet.tx_guard_steps.turn.turn_origin_verdict`` is the kernel half of the
turn-origin bar (``authorize_spend`` + the DEFI_MONITOR_EXITS carve-out). The EVM guard
and ``DefiTradeTool._solana_turn_gate`` both call it, so a refusal put into it refuses
both chains, and the two chains reach the same outcome on every combination of the
pause, the entry pause, a forged turn, an admitted autonomous run and an exit shape.
"""
import itertools
import types

import pytest

from core.wallet import tx_guard
from core.wallet.tx_guard_steps import turn

HOLDER = "0x" + "2" * 40
TO = "0x" + "3" * 40


def _ctx(present=True):
    if not present:
        return None
    return types.SimpleNamespace(user_id="local", role="orchestrator", is_sub_agent=False,
                                 session_id="s_s6", metadata={})


def _evm(ctx, *, halt, entry, forged, auto_ok):
    """tx_guard steps 0-2 only: the steps the SVM gate mirrors."""
    from core.wallet.tx_guard_steps import GuardContext
    intent = tx_guard.TxIntent(chain="base", token=None, to=TO, amount_raw=1,
                               max_spend_usd=1.0, expected_allowance_grants=(),
                               idempotency_key="s6")
    c = GuardContext(
        intent=intent, tx={"to": TO, "data": "0x", "value": 1, "chainId": 8453},
        holder=HOLDER, gate=None, execution_context=ctx, tool_self=None,
        simulate_fn=None, price_fn=None, fallback_price_fn=None, rpc_is_pinned_fn=None,
        halted_fn=lambda: halt, entry_paused_fn=lambda: entry,
        forged_fn=lambda _c, _t: forged, autonomous_ok_fn=lambda _c, _t: auto_ok,
        liquidity_rpc=None, account_rpc=None)
    for step in (turn.step_0_owner_authority, turn.step_1_owner_pause,
                 turn.step_1b_entry_pause, turn.step_2_turn_origin):
        d = step(c)
        if d is not None:
            return d.reason, False, False
    return None, c.autonomous_origin, c.monitor_exit


def _svm(ctx, *, exit_shaped):
    from tools.defi.trade_tool import DefiTradeTool
    tool = DefiTradeTool.__new__(DefiTradeTool)
    return tool._solana_turn_gate(ctx, exit_shaped_fn=lambda: exit_shaped)


@pytest.fixture
def world(monkeypatch):
    import tools.controller.action_registration as ar
    import tools.controller.turn_origin as to
    import core.wallet.authority as wa
    monkeypatch.setattr(wa, "turn_refusal", lambda ctx: None)
    state = {}

    def set_(*, halt, entry, forged, auto_ok, exit_shaped, monitor):
        monkeypatch.setattr(tx_guard, "_halted", lambda: halt)
        monkeypatch.setattr(tx_guard, "_entry_paused", lambda: entry)
        monkeypatch.setattr(tx_guard, "_intent_is_exit_shaped", lambda i: exit_shaped)
        monkeypatch.setattr(tx_guard, "_autonomous_turn_allowed",
                            lambda ctx, ts, fn, **kw: bool(fn and fn(ctx, ts)))
        monkeypatch.setattr(ar, "_is_forged_or_autonomous_turn", lambda c, t: forged)
        monkeypatch.setattr(to, "_is_forged_or_autonomous_turn", lambda c, t: forged)
        monkeypatch.setattr(ar, "_is_autonomous_goal_turn", lambda c, t: auto_ok)
        monkeypatch.setenv("DEFI_MONITOR_EXITS", "true" if monitor else "false")
        state.update(halt=halt, entry=entry, forged=forged, auto_ok=auto_ok,
                     exit_shaped=exit_shaped)
    return set_


def test_a_refusal_in_the_shared_function_refuses_both_chains(world, monkeypatch):
    world(halt=False, entry=False, forged=False, auto_ok=False, exit_shaped=False,
          monitor=False)
    monkeypatch.setattr(turn, "turn_origin_verdict",
                        lambda *a, **k: ("refused: the shared turn bar said no", False, False))
    ctx = _ctx()
    assert _evm(ctx, halt=False, entry=False, forged=False, auto_ok=False)[0] == \
        "refused: the shared turn bar said no"
    assert _svm(ctx, exit_shaped=False)[0] == "refused: the shared turn bar said no"


_TABLE = list(itertools.product((False, True), repeat=6))


@pytest.mark.parametrize("halt,entry,forged,auto_ok,exit_shaped,monitor", _TABLE)
@pytest.mark.parametrize("present", (True, False))
def test_both_chains_reach_the_same_outcome(world, present, halt, entry, forged, auto_ok,
                                            exit_shaped, monitor):
    world(halt=halt, entry=entry, forged=forged, auto_ok=auto_ok,
          exit_shaped=exit_shaped, monitor=monitor)
    ctx = _ctx(present)
    evm = _evm(ctx, halt=halt, entry=entry, forged=forged, auto_ok=auto_ok)
    svm = _svm(ctx, exit_shaped=exit_shaped)
    assert (evm[0] is None) == (svm[0] is None), (evm, svm)
    assert evm[1:] == svm[1:], (evm, svm)
