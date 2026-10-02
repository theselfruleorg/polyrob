"""CR-L20: a payment lane whose tool list cannot even be RESOLVED denies every
money action (the 57c9f62bf deny-all shape), never leaves them ungated."""
import asyncio

import pytest

import core.config_policy.spend_lane as spend_lane
from tests.unit.tools.controller.test_payment_approval_mode import (
    _make_controller, _owner_ctx)


@pytest.mark.parametrize("mode", ["approve", "auto"])
def test_unresolvable_payment_lane_denies_money_actions(tmp_path, monkeypatch, mode):
    import agents.task.constants as constants
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u1")
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", mode)
    constants._refreeze_payment_approval_flags_for_tests()
    monkeypatch.delattr(spend_lane, "spend_exemption")  # the import now raises
    c = _make_controller(tmp_path)
    for verb in ("defi_trade_swap", "hyperliquid_place_market_order",
                 "x402_invoice_x402_request"):
        reason = asyncio.run(c._run_pre_tool_call_hooks(
            verb, {"dry_run": False}, _owner_ctx()))
        assert reason and "failed this session" in reason, verb
    assert asyncio.run(c._run_pre_tool_call_hooks("read_file", {}, None)) is None
