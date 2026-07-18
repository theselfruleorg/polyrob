"""Task 9: the autonomy budget gate is removed, not merely disabled.

`AUTONOMY_BUDGET_USD` was a $10/day RATE ceiling that cannot protect a finite
balance — Rob was under it every single day while the OpenRouter balance went
to zero and the agent died. It also gated on the now-deleted MERGED ledger
number (Task 8), so an x402 wallet payment ate the agent's compute budget.

This test proves three things:
  1. The module is actually gone (not just unused).
  2. Its flags left the static flag catalog (docs/CONFIGURATION.md's SSOT).
  3. `metering_gate` — a DIFFERENT, independent gate ("refuse to START a
     money-moving run unmetered") — survives untouched. It is not the same
     mechanism and must not be collateral damage.
"""
import importlib

import pytest


def test_budget_gate_module_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("agents.task.runtime.budget_gate")


def test_budget_flags_absent_from_catalog():
    # core/flags_catalog.py:14 — CATALOG: list[tuple[str, str, str]]
    #   each entry is (flag_name, group, documented_default)
    from core.flags_catalog import CATALOG
    names = {row[0] for row in CATALOG}
    assert "AUTONOMY_BUDGET_USD" not in names
    assert "AUTONOMY_BUDGET_WINDOW_DAYS" not in names
    assert "BUDGET_AWARE_AUTONOMY" not in names


def test_metering_gate_survives_budget_gate_removal():
    """metering_gate justified itself partly via the budget cap's docstring, but
    its job — refuse to START a money-moving run unmetered — is independent.
    Verified exports: metering_available(task_agent),
    unmetered_money_gate(task_agent, tools)."""
    from agents.task.runtime.metering_gate import (metering_available,
                                                   unmetered_money_gate)
    assert callable(metering_available) and callable(unmetered_money_gate)
