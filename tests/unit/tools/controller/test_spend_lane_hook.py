"""The approval hook honours the tiered spend lane (proposal 023 §5.3 D3).

The module-level policy is tested in tests/unit/core/test_spend_lane.py; this
pins the WIRING — that an exempt call never reaches the provider, and that a
non-exempt one still does.
"""
import pytest

from tools.controller.approval import ApprovalProvider, make_approval_hook
from core.config_policy.spend_lane import defi_spend_exemption

GATED = ("defi_trade_swap", "defi_trade_transfer")


class _RecordingDenier(ApprovalProvider):
    """Stands in for owner_queue with nobody home: it denies, and records."""

    def __init__(self):
        self.calls = []

    async def request(self, action_name, params, context):
        self.calls.append(action_name)
        return False


@pytest.fixture
def provider():
    return _RecordingDenier()


@pytest.mark.asyncio
async def test_dry_run_never_reaches_the_provider(provider, monkeypatch):
    monkeypatch.delenv("DEFI_TIERED_SPEND_LANE", raising=False)
    hook = make_approval_hook(provider, GATED, exempt_fn=defi_spend_exemption)
    assert await hook("defi_trade_swap", {"dry_run": True, "max_spend_usd": 5}, None) is None
    assert provider.calls == []


@pytest.mark.asyncio
async def test_live_spend_reaches_the_provider_when_the_flag_is_off(provider, monkeypatch):
    monkeypatch.delenv("DEFI_TIERED_SPEND_LANE", raising=False)
    hook = make_approval_hook(provider, GATED, exempt_fn=defi_spend_exemption)
    deny = await hook("defi_trade_swap", {"dry_run": False, "max_spend_usd": 0.5}, None)
    assert deny and "approval denied" in deny
    assert provider.calls == ["defi_trade_swap"]


@pytest.mark.asyncio
async def test_within_ceiling_live_spend_is_exempt_when_the_flag_is_on(provider, monkeypatch):
    monkeypatch.setenv("DEFI_TIERED_SPEND_LANE", "true")
    monkeypatch.setenv("DEFI_AUTONOMOUS_MAX_USD", "1")
    hook = make_approval_hook(provider, GATED, exempt_fn=defi_spend_exemption)
    assert await hook("defi_trade_swap", {"dry_run": False, "max_spend_usd": 0.9}, None) is None
    assert provider.calls == []


@pytest.mark.asyncio
async def test_over_ceiling_live_spend_still_queues_when_the_flag_is_on(provider, monkeypatch):
    monkeypatch.setenv("DEFI_TIERED_SPEND_LANE", "true")
    monkeypatch.setenv("DEFI_AUTONOMOUS_MAX_USD", "1")
    hook = make_approval_hook(provider, GATED, exempt_fn=defi_spend_exemption)
    deny = await hook("defi_trade_swap", {"dry_run": False, "max_spend_usd": 25.0}, None)
    assert deny and "approval denied" in deny
    assert provider.calls == ["defi_trade_swap"]


@pytest.mark.asyncio
async def test_a_raising_exempt_fn_fails_closed(provider):
    """A broken predicate must gate normally, never wave the call through."""
    def _boom(action_name, params):
        raise RuntimeError("predicate is broken")

    hook = make_approval_hook(provider, GATED, exempt_fn=_boom)
    deny = await hook("defi_trade_swap", {"dry_run": False}, None)
    assert deny and "approval denied" in deny
    assert provider.calls == ["defi_trade_swap"]


@pytest.mark.asyncio
async def test_no_exempt_fn_is_byte_identical_to_today(provider):
    hook = make_approval_hook(provider, GATED)
    assert await hook("defi_trade_swap", {"dry_run": True}, None)
    assert provider.calls == ["defi_trade_swap"]
    assert await hook("some_other_tool", {}, None) is None
