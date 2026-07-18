"""Regression (P1 finalization): balance_manager was only bound inside
`if enable_credit_system`, but referenced by the deposit-monitor branch — an
UnboundLocalError when the credit system is OFF but the deposit monitor is ON with an
RPC URL. It must be bound (to None) before the conditional block that uses it.
"""
import inspect

import core.initialization as init


def test_balance_manager_bound_before_use():
    # Strip comment lines so a comment mentioning `if balance_manager:` (like the
    # explanatory note on the fix) can't fool the ordering check.
    src = "\n".join(
        l for l in inspect.getsource(init).splitlines() if not l.lstrip().startswith("#")
    )
    none_idx = src.find("balance_manager = None")
    enable_idx = src.find("if container.config.enable_credit_system")
    use_idx = src.find("if balance_manager:")
    assert none_idx != -1, "balance_manager must be initialized to None"
    assert enable_idx != -1 and use_idx != -1
    # The unconditional None binding must precede both the conditional assignment
    # and the later `if balance_manager:` use.
    assert none_idx < enable_idx < use_idx
